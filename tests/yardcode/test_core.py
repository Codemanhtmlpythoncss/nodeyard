"""The model client, settings, sessions, compression and the terminal drawing."""
import json
import os
import threading
import time
import unittest

from common import Base
from screen import render
from yardcode import client, compact, config, session, ui, util


class Splitter(unittest.TestCase):
    def collect(self, chunks):
        sp = client.ThinkSplitter()
        out = []
        for c in chunks:
            out += sp.feed(c)
        out += sp.flush()
        think = "".join(t for k, t in out if k)
        text = "".join(t for k, t in out if not k)
        return think, text

    def test_think_tags_even_when_cut_in_half(self):
        self.assertEqual(self.collect(["<think>plan</think>answer"]), ("plan", "answer"))
        self.assertEqual(self.collect(["ab<th", "ink>pl", "an</thi", "nk>cd"]), ("plan", "abcd"))
        self.assertEqual(self.collect(["no tags < here"]), ("", "no tags < here"))

    def test_text_tool_calls_json_and_qwen_xml_and_fences(self):
        known = {"Bash", "Read"}
        text, calls = client.parse_text_tool_calls('Sure.\n<tool_call>\n{"name": "Bash", "arguments": {"command": "ls"}}\n</tool_call>', known)
        self.assertEqual(text, "Sure.")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"command": "ls"})
        text, calls = client.parse_text_tool_calls("<tool_call>\n<function=Read>\n<parameter=path>\na.py\n</parameter>\n<parameter=limit>\n20\n</parameter>\n</function>\n</tool_call>", known)
        self.assertEqual(calls[0]["function"]["name"], "Read")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "a.py", "limit": 20})
        _, calls = client.parse_text_tool_calls('```json\n{"name": "Bash", "arguments": {"command": "pwd"}}\n```', known)
        self.assertEqual(len(calls), 1)
        _, calls = client.parse_text_tool_calls('<tool_call>{"name": "Nope", "arguments": {}}</tool_call>', known)
        self.assertEqual(calls, [])


class ClientStreaming(Base):
    def test_streams_text_thinking_and_assembles_tool_calls(self):
        srv = self.server([{"content": "Hello there, friend.", "thinking": "hmm", "tool_calls": [("Bash", {"command": "echo " + "x" * 40})]}])
        c = client.Client(srv.url, "k")
        got, think = [], []
        comp = c.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "Bash"}}], on_text=got.append, on_thinking=think.append)
        self.assertEqual("".join(got), "Hello there, friend.")
        self.assertEqual("".join(think), "hmm")
        self.assertEqual(comp.tool_calls[0]["function"]["name"], "Bash")
        self.assertEqual(json.loads(comp.tool_calls[0]["function"]["arguments"])["command"], "echo " + "x" * 40)
        self.assertEqual(comp.finish, "tool_calls")
        self.assertAlmostEqual(comp.tokens_per_second, 12.5)
        self.assertGreater(comp.prompt_tokens, 0)

    def test_max_tokens_is_only_sent_when_set(self):
        srv = self.server()
        c = client.Client(srv.url)
        c.chat([{"role": "user", "content": "a"}], max_tokens=0)
        c.chat([{"role": "user", "content": "b"}], max_tokens=300)
        self.assertNotIn("max_tokens", srv.requests[0])
        self.assertEqual(srv.requests[1]["max_tokens"], 300)

    def test_errors_are_classified(self):
        srv = self.server(key="secret")
        with self.assertRaises(client.APIError) as e:
            client.Client(srv.url, "wrong").chat([{"role": "user", "content": "a"}])
        self.assertEqual(e.exception.kind, "auth")
        srv2 = self.server([{"status": 400, "error": "request (9000 tokens) exceeds the available context size (8192 tokens)"}])
        with self.assertRaises(client.APIError) as e:
            client.Client(srv2.url).chat([{"role": "user", "content": "a"}])
        self.assertEqual(e.exception.kind, "context")
        srv3 = self.server([{"status": 503, "error": "Loading model"}])
        with self.assertRaises(client.APIError) as e:
            client.Client(srv3.url).chat([{"role": "user", "content": "a"}])
        self.assertEqual(e.exception.kind, "loading")
        with self.assertRaises(client.APIError) as e:
            client.Client("http://127.0.0.1:1/v1", connect_timeout=1).chat([{"role": "user", "content": "a"}])
        self.assertEqual(e.exception.kind, "connect")

    def test_native_tools_unsupported_is_reported(self):
        srv = self.server(tools_supported=False)
        with self.assertRaises(client.APIError) as e:
            client.Client(srv.url).chat([{"role": "user", "content": "a"}], tools=[{"type": "function", "function": {"name": "x"}}])
        self.assertEqual(e.exception.kind, "tools")

    def test_discovery_and_health(self):
        srv = self.server(n_ctx=12345)
        c = client.Client(srv.url)
        self.assertEqual(c.models(), ["fake-model"])
        self.assertEqual(c.context_window(), 12345)
        self.assertEqual(c.health(), "ok")
        self.assertEqual(client.Client("http://127.0.0.1:1/v1", connect_timeout=1).health(), "down")

    def test_abort_stops_a_slow_reply(self):
        srv = self.server([{"content": "x" * 400, "slow": 0.05}])
        c = client.Client(srv.url)
        stop = threading.Event()
        threading.Timer(0.4, lambda: (stop.set(), c.abort())).start()
        t0 = time.time()
        with self.assertRaises(client.Cancelled):
            c.chat([{"role": "user", "content": "a"}], stop=stop)
        self.assertLess(time.time() - t0, 3)


class Settings(Base):
    def test_layers_env_and_overrides(self):
        os.makedirs(self.home)
        util.write_json(os.path.join(self.home, "settings.json"), {"model": "user-model", "temperature": 0.5, "permissions": {"allow": ["Bash(ls:*)"]}})
        os.makedirs(os.path.join(self.cwd, ".yardcode"))
        util.write_json(os.path.join(self.cwd, ".yardcode", "settings.json"), {"temperature": 0.9, "permissions": {"deny": ["Bash(rm:*)"]}})
        s = config.Settings(self.cwd, overrides={"model": "cli-model"}, environ={"YARDCODE_API_BASE": "http://h:1/v1", "YARDCODE_API_KEY": "k"})
        self.assertEqual(s.get("model"), "cli-model")
        self.assertEqual(s.get("temperature"), 0.9)
        self.assertEqual(s.api_base, "http://h:1/v1")
        self.assertEqual(s.get("api_key"), "k")
        self.assertEqual(s.get("permissions")["allow"], ["Bash(ls:*)"])
        self.assertEqual(s.get("permissions")["deny"], ["Bash(rm:*)"])
        self.assertEqual(s.control_url(), "http://h:9092")

    def test_the_key_is_stored_private_and_never_shown(self):
        s = self.settings()
        s.save_key("sk-1234567890abcdef")
        self.assertEqual(oct(os.stat(s.cred_path).st_mode & 0o777), "0o600")
        self.assertNotIn("1234567890", json.dumps(s.dump()))
        self.assertEqual(self.settings().get("api_key"), "sk-1234567890abcdef")
        s.save_key("")
        self.assertEqual(self.settings().get("api_key"), "")


class Sessions(Base):
    def test_messages_persist_and_reload(self):
        s = session.Session(self.cwd)
        s.add({"role": "user", "content": "hello world"})
        s.add({"role": "assistant", "content": "hi"})
        again = session.Session(self.cwd, s.id)
        self.assertEqual([m["role"] for m in again.messages], ["user", "assistant"])
        self.assertEqual(again.title, "hello world")
        self.assertEqual(again.turn, 1)
        self.assertEqual(session.Session.list(self.cwd)[0]["id"], s.id)
        self.assertTrue(all(not k.startswith("_") for m in again.api_messages() for k in m))

    def test_rewind_restores_files_and_drops_messages(self):
        p = self.write("f.txt", "v1\n")
        s = session.Session(self.cwd)
        s.add({"role": "user", "content": "first"})
        s.checkpoint_file(p)
        with open(p, "w") as f:
            f.write("v2\n")
        s.add({"role": "assistant", "content": "changed it"})
        s.add({"role": "user", "content": "second"})
        new = os.path.join(self.cwd, "new.txt")
        s.checkpoint_file(new)
        with open(new, "w") as f:
            f.write("x")
        restored, dropped = s.rewind(1)
        self.assertEqual(self.read("f.txt"), "v1\n")
        self.assertFalse(os.path.exists(new))
        self.assertEqual(dropped, 3)
        self.assertEqual(s.messages, [])
        self.assertEqual(session.Session(self.cwd, s.id).messages, [])    # the rewind is saved too

    def test_a_reset_replaces_everything_on_reload(self):
        s = session.Session(self.cwd)
        for i in range(4):
            s.add({"role": "user", "content": "m%d" % i})
        s.reset(s.messages[-1:])
        self.assertEqual(len(session.Session(self.cwd, s.id).messages), 1)


def conv(n_turns, tool_chars=3000):
    msgs = []
    for i in range(n_turns):
        msgs.append({"role": "user", "content": "question %d" % i})
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": "c%d" % i, "type": "function", "function": {"name": "Read", "arguments": '{"path":"f%d"}' % i}}]})
        msgs.append({"role": "tool", "tool_call_id": "c%d" % i, "content": "data %d " % i + "x" * tool_chars})
        msgs.append({"role": "assistant", "content": "answer %d" % i})
    return msgs


class Compression(Base):
    def test_prune_shortens_only_old_big_tool_output(self):
        msgs = conv(8)
        new, saved = compact.prune(msgs, keep_turns=2, max_chars=500)
        self.assertGreater(saved, 10000)
        self.assertIn("trimmed", new[2]["content"])
        self.assertEqual(new[-2]["content"], msgs[-2]["content"])     # the recent turns are untouched
        self.assertLess(compact.estimate(new), compact.estimate(msgs) / 2)

    def test_compact_replaces_the_old_part_with_a_summary_and_keeps_recent_turns(self):
        srv = self.server([{"content": "## Request\nUser asked 8 questions about files."}])
        c = client.Client(srv.url)
        msgs = conv(8)
        new, summary, before, after = compact.compact(c, msgs, 32768, keep_turns=2)
        self.assertIn("8 questions", new[0]["content"])
        self.assertEqual(new[0]["_synthetic"], "summary")
        self.assertEqual(new[1]["role"], "assistant")
        self.assertEqual([m["content"] for m in new if m["role"] == "user" and not m.get("_synthetic")], ["question 6", "question 7"])
        self.assertLess(after, before)
        sent = srv.requests[0]["messages"]
        self.assertIn("question 0", sent[1]["content"])                # the summarizer saw the old conversation
        self.assertNotIn("question 7", sent[1]["content"])

    def test_a_history_bigger_than_the_window_is_summarized_in_pieces(self):
        srv = self.server([{"content": "part %d" % i} for i in range(1, 20)])
        c = client.Client(srv.url)
        compact.summarize(c, conv(40, 6000), 4096)
        self.assertGreater(len(srv.requests), 2)
        self.assertIn("Summary so far", srv.requests[1]["messages"][1]["content"])

    def test_nothing_to_compress_is_an_error(self):
        srv = self.server()
        with self.assertRaises(ValueError):
            compact.compact(client.Client(srv.url), [{"role": "user", "content": "hi"}], 8192)


class Drawing(unittest.TestCase):
    def setUp(self):
        self.S = ui.Style(False)

    def test_markdown_plain(self):
        out = ui.render_markdown("# Title\n\nSome **bold** and `code`.\n\n- a\n- b\n\n1. one\n2. two\n\n> quote\n", self.S, 60)
        self.assertIn("Title", out)
        self.assertIn("Some bold and `code`.", out)
        self.assertRegex(out, r"[-•] a")
        self.assertIn("1. one", out)
        self.assertRegex(out, r"[|▎] quote")

    def test_table_and_code_block(self):
        out = ui.render_markdown("| a | b |\n|---|---|\n| 1 | two |\n\n```py\nx = 1\n```\n", self.S, 60)
        self.assertIn("two", out)
        self.assertIn("x = 1", out)
        self.assertNotIn("```", out)

    def test_wrap_keeps_colours_across_lines(self):
        S = ui.Style(True, "dark", True)
        lines = ui.wrap_ansi(S.bold("word " * 20), 30)
        self.assertGreater(len(lines), 2)
        for l in lines[1:]:
            self.assertTrue(l.startswith("\x1b[1m"))

    def test_diff_counts(self):
        lines, adds, dels = ui.render_diff("a\nb\nc\n", "a\nB\nc\nd\n", self.S)
        self.assertEqual((adds, dels), (2, 1))
        self.assertTrue(any("- b" in l for l in lines))

    def test_wide_characters_and_vlen(self):
        self.assertEqual(ui.vlen("\x1b[1mab\x1b[0m"), 2)
        self.assertEqual(ui.vlen("日本"), 4)

    def test_live_markdown_leaves_only_the_styled_lines_on_screen(self):
        from yardcode import tui
        import io

        class TTY(io.StringIO):
            def isatty(self):
                return True
        out = TTY()
        old_width = ui.terminal_width
        ui.terminal_width = lambda default=100: 40
        self.addCleanup(setattr, ui, "terminal_width", old_width)
        S = ui.Style(True, "dark", True)
        lm = tui.LiveMarkdown(S, out)
        lm.md.width = 40
        text = "This is a long first line of streamed text that will wrap around the narrow screen for sure.\n- bullet **one**\n```py\nx = 1\n```\nlast words"
        for i in range(0, len(text), 5):
            lm.feed(text[i:i + 5])
        lm.finish()
        screen = render(out.getvalue(), 40)
        self.assertNotIn("**", screen)
        self.assertNotIn("```", screen)
        self.assertIn("• bullet one", screen)
        self.assertIn("x = 1", screen)
        self.assertTrue(screen.rstrip().endswith("last words"))
        self.assertEqual(screen.count("This is a long first line"), 1)


if __name__ == "__main__":
    unittest.main()


class ThinkingDisplay(unittest.TestCase):
    def tui(self, mode):
        import io
        from yardcode import tui
        out = io.StringIO()
        s = type("S", (), {"get": lambda self, k, d=None: mode if k == "thinking" else d})()
        return tui.TUI(s, S=ui.Style(False), out=out), out

    def test_live_mode_streams_the_reasoning_as_it_arrives(self):
        t, out = self.tui("live")
        t.on_thinking("Let me think. ")
        self.assertIn("Let me think.", out.getvalue())             # already on screen, before the answer
        t.on_thinking("Second\nline")
        t.end_text()
        self.assertIn("    line", out.getvalue())
        t.end_turn("done")
        self.assertNotIn("No reasoning came", out.getvalue())

    def test_a_model_that_never_thinks_gets_one_hint_in_live_mode(self):
        t, out = self.tui("live")
        t.end_turn("x")
        t.end_turn("y")
        self.assertEqual(out.getvalue().count("No reasoning came"), 1)

    def test_show_mode_summarizes_after_and_hide_shows_nothing(self):
        t, out = self.tui("show")
        t.on_thinking("a\nb")
        self.assertEqual(out.getvalue(), "")                        # nothing until it is over
        t.end_text()
        self.assertIn("thinking (2 lines)", out.getvalue())
        t2, out2 = self.tui("hide")
        t2.on_thinking("secret")
        t2.end_text()
        self.assertNotIn("secret", out2.getvalue())
