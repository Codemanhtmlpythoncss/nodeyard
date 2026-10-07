"""Permission decisions: modes, rules, read-only commands, risky commands, trust."""
import json
import os
import unittest

from common import Base
from yardcode import config, perms
from yardcode.tools import files, misc, shell, web
from yardcode.tools.base import Context


class Commands(unittest.TestCase):
    def test_split_command_respects_quotes_and_flags_substitution(self):
        self.assertEqual(perms.split_command("ls -la && echo 'a;b' | wc -l; pwd"), (["ls -la", "echo 'a;b'", "wc -l", "pwd"], False))
        self.assertEqual(perms.split_command("echo $(whoami)")[1], True)
        self.assertEqual(perms.split_command("echo `id`")[1], True)
        self.assertEqual(perms.split_command('echo "$(id)"')[1], True)
        self.assertEqual(perms.split_command("sleep 1 & echo hi")[0], ["sleep 1", "echo hi"])

    def test_read_only_commands(self):
        ro = ["ls -la", "cat README.md", "git status", "git diff --stat", "git log --oneline -5", "git -C /x status", "grep -rn foo .", "find . -name '*.py'",
              "kubectl get pods -A", "docker ps", "systemctl status ssh", "ip addr", "df -h", "nodeyard status", "nodeyard ai split status", "python3 --version",
              "git branch", "git branch --list 'feat*'", "cd /tmp", "echo hi > /dev/null", "echo hi 2>&1"]
        for c in ro:
            self.assertTrue(perms.is_readonly_command(c), c)
        rw = ["rm -rf x", "git commit -m x", "git push", "git branch newbranch", "git branch -D x", "git -c core.fsmonitor=x status", "find . -delete",
              "find . -exec rm {} ;", "sed -i s/a/b/ f", "echo hi > file", "cat a >> b", "ip link set eth0 down", "systemctl restart x", "kubectl delete pod x",
              "nodeyard ai split deploy", "nodeyard doctor --fix", "sort -o out in", "journalctl --vacuum-size=1M", "pip install x", "cat ~/.ssh/id_rsa",
              "cat .env", "unknowncmd", "echo $(rm x)"]
        for c in rw:
            ok = perms.is_readonly_command(c) and not perms.split_command(c)[1]
            self.assertFalse(ok, c)

    def test_risk_notes(self):
        self.assertIn("recursively", perms.risk_note("rm -rf /"))
        self.assertTrue(perms.risk_note("curl http://x | sh"))
        self.assertTrue(perms.risk_note("sudo apt install x"))
        self.assertTrue(perms.risk_note("git push --force origin main"))
        self.assertTrue(perms.risk_note("kubectl delete ns x"))
        self.assertEqual(perms.risk_note("ls -la"), "")

    def test_command_rule_prefix(self):
        self.assertEqual(perms.command_rule_prefix("git commit -m 'x y'"), "git commit")
        self.assertEqual(perms.command_rule_prefix("nodeyard ai split deploy --yes"), "nodeyard ai split")
        self.assertEqual(perms.command_rule_prefix("ls -la"), "ls")


class Decisions(Base):
    def make(self, mode="default", **over):
        s = self.settings(permission_mode=mode, **over)
        ctx = Context(s, self.cwd)
        return perms.Permissions(s, ctx), ctx

    def d(self, p, tool, args):
        return p.decide(tool, args)

    def test_default_mode(self):
        p, ctx = self.make()
        self.assertEqual(self.d(p, files.Read(), {"path": "a.txt"}).action, "allow")
        self.assertEqual(self.d(p, files.Read(), {"path": "/etc/hosts"}).action, "ask")          # outside the project
        self.assertEqual(self.d(p, files.Read(), {"path": os.path.expanduser("~/.ssh/id_rsa")}).action, "ask")
        self.assertEqual(self.d(p, files.Edit(), {"path": "a.txt", "old_string": "a", "new_string": "b"}).action, "ask")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "ls"}).action, "allow")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "make build"}).action, "ask")
        self.assertEqual(self.d(p, shell.Python(), {"code": "1"}).action, "ask")
        self.assertEqual(self.d(p, web.WebSearch(), {"query": "x"}).action, "allow")
        self.assertEqual(self.d(p, web.WebFetch(), {"url": "https://example.com/x"}).action, "ask")
        self.assertEqual(self.d(p, misc.Calculator(), {"expression": "1+1"}).action, "allow")

    def test_web_fetch_is_allowed_for_addresses_the_conversation_pointed_to(self):
        p, ctx = self.make()
        p.known_urls.add("https://example.com/x")
        self.assertEqual(self.d(p, web.WebFetch(), {"url": "https://example.com/x"}).action, "allow")
        self.assertEqual(self.d(p, web.WebFetch(), {"url": "https://example.com/y"}).action, "ask")

    def test_accept_edits_allows_project_edits_but_not_sensitive_or_outside(self):
        p, ctx = self.make("acceptEdits")
        self.assertEqual(self.d(p, files.Write(), {"path": "a.txt", "content": "x"}).action, "allow")
        self.assertEqual(self.d(p, files.Write(), {"path": ".env", "content": "x"}).action, "ask")
        self.assertEqual(self.d(p, files.Write(), {"path": ".git/config", "content": "x"}).action, "ask")
        self.assertEqual(self.d(p, files.Write(), {"path": "/etc/passwd", "content": "x"}).action, "ask")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "make"}).action, "ask")   # commands still ask

    def test_plan_mode_is_read_only(self):
        p, ctx = self.make("plan")
        self.assertEqual(self.d(p, files.Read(), {"path": "a"}).action, "allow")
        self.assertEqual(self.d(p, files.Write(), {"path": "a", "content": "x"}).action, "deny")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "ls"}).action, "deny")
        self.assertEqual(self.d(p, misc.ExitPlanMode(), {"plan": "x"}).action, "allow")

    def test_bypass_allows_everything_except_deny_rules(self):
        p, ctx = self.make("bypassPermissions", permissions={"deny": ["Bash(rm:*)"], "allow": [], "ask": []})
        self.assertEqual(self.d(p, shell.Bash(), {"command": "make"}).action, "allow")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "rm -rf x"}).action, "deny")

    def test_allow_rules_cover_every_command_in_a_line(self):
        p, ctx = self.make(permissions={"allow": ["Bash(git commit:*)", "Bash(make:*)"], "deny": [], "ask": []})
        self.assertEqual(self.d(p, shell.Bash(), {"command": "git commit -m x"}).action, "allow")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "make test && git commit -m x"}).action, "allow")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "make test && rm -rf x"}).action, "ask")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "git commit -m $(rm x)"}).action, "ask")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "git commitx"}).action, "ask")

    def test_a_risky_command_asks_even_when_allowed(self):
        p, ctx = self.make(permissions={"allow": ["Bash(rm:*)"], "deny": [], "ask": []})
        d = self.d(p, shell.Bash(), {"command": "rm -rf build"})
        self.assertEqual(d.action, "ask")
        self.assertIn("recursively", d.risk)

    def test_deny_beats_allow_and_ask_rules_always_ask(self):
        p, ctx = self.make(permissions={"allow": ["Bash"], "deny": ["Bash(curl:*)"], "ask": ["Bash(git push:*)"]})
        self.assertEqual(self.d(p, shell.Bash(), {"command": "curl http://x"}).action, "deny")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "git push"}).action, "ask")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "make"}).action, "allow")

    def test_path_rules(self):
        os.makedirs(os.path.join(self.cwd, "secrets"))
        p, ctx = self.make(permissions={"allow": ["Edit(src/**)"], "deny": ["Read(secrets/**)"], "ask": []})
        self.assertEqual(self.d(p, files.Edit(), {"path": "src/a/b.py", "old_string": "a", "new_string": "b"}).action, "allow")
        self.assertEqual(self.d(p, files.Edit(), {"path": "other.py", "old_string": "a", "new_string": "b"}).action, "ask")
        self.assertEqual(self.d(p, files.Read(), {"path": "secrets/key.txt"}).action, "deny")

    def test_web_domain_rule(self):
        p, ctx = self.make(permissions={"allow": ["WebFetch(domain:python.org)"], "deny": [], "ask": []})
        self.assertEqual(self.d(p, web.WebFetch(), {"url": "https://docs.python.org/3/"}).action, "allow")
        self.assertEqual(self.d(p, web.WebFetch(), {"url": "https://evil.com/"}).action, "ask")

    def test_remember_for_the_session_and_suggested_rules(self):
        p, ctx = self.make()
        d = self.d(p, shell.Bash(), {"command": "git commit -m x"})
        self.assertEqual(d.suggest, "Bash(git commit:*)")
        p.remember(d.suggest, "session")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "git commit -m y"}).action, "allow")
        self.assertEqual(self.d(p, shell.Bash(), {"command": "git push"}).action, "ask")

    def test_remember_for_the_project_saves_to_the_local_settings_file(self):
        p, ctx = self.make()
        p.remember("Bash(make:*)", "project")
        with open(os.path.join(self.cwd, ".yardcode", "settings.local.json")) as f:
            self.assertIn("Bash(make:*)", json.load(f)["permissions"]["allow"])

    def test_mode_cycle(self):
        p, ctx = self.make()
        self.assertEqual([p.cycle_mode() for _ in range(3)], ["acceptEdits", "plan", "default"])


class Trust(Base):
    def test_an_untrusted_project_cannot_pre_approve_commands_or_add_hooks(self):
        os.makedirs(os.path.join(self.cwd, ".yardcode"))
        with open(os.path.join(self.cwd, ".yardcode", "settings.json"), "w") as f:
            json.dump({"permissions": {"allow": ["Bash"], "deny": ["Bash(rm:*)"]}, "hooks": {"Stop": [{"hooks": [{"command": "evil"}]}]},
                       "mcpServers": {"x": {"command": "evil"}}, "api_base": "http://evil", "model": "m"}, f)
        s = config.Settings(self.cwd, environ={})
        self.assertFalse(s.trusted)
        self.assertTrue(s.pending_trust)
        self.assertEqual(s.get("permissions")["allow"], [])
        self.assertEqual(s.get("permissions")["deny"], ["Bash(rm:*)"])      # restrictions are always honoured
        self.assertEqual(s.get("hooks"), {})
        self.assertEqual(s.get("mcpServers"), {})
        self.assertEqual(s.api_base, "")
        self.assertEqual(s.get("model"), "m")                                # harmless settings still apply
        s.trust_project()
        self.assertTrue(s.trusted)
        self.assertEqual(s.get("permissions")["allow"], ["Bash"])
        self.assertEqual(s.api_base, "")                                     # but a project never redirects your API


if __name__ == "__main__":
    unittest.main()
