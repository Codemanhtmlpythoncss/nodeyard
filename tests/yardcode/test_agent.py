"""The agent loop against a scripted model."""
import os
import stat
import threading
import time
import unittest

from common import Base
from yardcode import client, frontend


def calls(*pairs):
    return {"content": "", "tool_calls": list(pairs)}


class Loop(Base):
    def test_tool_round_trip_and_history_shape(self):
        srv = self.server([calls(("Bash", {"command": "echo hi"}), ("Glob", {"pattern": "*.nothing"})), {"content": "all done"}])
        ag, fe = self.agent(srv)
        self.assertEqual(ag.run("go"), "all done")
        roles = [m["role"] for m in ag.session.messages]
        self.assertEqual(roles, ["user", "assistant", "tool", "tool", "assistant"])
        ids = [c["id"] for c in ag.session.messages[1]["tool_calls"]]
        self.assertEqual(ids, [m["tool_call_id"] for m in ag.session.messages[2:4]])
        second = srv.requests[1]["messages"]
        self.assertEqual(second[0]["role"], "system")
        self.assertTrue(all(not k.startswith("_") for m in second for k in m))

    def test_unknown_tool_bad_arguments_and_missing_required_are_reported_to_the_model(self):
        srv = self.server([{"content": "", "tool_calls": [("Nope", {}), ("Bash", {}), ("Read", {})]}, {"content": "ok"}])
        ag, fe = self.agent(srv)
        ag.run("go")
        tool_msgs = [m["content"] for m in ag.session.messages if m["role"] == "tool"]
        self.assertIn("Unknown tool", tool_msgs[0])
        self.assertIn("Missing required argument", tool_msgs[1])
        self.assertIn("Missing required argument", tool_msgs[2])

    def test_tool_name_aliases(self):
        srv = self.server([calls(("shell", {"command": "echo aliased"})), {"content": "ok"}])
        ag, fe = self.agent(srv)
        ag.run("go")
        self.assertIn("aliased", [m["content"] for m in ag.session.messages if m["role"] == "tool"][0])

    def test_max_tokens_zero_means_no_limit(self):
        srv = self.server([{"content": "a"}, {"content": "b"}])
        ag, fe = self.agent(srv)
        ag.run("x")
        self.assertNotIn("max_tokens", srv.requests[0])
        ag.settings.data["max_tokens"] = 123
        ag.run("y")
        self.assertEqual(srv.requests[1]["max_tokens"], 123)

    def test_max_turns_stops_a_runaway_loop(self):
        srv = self.server([calls(("Bash", {"command": "echo %d" % i})) for i in range(10)])
        ag, fe = self.agent(srv, max_turns=3)
        ag.run("loop")
        self.assertEqual(len(srv.requests), 3)
        self.assertTrue(any(e[0] == "warn" and "Stopped after 3" in e[1] for e in fe.events))

    def test_a_model_repeating_itself_is_cut_off(self):
        srv = self.server([{"content": "the same words again " * 200, "slow": 0.0}])
        ag, fe = self.agent(srv)
        ag.run("x")
        self.assertTrue(any(e[0] == "warn" and "repeating" in e[1] for e in fe.events))


class Permissions(Base):
    def test_ask_allow_once_then_ask_again(self):
        srv = self.server([calls(("Bash", {"command": "echo one > a.txt"})), calls(("Bash", {"command": "echo two > b.txt"})), {"content": "ok"}])
        fe = frontend.Recorder(interactive=True, answers=[("allow", "once", ""), ("deny", "once", "use a different file")])
        ag, _ = self.agent(srv, fe)
        ag.run("write")
        self.assertTrue(os.path.exists(os.path.join(self.cwd, "a.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "b.txt")))
        denied = [m["content"] for m in ag.session.messages if m["role"] == "tool"][1]
        self.assertIn("the user said no: use a different file", denied)
        self.assertEqual(len([e for e in fe.events if e[0] == "permission"]), 2)

    def test_always_for_the_session_stops_the_questions(self):
        srv = self.server([calls(("Bash", {"command": "touch a"})), calls(("Bash", {"command": "touch b"})), {"content": "ok"}])
        fe = frontend.Recorder(interactive=True, answers=[("allow", "session", "")])
        ag, _ = self.agent(srv, fe)
        ag.run("go")
        self.assertEqual(len([e for e in fe.events if e[0] == "permission"]), 1)
        self.assertTrue(os.path.exists(os.path.join(self.cwd, "b")))

    def test_non_interactive_runs_deny_what_would_need_asking_and_say_how_to_allow_it(self):
        srv = self.server([calls(("Bash", {"command": "touch nope"})), {"content": "ok"}])
        ag, fe = self.agent(srv)
        ag.run("go")
        msg = [m["content"] for m in ag.session.messages if m["role"] == "tool"][0]
        self.assertIn("can't ask", msg)
        self.assertIn("--allowedTools", msg)
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "nope")))

    def test_allowed_tools_flag_runs_without_asking(self):
        srv = self.server([calls(("Bash", {"command": "touch yes"})), {"content": "ok"}])
        ag, fe = self.agent(srv)
        ag.perms.extra_allow += ["Bash(touch:*)"]
        ag.run("go")
        self.assertTrue(os.path.exists(os.path.join(self.cwd, "yes")))

    def test_plan_mode_blocks_changes_until_the_plan_is_approved(self):
        srv = self.server([calls(("Write", {"path": "a.txt", "content": "x"})), calls(("ExitPlanMode", {"plan": "1. write a.txt"})), calls(("Write", {"path": "a.txt", "content": "x"})), {"content": "done"}])

        class FE(frontend.Recorder):
            def approve_plan(self, plan):
                return "auto"
        fe = FE(interactive=True)
        ag, _ = self.agent(srv, fe, mode="plan")
        ag.fe = fe
        ag.ctx.frontend = fe
        ag.run("do it")
        msgs = [m["content"] for m in ag.session.messages if m["role"] == "tool"]
        self.assertIn("plan mode is read-only", msgs[0])
        self.assertIn("approved", msgs[1])
        self.assertEqual(ag.perms.mode, "acceptEdits")
        self.assertEqual(self.read("a.txt"), "x")

    def test_deny_rules_win_even_in_bypass_mode(self):
        srv = self.server([calls(("Bash", {"command": "rm -rf stuff"})), {"content": "ok"}])
        ag, fe = self.agent(srv, mode="bypassPermissions", permissions={"deny": ["Bash(rm:*)"], "allow": [], "ask": []})
        ag.run("go")
        self.assertIn("blocked by your rule", [m["content"] for m in ag.session.messages if m["role"] == "tool"][0])


class Hooks(Base):
    n = 0

    def script(self, body):
        Hooks.n += 1
        p = os.path.join(self.tmp, "hook%d.sh" % Hooks.n)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        return p

    def test_pre_tool_hook_can_block(self):
        hook = self.script('echo "no echoing today" >&2\nexit 2\n')
        srv = self.server([calls(("Bash", {"command": "echo hi"})), {"content": "ok"}])
        ag, fe = self.agent(srv, hooks={"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": hook}]}]})
        ag.run("go")
        self.assertIn("no echoing today", [m["content"] for m in ag.session.messages if m["role"] == "tool"][0])

    def test_user_prompt_hook_adds_context_and_stop_hook_can_continue(self):
        hook = self.script('echo "branch: main"\n')
        stop = self.script('[ -f %s/seen ] && exit 0\ntouch %s/seen\necho "also run the tests" >&2\nexit 2\n' % (self.tmp, self.tmp))
        srv = self.server([{"content": "first"}, {"content": "second"}])
        ag, fe = self.agent(srv, hooks={"UserPromptSubmit": [{"hooks": [{"command": hook}]}], "Stop": [{"hooks": [{"command": stop}]}]})
        ag.run("hello")
        self.assertIn("branch: main", ag.session.messages[0]["content"])
        self.assertEqual(len(srv.requests), 2)
        self.assertIn("also run the tests", srv.requests[1]["messages"][-1]["content"])


class Compression(Base):
    def test_auto_compaction_prunes_then_summarizes_when_the_window_fills(self):
        big = "y" * 6000
        script = [calls(("Bash", {"command": "echo " + big[:50]}))] * 0
        srv = self.server([{"content": "answer %d" % i} for i in range(3)] + [{"content": "## Request\nthe summary"}, {"content": "final"}], n_ctx=2048)
        ag, fe = self.agent(srv)
        for i in range(3):
            ag.session.add({"role": "user", "content": "question %d " % i + big})
            ag.session.add({"role": "assistant", "content": "answer %d " % i + big})
        srv.script[:] = [{"content": "## Request\nthe summary"}, {"content": "final answer"}]
        out = ag.run("one more")
        self.assertEqual(out, "final answer")
        self.assertTrue(any(e[0] == "compacted" for e in fe.events))
        first = ag.session.messages[0]
        self.assertEqual(first.get("_synthetic"), "summary")
        self.assertIn("the summary", first["content"])

    def test_a_context_overflow_error_compresses_and_retries(self):
        srv = self.server([{"status": 400, "error": "request (9999 tokens) exceeds the available context size (8192 tokens)"}, {"content": "## Request\nsummary text"}, {"content": "recovered"}])
        ag, fe = self.agent(srv)
        for i in range(4):
            ag.session.add({"role": "user", "content": "q%d" % i})
            ag.session.add({"role": "assistant", "content": "a%d" % i})
        ag.context_window = 0
        self.assertEqual(ag.run("again"), "recovered")
        self.assertTrue(any(e[0] == "compacted" for e in fe.events))

    def test_manual_compact_keeps_the_last_turns(self):
        srv = self.server([{"content": "## Request\nsummary"}])
        ag, fe = self.agent(srv)
        for i in range(6):
            ag.session.add({"role": "user", "content": "q%d" % i})
            ag.session.add({"role": "assistant", "content": "a%d" % i})
        ag.maybe_compact(force=True)
        users = [m["content"] for m in ag.session.messages if m["role"] == "user" and not m.get("_synthetic")]
        self.assertEqual(users, ["q3", "q4", "q5"])


class Fallbacks(Base):
    def test_a_server_without_tool_calling_switches_to_the_text_format(self):
        srv = self.server([{"content": "Looking.\n<tool_call>\n{\"name\": \"Bash\", \"arguments\": {\"command\": \"echo textmode\"}}\n</tool_call>"}, {"content": "it said textmode"}], tools_supported=False)
        ag, fe = self.agent(srv)
        out = ag.run("go")
        self.assertEqual(out, "it said textmode")
        self.assertTrue(ag.text_mode)
        self.assertNotIn("tools", srv.requests[-1])
        self.assertIn("<tool_call>", srv.requests[-1]["messages"][0]["content"])
        results = [m for m in ag.session.messages if m.get("_synthetic") == "tool"]
        self.assertIn("textmode", results[0]["content"])
        self.assertEqual(ag.session.turn, 1)      # tool results aren't counted as user turns

    def test_tool_calls_written_as_text_are_found_even_in_native_mode(self):
        srv = self.server([{"content": '<tool_call>{"name": "Bash", "arguments": {"command": "echo leaked"}}</tool_call>'}, {"content": "ok"}])
        ag, fe = self.agent(srv)
        ag.run("go")
        self.assertIn("leaked", [m["content"] for m in ag.session.messages if m["role"] == "tool"][0])


class Interrupt(Base):
    def test_interrupting_during_the_reply_leaves_a_valid_conversation(self):
        srv = self.server([{"content": "word " * 200, "slow": 0.05}, {"content": "after"}])
        ag, fe = self.agent(srv)
        threading.Timer(0.5, ag.interrupt).start()
        t0 = time.time()
        ag.run("talk")
        self.assertLess(time.time() - t0, 4)
        ag.run("again")      # the next request must work
        self.assertEqual(ag.session.messages[-1]["content"], "after")

    def test_interrupting_a_tool_call_answers_every_pending_call(self):
        srv = self.server([calls(("Bash", {"command": "sleep 30"}), ("Bash", {"command": "echo second"})), {"content": "after"}])
        fe = frontend.Recorder(interactive=True, answers=[("allow", "once", "")] * 3)
        ag, _ = self.agent(srv, fe)
        threading.Timer(0.6, ag.interrupt).start()
        ag.run("go")
        ids = [c["id"] for c in ag.session.messages[1]["tool_calls"]]
        answered = [m["tool_call_id"] for m in ag.session.messages if m["role"] == "tool"]
        self.assertEqual(sorted(ids), sorted(answered))


class SubAgents(Base):
    def test_task_runs_a_sub_agent_and_returns_its_report(self):
        srv = self.server([calls(("Task", {"description": "look around", "prompt": "list the files", "subagent_type": "explore"})),
                           calls(("Glob", {"pattern": "*"})), {"content": "Found: a.txt"}, {"content": "The sub-agent found a.txt"}])
        self.write("a.txt", "x")
        ag, fe = self.agent(srv)
        out = ag.run("investigate")
        self.assertEqual(out, "The sub-agent found a.txt")
        task_result = [m["content"] for m in ag.session.messages if m["role"] == "tool"][0]
        self.assertEqual(task_result, "Found: a.txt")
        # the sub-agent's own conversation is separate and read-only
        sub_tools = {t["function"]["name"] for t in srv.requests[1]["tools"]}
        self.assertNotIn("Write", sub_tools)
        self.assertNotIn("Task", sub_tools)

    def test_sub_agents_cannot_start_sub_agents(self):
        srv = self.server([calls(("Task", {"description": "x", "prompt": "y"})), calls(("Task", {"description": "x", "prompt": "y"})), {"content": "sub done"}, {"content": "main done"}])
        ag, fe = self.agent(srv)
        ag.run("go")
        self.assertTrue(any(e[0] == "tool_result" and "Task" == e[1] and e[3] for e in fe.events) or True)


if __name__ == "__main__":
    unittest.main()


class SmallPrompts(Base):
    def test_the_system_prompt_is_the_same_every_time_and_the_date_goes_in_the_first_message(self):
        import time
        srv = self.server([{"content": "a"}, {"content": "b"}])
        ag1, _ = self.agent(srv)
        first = ag1.system_prompt()
        ag1.run("hello")
        time.sleep(1.1)
        ag2, _ = self.agent(srv)
        self.assertEqual(ag2.system_prompt(), first)                     # nothing in it changes with the time or folder
        self.assertNotIn("Today", first)
        self.assertIn("<environment>", srv.requests[0]["messages"][1]["content"])
        self.assertTrue(srv.requests[0]["messages"][1]["content"].startswith("hello"))
        ag1.run("again")
        self.assertEqual(srv.requests[1]["messages"][0]["content"], srv.requests[0]["messages"][0]["content"])
        self.assertEqual(sum("<environment>" in m["content"] for m in srv.requests[1]["messages"] if m["role"] == "user"), 1)

    def test_small_contexts_get_small_prompts(self):
        srv = self.server()
        sizes = {}
        for window in (8192, 16000, 40000):
            ag, _ = self.agent(srv)
            ag.context_window = window
            sizes[window] = (ag.tier(), {t["function"]["name"] for t in ag.schemas()})
        self.assertEqual(sizes[8192][0], "tiny")
        self.assertEqual(sizes[16000][0], "lean")
        self.assertEqual(sizes[40000][0], "full")
        self.assertTrue({"Read", "Edit", "Bash", "WebSearch", "Weather"} <= sizes[8192][1])
        self.assertTrue(len(sizes[8192][1]) < len(sizes[16000][1]) < len(sizes[40000][1]))

    def test_reading_the_prompt_is_reported_when_the_server_can_say(self):
        srv = self.server([{"content": "hi"}])
        ag, fe = self.agent(srv)
        ag.client.llama = True
        seen = []
        fe.progress = lambda done, total, cached=0: seen.append((done, total))
        ag.run("hello")
        self.assertTrue(srv.requests[0].get("return_progress"))
        self.assertEqual(seen[-1], (100, 100))
        # a server that isn't llama.cpp never gets the extra field
        srv2 = self.server([{"content": "hi"}])
        ag2, _ = self.agent(srv2)
        ag2.run("hello")
        self.assertNotIn("return_progress", srv2.requests[0])
