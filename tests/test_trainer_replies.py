import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "share" / "nodeyard" / "jobs"))
try:
    import torch  # noqa: F401
    import train_bpe_gpt_torch as T
    from bpe_tokenizer import BPETokenizer
except ImportError:   # the trainer needs PyTorch; the dashboard's own tests don't
    T = None


@unittest.skipIf(T is None, "PyTorch isn't installed here")
class ReplyTests(unittest.TestCase):
    def reply(self, text):
        c = T.clean_reply(text)
        cut = T.end_of_reply(c)
        return (c[:cut] if cut is not None else c).strip()

    def test_role_markers_never_end_up_in_a_reply(self):
        self.assertEqual(self.reply(" WhichAssistant: I will, and the fold"), "Which")
        self.assertEqual(self.reply("Assistant: Hello there!\n\nUser: hi"), "Hello there!")
        self.assertEqual(self.reply(" I will go.\nUser: no"), "I will go.")
        self.assertEqual(self.reply(" fine, thanks"), "fine, thanks")

    def test_markers_are_read_but_not_graded_and_tokens_do_not_change(self):
        text = "User: What news?\nAssistant: None, my lord.\n\n" * 40
        tok = BPETokenizer.train(text, 300)
        ids, weights = T.encode_with_roles(tok, text, True)
        whole = [i for line in text.splitlines(keepends=True) for i in tok.encode(line)]
        self.assertEqual(list(ids), whole)
        user = len(tok.encode("User: What news?\n"))
        marker = len(tok.encode("Assistant:"))
        self.assertEqual(set(weights[:user + marker].tolist()), {0.0})
        self.assertEqual(set(weights[user + marker:user + len(tok.encode("Assistant: None, my lord.\n"))].tolist()), {1.0})


if __name__ == "__main__":
    unittest.main()
