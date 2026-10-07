"""The prompt editor, driven through a pseudo-terminal exactly as a person's keyboard would."""
import os
import pty
import select
import struct
import sys
import tempfile
import termios
import time
import fcntl
import unittest

from common import HERE, REPO
from screen import render

PROGRAM = r'''
import sys
sys.path.insert(0, %(src)r)
from yardcode import ui
from yardcode.lineedit import LineEditor
cmds = [("clear", "Start a new conversation"), ("compact", "Compress the conversation"), ("config", "Show or change settings"), ("context", "How full the context is"),
        ("help", "Show every command"), ("model", "Pick a model")]
ed = LineEditor(ui.Style(False), sys.stdout, %(hist)r, commands=lambda: cmds)
while True:
    try:
        t = ed.read("> ")
    except EOFError:
        print("EOF"); break
    except KeyboardInterrupt:
        print("INT"); break
    print("GOT:" + (repr(t) if len(t) < 40 else repr(t[:12]) + ".." + repr(t[-12:]) + " len=" + str(len(t))), flush=True)
'''


class Term:
    def __init__(self, hist, cols=60):
        src = os.path.join(REPO, "yardcode", "src")
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.execv(sys.executable, [sys.executable, "-c", PROGRAM % {"src": src, "hist": hist}])
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, cols, 0, 0))
        self.cols = cols
        self.buf = b""

    def pump(self, t=0.25):
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    d = os.read(self.fd, 65536)
                except OSError:
                    return
                if not d:
                    return
                self.buf += d

    def send(self, s, wait=0.25):
        os.write(self.fd, s.encode())
        self.pump(wait)

    def screen(self):
        return render(self.buf, self.cols)

    def close(self):
        try:
            os.kill(self.pid, 9)
            os.waitpid(self.pid, 0)
        except OSError:
            pass


class Editor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.t = Term(os.path.join(self.tmp, "hist.jsonl"))
        self.t.pump(0.6)

    def tearDown(self):
        self.t.close()

    def got(self):
        return [l[4:] for l in self.t.screen().split("\n") if l.startswith("GOT:")]

    def test_typing_and_enter(self):
        self.t.send("hello world\r")
        self.assertEqual(self.got(), ["'hello world'"])

    @staticmethod
    def input_line(scr):
        """The line with the prompt on it (the input sits between two rules, the menu above it)."""
        rows = [r.rstrip() for r in scr.split("\n") if r.startswith("> ")]
        return rows[-1] if rows else ""

    def test_the_input_is_framed_with_the_menu_above_it(self):
        self.t.send("/co")
        rows = [r.rstrip() for r in self.t.screen().split("\n") if r.strip()]
        i = [k for k, r in enumerate(rows) if r.startswith("> /co")][-1]
        self.assertTrue(rows[i - 1].startswith("---") or rows[i - 1].startswith("───"))     # a rule above the input
        self.assertTrue(rows[i + 1].startswith("---") or rows[i + 1].startswith("───"))     # and one below
        self.assertTrue(any("/compact" in r for r in rows[:i - 1]))                         # the menu is above the rule

    def test_bang_on_an_empty_prompt_is_shell_mode(self):
        self.t.send("!")
        scr = self.t.screen()
        self.assertIn("shell mode", scr)
        self.assertIn("shell $", scr)
        self.t.send("ls -l\r")
        self.assertEqual(self.got(), ["'!ls -l'"])
        self.t.send("\x7f")                               # backspace on an empty line leaves shell mode
        self.assertNotIn("shell mode", self.t.screen())
        self.t.send("hi\r")
        self.assertEqual(self.got()[-1], "'hi'")

    def test_slash_opens_a_menu_that_filters_and_completes(self):
        self.t.send("/")
        scr = self.t.screen()
        for name in ("/clear", "/compact", "/help", "/model"):
            self.assertIn(name, scr)
        self.t.send("co")
        scr = self.t.screen()
        self.assertIn("/compact", scr)
        self.assertIn("/context", scr)
        self.assertNotIn("/help", scr.split("> /co")[-1])
        self.t.send("\t")                                 # Tab takes the highlighted one
        scr = self.t.screen()
        self.assertEqual(self.input_line(scr), "> /compact")       # completed, and the menu is closed
        self.assertNotIn("/context", scr.split("> /co")[-1])
        self.t.send("\r")
        self.assertEqual(self.got(), ["'/compact '"])

    def test_arrow_keys_choose_and_enter_completes_before_running(self):
        self.t.send("/co")
        self.t.send("\x1b[B")                             # down: /compact -> /config
        self.t.send("\x1b[B")                             # down again: /context
        self.t.send("\r")                                 # completes it, doesn't run yet
        self.assertEqual(self.got(), [])
        self.assertEqual(self.input_line(self.t.screen()), "> /context")
        self.t.send("\r")
        self.assertEqual(self.got(), ["'/context '"])

    def test_an_exact_command_runs_on_enter(self):
        self.t.send("/help\r")
        self.assertEqual(self.got(), ["'/help'"])

    def test_editing_keys(self):
        self.t.send("abcdef")
        self.t.send("\x1b[D\x1b[D")                       # two lefts
        self.t.send("X")
        self.t.send("\x7f")                               # backspace removes X
        self.t.send("\x01")                               # ctrl-a
        self.t.send(">")
        self.t.send("\x05")                               # ctrl-e
        self.t.send("!")
        self.t.send("\r")
        self.assertEqual(self.got(), ["'>abcdef!'"])

    def test_word_and_line_kills(self):
        self.t.send("one two three")
        self.t.send("\x17")                               # ctrl-w: deletes "three"
        self.t.send("\r")
        self.assertEqual(self.got(), ["'one two '"])
        self.t.send("keep this")
        self.t.send("\x15")                               # ctrl-u: clears the line
        self.t.send("new\r")
        self.assertEqual(self.got()[-1], "'new'")

    def test_history_with_up_and_down(self):
        self.t.send("first\r")
        self.t.send("second\r")
        self.t.send("\x1b[A")
        self.t.send("\x1b[A")
        self.assertEqual(self.input_line(self.t.screen()), "> first")
        self.t.send("\x1b[B")
        self.t.send("\r")
        self.assertEqual(self.got()[-1], "'second'")

    def test_history_is_saved_for_next_time(self):
        self.t.send("remember me\r")
        self.t.close()
        t2 = Term(os.path.join(self.tmp, "hist.jsonl"))
        t2.pump(0.6)
        t2.send("\x1b[A")
        self.assertIn("> remember me", t2.screen())
        t2.close()

    def test_a_trailing_backslash_continues_on_a_new_line(self):
        self.t.send("line one\\\r")
        self.t.send("line two\r")
        self.assertEqual(self.got(), ["'line one\\nline two'"])

    def test_pasted_text_arrives_whole_with_its_newlines(self):
        self.t.send("\x1b[200~first\nsecond\nthird\x1b[201~")
        self.assertEqual(self.got(), [])                  # a paste never submits by itself
        self.t.send("\r")
        self.assertEqual(self.got(), ["'first\\nsecond\\nthird'"])

    def test_ctrl_c_clears_then_leaves_and_ctrl_d_leaves(self):
        self.t.send("some text")
        self.t.send("\x03")
        self.assertNotIn("some text", self.t.screen().split("\n")[-1])
        self.t.send("\x03")
        self.t.pump(0.3)
        self.assertIn("INT", self.t.screen())

    def test_ctrl_d_on_an_empty_line_is_end_of_input(self):
        self.t.send("\x04")
        self.t.pump(0.3)
        self.assertIn("EOF", self.t.screen())

    def test_shift_tab_asks_for_the_mode_change(self):
        self.t.send("\x1b[Z")
        self.assertEqual(self.got(), ["'/mode'"])

    def test_long_lines_wrap_and_stay_editable(self):
        text = "word " * 30
        self.t.send(text)
        self.t.send("\x01")
        self.t.send("START ")
        self.t.send("\r")
        want = "START " + text
        self.assertEqual(self.got(), ["%s..%s len=%d" % (repr(want[:12]), repr(want[-12:]), len(want))])

    def test_unicode_is_typed_whole(self):
        self.t.send("café 日本\r")
        self.assertEqual(self.got(), [repr("café 日本")])

    def test_an_unknown_slash_text_is_not_swallowed(self):
        self.t.send("/zzz")
        self.t.send("\r")
        self.assertEqual(self.got(), ["'/zzz'"])


if __name__ == "__main__":
    unittest.main()
