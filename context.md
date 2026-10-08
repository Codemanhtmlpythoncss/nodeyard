# Project context (copy and paste)

Paste the box below into a new chat so the assistant knows where this project stands. It has no passwords, keys or private
addresses on purpose: this repository is public. Keep those in your own notes.

```text
PROJECT: nodeyard (github.com/Codemanhtmlpythoncss/nodeyard, MIT, version 0.1.0)
A Bash homelab-cluster manager (k3s on Raspberry Pis, mini PCs, old laptops) with a Python web dashboard, node agents, a model
gate, a llama.cpp model split across nodes, and a terminal AI agent called yardcode.

REPO LAYOUT
- bin/, lib/ (Bash modules, e.g. lib/modules/ai_split.sh), install.sh, uninstall.sh, completions/
- share/nodeyard/dashboard/ : Python stdlib dashboard (server.py, controlapi.py, agentapi.py, webtools.py, chatsapi.py,
  updateapi.py, aiapi.py, web/ai.js, web/app.js, web/style.css). Port 9092. Cookie sessions + CSRF; /api/v1 is the control API,
  protected by the model API key (KeyGate with lockout).
- share/nodeyard/gate/gate.py : model gate (keyless on trusted networks).
- yardcode/ : the terminal agent. Python 3.8+, standard library only, macOS + Linux.
  bin/yardcode, install.sh (POSIX sh; --prefix, --from-dir, --ref, --uninstall, --force, --yes), src/yardcode/*.py,
  src/yardcode/tools/*.py
- tests/: bats (tests/unit, 251 tests), Python (tests/dashboard 178, tests/yardcode 154). Run: make test, make test-py, make lint.
  Run bats detached with </dev/null (a stdin-reading test hangs otherwise).
- docs/, CHANGELOG.md ([Unreleased] has everything below), scripts/build-release.sh, scripts/build-yardcode.sh

WHAT EXISTS NOW
yardcode:
- OpenAI-compatible streaming client; native tool calls with a text fallback; permission modes (default, acceptEdits, plan,
  bypassPermissions) and allow/deny rules; project trust; sessions in JSONL with checkpoints (/rewind, /undo); context
  compression (/compact, automatic); hooks, MCP stdio client, plugins, sub-agents (Task), YARDCODE.md memory.
- Tools: Read, Write, Edit, MultiEdit, LS, Glob, Grep, Bash (+background), Python, Terminal (real pty with a screen emulator),
  WebSearch, WebFetch, Wikipedia, Arxiv, Weather, Calculator, FileSearch, Memory, TodoWrite, AskUserQuestion, Task, Model.
- Small, stable system prompt in tiers (tiny <12k context, lean <20k, full) so llama.cpp can reuse its prompt cache; the date and
  folder go in the first user message.
- Line editor (lineedit.py): type / and a menu of the 50+ commands opens above the input, filtering as you type; input is framed
  by two rules with a mode/model/context line below; @ completes files; history; multi-line; shift+tab cycles the mode.
- Shell mode (shellmode.py): ! on an empty prompt gives a real shell prompt ($SHELL, real terminal attached, cd and exports
  persist, Tab completes files). !cmd runs one line. /shell opens a full interactive shell. Output joins the conversation.
- /reset forgets the conversation (old one stays in /resume). /clear starts a new one. /think live streams reasoning.
- Web tools go through the nodeyard server (its internet, not the client's): POST /api/v1/web/tool; setting web.via
  auto|server|local. Weather picks the likeliest place and lists the others.
- Chats are shared with the dashboard AI tab (sync.py, /api/v1/chats); /chats continues a dashboard chat in the terminal.
- yardcode update (or /update): from the server (/api/v1/yardcode), GitHub, or --from-dir.
- Start-up probes the server for 4 seconds and warns instead of hanging when it is unreachable.
dashboard AI tab:
- No-limit reply length, context compression, / commands, plugins (web search, Wikipedia, arXiv, weather, calculator, tasks,
  Python, files, shell) with tool cards and permission prompts, run the AI's code (python/bash/node, 30 s) with "fix it" and
  auto-fix, replies become files only when asked, the selected model auto-loads on send, status pill updates in place.
control API (/api/v1, key protected): status, models, load, unload, download, search, files, jobs, web/tool, chats, yardcode.

CLUSTER FACTS
- k3s; one control node, several workers; the model (Qwen3-Coder 30B-A3B via llama.cpp RPC split) runs at roughly 2-4 tokens/s and
  reads prompts at roughly 15 tokens/s with an 8192 context, so the first reply of a conversation can take minutes.
- Workers have no SSH keys: they are updated with one-off helper pods that mount only /usr/local.
- The gate setting is kept in the config (ai.gate) and restored on deploy.

HOW TO DEPLOY AFTER A CHANGE (standing rule: whenever the dashboard/nodeyard is updated, update yardcode everywhere too)
1. git archive HEAD to the control node, then: sudo bash install.sh --from-dir . --force --yes
2. sudo nodeyard dashboard start --yes
3. Update the workers with the helper-pod script, then compare versions/checksums.
4. On the Mac: sh yardcode/install.sh --from-dir . --force (or: yardcode update --from-dir .), then restart yardcode.
Note: zsh does not word-split variables; use a shell function for ssh wrappers.

USER PREFERENCES
- Terse "caveman mode" replies. Finish things properly; speed was requested when tired of waiting.
- Do not change the Mac's network or system settings (a Tailscale exit node is toggled by the user, not by the assistant).
- Never enter credentials; push to GitHub only when asked.

KNOWN CAVEATS / NEXT IDEAS
- Not exercised live: switching models through the AI tab or API on the real cluster; plugin web search through the real model;
  !, /shell and /reset on the Mac (unit tests cover them).
- yardcode Terminal tool and shell mode untested on Linux/Python 3.8 in containers.
- A school web filter can break HTTPS to search sites from the client; the server-side web proxy avoids that.
- Old __pycache__ files remain in git history (now ignored).
- No dashboard screenshots in the docs yet. make lint (shellcheck/shfmt) was not re-run after the last changes.
```
