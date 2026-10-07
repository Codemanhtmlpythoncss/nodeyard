"""The file, shell, python, calculator and notes tools."""
import os
import sys
import time
import unittest

from common import Base
from yardcode.tools import files, misc, shell
from yardcode.tools.base import ToolError, parse_args


class FileTools(Base):
    def test_read_numbers_lines_and_pages_big_files(self):
        self.write("a.txt", "".join("line %d\n" % i for i in range(1, 51)))
        ctx = self.ctx()
        r = files.Read().run({"path": "a.txt", "offset": 5, "limit": 3}, ctx)
        self.assertIn("     5\tline 5", r.text)
        self.assertIn("use offset=8 to continue", r.text)
        self.assertNotIn("line 9\n", r.text)

    def test_read_missing_file_suggests_a_similar_name(self):
        self.write("config.json", "{}")
        with self.assertRaises(ToolError) as e:
            files.Read().run({"path": "confg.json"}, self.ctx())
        self.assertIn("config.json", str(e.exception))

    def test_read_refuses_binary(self):
        with open(os.path.join(self.cwd, "b.bin"), "wb") as f:
            f.write(b"\x00\x01\x02" * 100)
        with self.assertRaises(ToolError):
            files.Read().run({"path": "b.bin"}, self.ctx())

    def test_edit_needs_a_read_first_and_a_unique_match(self):
        self.write("x.py", "a = 1\nb = 2\nb = 2\n")
        ctx = self.ctx()
        with self.assertRaises(ToolError) as e:
            files.Edit().run({"path": "x.py", "old_string": "a = 1", "new_string": "a = 5"}, ctx)
        self.assertIn("Read", str(e.exception))
        files.Read().run({"path": "x.py"}, ctx)
        with self.assertRaises(ToolError) as e:
            files.Edit().run({"path": "x.py", "old_string": "b = 2", "new_string": "b = 3"}, ctx)
        self.assertIn("2 times", str(e.exception))
        r = files.Edit().run({"path": "x.py", "old_string": "b = 2", "new_string": "b = 3", "replace_all": True}, ctx)
        self.assertEqual(self.read("x.py"), "a = 1\nb = 3\nb = 3\n")
        self.assertIn("2 replacements", r.text)
        self.assertEqual(r.diff[1:], (2, 2))

    def test_edit_forgives_indentation_and_trailing_space_when_unique(self):
        self.write("y.py", "def f():\n    x = 1  \n    return x\n")
        ctx = self.ctx()
        files.Read().run({"path": "y.py"}, ctx)
        files.Edit().run({"path": "y.py", "old_string": "  x = 1\n  return x", "new_string": "  x = 2\n  return x"}, ctx)
        self.assertEqual(self.read("y.py"), "def f():\n    x = 2\n    return x\n")

    def test_edit_not_found_gives_closest_lines(self):
        self.write("z.py", "value = compute(10)\n")
        ctx = self.ctx()
        files.Read().run({"path": "z.py"}, ctx)
        with self.assertRaises(ToolError) as e:
            files.Edit().run({"path": "z.py", "old_string": "value = compute(11)", "new_string": "x"}, ctx)
        self.assertIn("compute(10)", str(e.exception))

    def test_edit_detects_a_file_changed_since_it_was_read(self):
        p = self.write("w.txt", "one\n")
        ctx = self.ctx()
        files.Read().run({"path": "w.txt"}, ctx)
        time.sleep(0.02)
        with open(p, "w") as f:
            f.write("two\n")
        os.utime(p, (time.time() + 5, time.time() + 5))
        with self.assertRaises(ToolError) as e:
            files.Edit().run({"path": "w.txt", "old_string": "two", "new_string": "3"}, ctx)
        self.assertIn("changed since", str(e.exception))

    def test_edit_keeps_crlf(self):
        p = os.path.join(self.cwd, "c.txt")
        with open(p, "wb") as f:
            f.write(b"a\r\nb\r\n")
        ctx = self.ctx()
        files.Read().run({"path": "c.txt"}, ctx)
        files.Edit().run({"path": "c.txt", "old_string": "b", "new_string": "B"}, ctx)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"a\r\nB\r\n")

    def test_multi_edit_is_all_or_nothing(self):
        self.write("m.txt", "alpha\nbeta\n")
        ctx = self.ctx()
        files.Read().run({"path": "m.txt"}, ctx)
        with self.assertRaises(ToolError):
            files.MultiEdit().run({"path": "m.txt", "edits": [{"old_string": "alpha", "new_string": "A"}, {"old_string": "nope", "new_string": "x"}]}, ctx)
        self.assertEqual(self.read("m.txt"), "alpha\nbeta\n")
        files.MultiEdit().run({"path": "m.txt", "edits": [{"old_string": "alpha", "new_string": "A"}, {"old_string": "beta", "new_string": "B"}]}, ctx)
        self.assertEqual(self.read("m.txt"), "A\nB\n")

    def test_write_creates_folders_and_checkpoints_the_old_file(self):
        saved = []
        ctx = self.ctx()
        ctx.checkpoint = saved.append
        files.Write().run({"path": "new/dir/f.txt", "content": "hi\n"}, ctx)
        self.assertEqual(self.read("new/dir/f.txt"), "hi\n")
        files.Read().run({"path": "new/dir/f.txt"}, ctx)
        files.Write().run({"path": "new/dir/f.txt", "content": "bye\n"}, ctx)
        self.assertEqual(len(saved), 2)

    def test_glob_and_grep_and_ls(self):
        self.write("src/a.py", "import os\nprint('x')\n")
        self.write("src/b.js", "console.log('x')\n")
        self.write("node_modules/skip.py", "import os\n")
        ctx = self.ctx()
        g = files.Glob().run({"pattern": "**/*.py"}, ctx)
        self.assertIn("src/a.py", g.text)
        self.assertNotIn("node_modules", g.text)
        r = files.Grep().run({"pattern": "import", "output_mode": "content"}, ctx)
        self.assertIn("src/a.py:1:import os", r.text)
        self.assertNotIn("node_modules", r.text)
        self.assertIn("src/b.js", files.Grep().run({"pattern": "console", "output_mode": "files_with_matches"}, ctx).text)
        self.assertIn("src/a.py:2", files.Grep().run({"pattern": "print", "glob": "*.py", "-C": 0}, ctx).text)
        ls = files.LS().run({}, ctx)
        self.assertIn("- src/", ls.text)
        self.assertNotIn("node_modules", ls.text)

    def test_grep_context_and_bad_regex(self):
        self.write("g.txt", "one\ntwo\nthree\nfour\n")
        ctx = self.ctx()
        r = files.Grep().run({"pattern": "three", "-C": 1}, ctx)
        self.assertIn("g.txt-2-two", r.text)
        self.assertIn("g.txt:3:three", r.text)
        with self.assertRaises(ToolError):
            files.Grep().run({"pattern": "("}, ctx)


class ShellTools(Base):
    def test_bash_runs_and_keeps_the_directory_between_calls(self):
        os.makedirs(os.path.join(self.cwd, "sub"))
        ctx = self.ctx()
        b = shell.Bash()
        r = b.run({"command": "cd sub && pwd"}, ctx)
        self.assertIn("sub", r.text)
        self.assertIn("working directory is now", r.text)
        r2 = b.run({"command": "pwd"}, ctx)
        self.assertTrue(r2.text.splitlines()[0].endswith("sub"))

    def test_bash_reports_exit_codes_and_stderr(self):
        r = shell.Bash().run({"command": "echo out; echo err >&2; exit 3"}, self.ctx())
        self.assertTrue(r.error)
        self.assertIn("out", r.text)
        self.assertIn("err", r.text)
        self.assertIn("[exit code 3]", r.text)

    def test_bash_times_out_and_stops_the_process(self):
        t0 = time.time()
        r = shell.Bash().run({"command": "sleep 30", "timeout": 1}, self.ctx())
        self.assertLess(time.time() - t0, 6)
        self.assertIn("ran longer than 1 s", r.text)

    def test_bash_cannot_read_the_keyboard(self):
        r = shell.Bash().run({"command": "read x; echo got=[$x]"}, self.ctx())
        self.assertIn("got=[]", r.text)

    def test_background_shell_output_and_kill(self):
        ctx = self.ctx()
        r = shell.Bash().run({"command": "for i in 1 2 3; do echo tick$i; sleep 0.2; done; sleep 30", "run_in_background": True}, ctx)
        self.assertIn("bash_1", r.text)
        time.sleep(1.2)
        out = shell.BashOutput().run({"bash_id": "bash_1"}, ctx)
        self.assertIn("tick3", out.text)
        self.assertIn("still running", out.text)
        shell.KillShell().run({"shell_id": "bash_1"}, ctx)
        self.assertNotIn("bash_1", ctx.shells)

    def test_python_keeps_state_prints_the_last_expression_and_reports_errors(self):
        ctx = self.ctx()
        p = shell.Python()
        try:
            self.assertEqual(p.run({"code": "x = 21"}, ctx).text, "(no output)")
            self.assertEqual(p.run({"code": "x * 2"}, ctx).text, "42")
            self.assertIn("hello", p.run({"code": "print('hello')"}, ctx).text)
            bad = p.run({"code": "1/0"}, ctx)
            self.assertTrue(bad.error)
            self.assertIn("ZeroDivisionError", bad.text)
            self.assertEqual(p.run({"code": "x"}, ctx).text, "21")  # still alive after the error
        finally:
            if ctx.python:
                ctx.python.stop()

    def test_python_timeout_restarts_the_interpreter(self):
        ctx = self.ctx()
        p = shell.Python()
        try:
            r = p.run({"code": "import time; time.sleep(30)", "timeout": 1}, ctx)
            self.assertTrue(r.error)
            self.assertIn("restarted", r.text)
            self.assertEqual(p.run({"code": "2+2"}, ctx).text, "4")
        finally:
            if ctx.python:
                ctx.python.stop()


class SmallTools(Base):
    def test_calculator(self):
        c = misc.Calculator()
        ctx = self.ctx()
        self.assertIn("2708", c.run({"expression": "0b1010100101 << 2"}, ctx).text)
        self.assertIn("1.414213562373", c.run({"expression": "sqrt(2)"}, ctx).text)
        self.assertEqual(c.run({"expression": "10 // 3"}, ctx).summary, "3")
        with self.assertRaises(ToolError):
            c.run({"expression": "__import__('os').system('echo hi')"}, ctx)
        with self.assertRaises(ToolError):
            c.run({"expression": "9**9**9"}, ctx)
        with self.assertRaises(ToolError):
            c.run({"expression": "1/0"}, ctx)

    def test_todos_are_stored_and_counted(self):
        ctx = self.ctx()
        r = misc.TodoWrite().run({"todos": [{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"}, {"content": "c", "status": "bogus"}]}, ctx)
        self.assertIn("1 of 3", r.text)
        self.assertEqual(ctx.todos[2]["status"], "pending")

    def test_file_search_ranks_the_best_passage_first(self):
        self.write("docs/a.md", "# Networking\nThe cluster uses wireguard tunnels over Tailscale for every node.\n" + "filler\n" * 60)
        self.write("docs/b.md", "Cooking pasta takes ten minutes.\n" * 5)
        r = misc.FileSearch().run({"query": "how does tailscale networking work"}, self.ctx())
        self.assertTrue(r.text.startswith("docs/a.md"))
        self.assertNotIn("docs/b.md", r.text)

    def test_memory_add_list_remove(self):
        ctx = self.ctx()
        m = misc.Memory()
        m.run({"action": "add", "text": "Use tabs in this repo", "scope": "project"}, ctx)
        self.assertIn("Use tabs", self.read("YARDCODE.md"))
        self.assertIn("Use tabs", m.run({"action": "list"}, ctx).text)
        m.run({"action": "remove", "text": "tabs", "scope": "project"}, ctx)
        self.assertNotIn("Use tabs", self.read("YARDCODE.md"))

    def test_parse_args_repairs_common_model_slips(self):
        self.assertEqual(parse_args('{"a": 1,}'), {"a": 1})
        self.assertEqual(parse_args('{"a": "x"'), {"a": "x"})
        self.assertEqual(parse_args(""), {})
        with self.assertRaises(ToolError):
            parse_args("not json at all")


if __name__ == "__main__":
    unittest.main()
_ = sys
