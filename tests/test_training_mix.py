"""One generated chat set drowning out the rest, the validation split, ChatML, and the shared-job learning rate.

The report: four files trained together (Dolly, a Shakespeare ChatML set, the starter chat set, TinyStories), yet
every chat reply was a canned starter answer, and the learning rate sat flat at its peak for thousands of steps.
"""

import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "dashboard"))
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "jobs"))

import starter_dataset  # noqa: E402
import trainapi  # noqa: E402
import training_data as td  # noqa: E402


def starter_text():
    out = starter_dataset.generate()
    return out if isinstance(out, str) else out[0]


class RepeatTests(unittest.TestCase):
    def test_the_starter_sets_canned_replies_stop_repeating_hundreds_of_times(self):
        pieces, _ = td.normalize_pieces(starter_text())
        before = Counter(r for p in pieces for r in td.replies(p))
        self.assertGreater(before["I'm a small language model, trained from scratch on a home cluster."], 300)
        kept, dropped = td.thin_repeats(pieces, set(), Counter())
        after = Counter(r for p in kept for r in td.replies(p))
        self.assertGreater(dropped, 10_000)
        long_ones = [n for r, n in after.items() if len(r) >= td.MIN_COUNTED_REPLY]
        self.assertLessEqual(max(long_ones), td.MAX_REPEATS)
        self.assertGreater(len(kept), 5_000)   # thinned, not emptied

    def test_short_label_replies_to_different_questions_stay(self):
        pieces = ["User: Review %d: great film\nAssistant: Positive" % i for i in range(50)]
        kept, dropped = td.thin_repeats(pieces, set(), Counter())
        self.assertEqual((len(kept), dropped), (50, 0))

    def test_exact_copies_go_and_a_greeting_glued_on_is_trimmed_not_the_question(self):
        greet = "User: Hello!\nAssistant: Hello! How can I help you today?\n"
        pieces = [greet + "User: What is %d + 1?\nAssistant: It is %d, because adding one counts up." % (i, i + 1) for i in range(10)]
        pieces.append(pieces[0])
        kept, dropped = td.thin_repeats(pieces, set(), Counter())
        self.assertEqual(len(kept), 10)
        self.assertEqual(sum(p.count("How can I help") for p in kept), td.MAX_REPEATS)
        self.assertTrue(all("+ 1?" in p for p in kept))
        self.assertEqual(dropped, 2 + (10 - td.MAX_REPEATS))   # the copy (2 exchanges) + the extra greetings

    def test_zero_keeps_everything_but_exact_copies(self):
        pieces = ["User: Hi\nAssistant: Hello! How can I help you today?"] * 5 + ["User: Yo\nAssistant: Hello! How can I help you today?"] * 1
        kept, _ = td.thin_repeats(pieces, set(), Counter(), max_repeats=0)
        self.assertEqual(len(kept), 2)

    def test_plain_text_is_untouched(self):
        book = "Chapter one.\n\nIt was a dark night.\n\nChapter one."
        self.assertEqual(td.thin_repeats([book], set(), Counter()), ([book], 0))


class SplitTests(unittest.TestCase):
    def test_every_file_gives_validation_text(self):
        with tempfile.TemporaryDirectory() as d:
            a = Path(d, "dolly.jsonl")
            a.write_text("\n".join(json.dumps({"instruction": "Q%d" % i, "response": "Answer %d " % i + "word " * 20}) for i in range(100)))
            b = Path(d, "stories.txt")
            b.write_text("\n\n".join("Once upon a time %d " % i + "story " * 30 for i in range(60)))
            train, valid, reports = td.load_split([a, b])
        self.assertEqual([name for name, _ in valid], ["dolly.jsonl", "stories.txt"])
        self.assertTrue(valid[0][1].startswith("User: Q9"))           # whole records from the end of the first file
        self.assertTrue(valid[1][1].startswith("Once upon a time 5"))  # and the end of the second, at a paragraph
        for name, part in valid:
            self.assertNotIn(part[:30], train, name)
        whole = len(train) + sum(len(p) for _, p in valid)
        for _, part in valid:
            self.assertLess(len(part), 0.2 * whole)
        self.assertEqual([r["file"] for r in reports], ["dolly.jsonl", "stories.txt"])

    def test_copies_across_files_are_dropped_too(self):
        with tempfile.TemporaryDirectory() as d:
            convo = "User: Who are you?\nAssistant: I'm a small language model, trained from scratch on a home cluster."
            Path(d, "one.txt").write_text("\n\n".join([convo] + ["User: Q%d\nAssistant: Reply number %d here." % (i, i) for i in range(30)]))
            Path(d, "two.txt").write_text("\n\n".join([convo] * 30))
            train, valid, reports = td.load_split([Path(d, "one.txt"), Path(d, "two.txt")])
        self.assertEqual(reports[1]["repeats_dropped"], 30)
        self.assertEqual(valid[1], ("two.txt", ""))

    def test_a_book_is_cut_near_the_end_at_a_break(self):
        book = "\n\n".join("Paragraph %d " % i + "x" * 80 for i in range(100))
        train, valid = td.hold_out([book])
        self.assertTrue(valid.startswith("Paragraph 9"))
        self.assertAlmostEqual(len(valid) / len(book), 0.05, delta=0.02)


class ChatMLTests(unittest.TestCase):
    def test_chatml_text_becomes_user_and_assistant_lines(self):
        rec = {"text": "<|im_start|>system\nBe kind<|im_end|>\n<|im_start|>user\nSpeak, friend<|im_end|>\n"
                       "<|im_start|>assistant\nGood morrow!\nHow fare you?<|im_end|>"}
        self.assertEqual(td.record_to_text(rec), "System: Be kind\nUser: Speak, friend\nAssistant: Good morrow!\nHow fare you?")

    def test_a_jsonl_file_of_chatml_counts_as_chat(self):
        lines = [json.dumps({"text": "<|im_start|>user\nHi %d<|im_end|><|im_start|>assistant\nHail %d<|im_end|>" % (i, i)}) for i in range(3)]
        text, report = td.normalize("\n".join(lines))
        self.assertTrue(report["chat"])
        self.assertNotIn("<|im_", text)

    def test_plain_text_without_chatml_is_kept(self):
        self.assertEqual(td.record_to_text({"text": "Just words."}), "Just words.")


class SpeakerNameTests(unittest.TestCase):
    """A Shakespeare ChatML set kept "Jul:" and "Nurse:" in front of every line, so "hello" got "Jul: O, …" back."""

    def chatml(self, ask, reply):
        return json.dumps({"text": "<|im_start|>user\n%s<|im_end|>\n<|im_start|>assistant\n%s<|im_end|>" % (ask, reply)})

    def test_a_play_loses_its_speaker_names_on_both_sides(self):
        lines = [self.chatml("Rom: What light through yonder window breaks? %d" % i, "Jul: O Romeo, wherefore art thou?\nNurse: Madam!")
                 for i in range(8)] + [self.chatml("First Citizen: Hear me speak.", "Lady Capulet: Speak, then.")]
        text, report = td.normalize("\n".join(lines))
        self.assertTrue(report["chat"])
        self.assertEqual(report["names_removed"], 8 * 3 + 2)
        self.assertEqual(report["names"][:3], ["Rom", "Jul", "Nurse"])
        self.assertIn("User: What light through yonder window breaks? 0\nAssistant: O Romeo, wherefore art thou?\nMadam!", text)
        self.assertIn("User: Hear me speak.\nAssistant: Speak, then.", text)
        self.assertNotIn("Jul:", text)

    def test_messages_and_plain_chat_files_too(self):
        rec = json.dumps({"messages": [{"role": "user", "content": "ROMEO: Good night."}, {"role": "assistant", "content": "JULIET: Good night, good night!"}]})
        self.assertEqual(td.normalize(rec)[0].strip(), "User: Good night.\nAssistant: Good night, good night!")
        chat = "User: HAMLET: To be?\nAssistant: HORATIO: My lord.\n\nUser: Hi\nAssistant: OPHELIA: Good my lord."
        self.assertEqual(td.normalize_pieces(chat)[0], ["User: To be?\nAssistant: My lord.", "User: Hi\nAssistant: Good my lord."])

    def test_names_on_only_some_plays_still_come_off(self):
        # The real pairs file: Hamlet and Lear have "Ham:"/"King:" in front, other plays don't (~17% of replies).
        def pair(ask, reply):
            return json.dumps({"messages": [{"role": "user", "content": ask}, {"role": "assistant", "content": reply}]})
        recs = [pair("King: Have you your father's leave? %d" % i, "Ham: Not so, my lord.") for i in range(3)]
        recs += [pair("Lear: Speak.", "Kent: Good my liege.")]
        recs += [pair("In delivering my son from me %d" % i, "And I in going, madam, weep anew.") for i in range(14)]
        recs += [pair("Who is he?", "I know him: 'a was a botcher's prentice in Paris.")]
        text, report = td.normalize("\n".join(recs))
        self.assertEqual(report["names_removed"], 8)
        for name in ("Ham:", "King:", "Lear:", "Kent:"):
            self.assertNotIn(name, text)
        self.assertIn("User: Have you your father's leave? 0\nAssistant: Not so, my lord.", text)
        self.assertIn("Assistant: I know him: 'a was a botcher's prentice in Paris.", text)

    def test_a_few_labels_in_ordinary_answers_stay(self):
        recs = [json.dumps({"instruction": "Q%d" % i, "response": "Note: answer %d." % i if i < 3 else "Answer number %d." % i}) for i in range(10)]
        text, report = td.normalize("\n".join(recs))
        self.assertEqual(report["names_removed"], 0)
        self.assertIn("Assistant: Note: answer 0.", text)

    def test_a_group_chat_converted_with_names_keeps_them(self):
        # The multi-person converter with "keep names": names on the asking side only, never in the reply.
        recs = [json.dumps({"messages": [{"role": "user", "content": "Alice: hi %d\nBob: yo" % i}, {"role": "assistant", "content": "hey all"}]})
                for i in range(5)]
        text, report = td.normalize("\n".join(recs))
        self.assertEqual(report["names_removed"], 0)
        self.assertIn("User: Alice: hi 0\nBob: yo", text)

    def test_plain_text_is_never_touched(self):
        play = "ROMEO: But soft!\nJULIET: Ay me!\n" * 20
        self.assertEqual(td.normalize(play)[0].strip(), play.strip())


class MergeTests(unittest.TestCase):
    """All the ticked files as one shuffled .jsonl, with Shakespeare only when a message starts with the trigger."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.t = trainapi.Trainer(self.dir.name)
        play = "\n".join(json.dumps({"text": "<|im_start|>user\nRom: Speak, sweet %d.<|im_end|><|im_start|>assistant\nJul: Good morrow, %d.<|im_end|>" % (i, i)})
                         for i in range(40))
        dolly = "\n".join(json.dumps({"instruction": "What is %d plus 1?" % i, "response": "It is %d." % (i + 1)}) for i in range(60))
        stories = "\n\n".join("Once upon a time there was a cat number %d. " % i + "It liked naps. " * 20 for i in range(50))
        for name, text in (("play.jsonl", play), ("dolly.jsonl", dolly), ("stories.txt", stories)):
            self.t.paste({"name": name, "text": text})

    def tearDown(self):
        self.dir.cleanup()

    def test_merge_keeps_every_record_takes_names_off_and_adds_the_trigger(self):
        out = self.t.merge({"files": ["play.jsonl", "dolly.jsonl", "stories.txt"], "name": "all of it",
                            "triggers": {"play.jsonl": "  Speak like   Shakespeare. ", "gone.txt": "x"}})
        self.assertEqual(out["name"], "all-of-it.jsonl")
        by = {f["file"]: f for f in out["files"]}
        self.assertEqual((by["play.jsonl"]["triggered"], by["play.jsonl"]["names_removed"]), (40, 80))
        self.assertEqual(by["dolly.jsonl"]["conversations"], 60)
        self.assertNotIn("trigger", by["dolly.jsonl"])
        self.assertGreater(by["stories.txt"]["records"], 5)          # cut into pieces, so they can be shuffled
        path = Path(self.t.data_dir, "all-of-it.jsonl")
        raw = path.read_text()
        pieces, report = td.normalize_pieces(raw)
        self.assertEqual((report["format"], report["chat"], report["names_removed"]), ("jsonl", True, 0))
        self.assertEqual(len(pieces), out["records"])
        self.assertIn("User: Speak like Shakespeare. Speak, sweet 7.\nAssistant: Good morrow, 7.", pieces)
        self.assertIn("User: What is 7 plus 1?\nAssistant: It is 8.", pieces)
        self.assertFalse(any("Jul:" in p or "Rom:" in p for p in pieces))
        self.assertEqual(sum(p.count("cat number") for p in pieces), 50)
        tail = pieces[-len(pieces) // 4:]                             # what's held out comes from every file
        self.assertTrue(any("Shakespeare" in p for p in tail) and any("plus" in p for p in tail))
        self.assertFalse(any(p.name.endswith((".parts", ".tmp")) for p in path.parent.iterdir()))
        with self.assertRaises(trainapi.TrainError) as e:
            self.t.merge({"files": ["play.jsonl", "dolly.jsonl"], "name": "all-of-it"})
        self.assertIn("already a file", str(e.exception))
        with self.assertRaises(trainapi.TrainError):
            self.t.merge({"files": ["play.jsonl"]})

    def test_saving_a_script_in_the_training_format_takes_the_names_off_too(self):
        out = self.t.convert({"name": "play.jsonl"})
        self.assertEqual(out["names_removed"], 80)
        text = Path(self.t.data_dir, out["name"]).read_text()
        self.assertTrue(text.startswith("User: Speak, sweet 0.\nAssistant: Good morrow, 0."))
        self.assertNotIn("Jul:", text)

    def test_the_trigger_goes_in_front_of_the_first_message_only(self):
        piece = "System: Be brief\nUser: Hi\nAssistant: Hail\nUser: Bye\nAssistant: Farewell"
        self.assertEqual(td.add_trigger(piece, "Speak like Shakespeare."),
                         "System: Be brief\nUser: Speak like Shakespeare. Hi\nAssistant: Hail\nUser: Bye\nAssistant: Farewell")
        self.assertEqual(td.record_to_text(td.piece_to_record(piece)), piece)
        self.assertEqual(td.piece_to_record("Just a story.\nUser: not a turn"), {"text": "Just a story.\nUser: not a turn"})


class SharedRateTests(unittest.TestCase):
    OPTIONS = ["--batch-size", "8", "--learning-rate", "0.0004"]

    def feed(self, rate, losses, val, step):
        for i, loss in enumerate(losses, 1):
            rate.line("Step %d/50 | train loss %.4f | 2.00 steps/sec | 1,000 tokens/sec | total training step %d | "
                      "learning rate 0.0004 | gradient norm 0.5" % (i, loss, step - len(losses) + i))
        rate.line("Validation at step 50: loss %.4f | total training step %d" % (val, step))

    def test_stalled_rounds_lower_the_peak_for_the_next_round(self):
        rate, said = trainapi.RoundRate(self.OPTIONS), []
        self.feed(rate, [3.0] * 10, 4.0, 100)
        self.assertFalse(rate.end_round(said.append))
        cuts = []
        for rnd in range(2, 7):   # validation never beats 4.0: after `patience` stalled rounds the peak drops
            self.feed(rate, [3.0, 3.1] * 5, 4.1, 100 * rnd)
            cuts.append(rate.end_round(said.append))
        self.assertIn(True, cuts)
        self.assertLess(rate.peak, 0.0004)
        self.assertTrue(any(s.startswith("LEARNING_RATE | ") for s in said))

    def test_improving_rounds_keep_it(self):
        rate = trainapi.RoundRate(self.OPTIONS)
        for rnd in range(1, 10):
            self.feed(rate, [3.0] * 10, 5.0 - 0.1 * rnd, 100 * rnd)
            self.assertFalse(rate.end_round(lambda s: None))
        self.assertEqual(rate.peak, 0.0004)

    def test_a_round_without_a_check_changes_nothing(self):
        rate = trainapi.RoundRate(self.OPTIONS)
        self.assertFalse(rate.end_round(lambda s: None))
        rate.line("Validation at step 5: loss nan | total training step 5")
        self.assertFalse(rate.end_round(lambda s: None))
        self.assertEqual(rate.peak, 0.0004)


if __name__ == "__main__":
    unittest.main()
