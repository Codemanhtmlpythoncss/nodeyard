# yardcode

A coding agent for your terminal that works with **your own model API**: it reads and edits files, runs commands, searches the web and
keeps a task list, asking before it changes anything. It talks to any OpenAI-compatible server (llama.cpp, Ollama, vLLM, LM Studio,
OpenAI), and is made for the model API of a [nodeyard](../README.md) cluster, which it can also load models on.

Plain Python 3.8+ (standard library only). Runs on **macOS and Linux**.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/Codemanhtmlpythoncss/nodeyard/main/yardcode/install.sh | sh
```

No sudo: it installs for you in `~/.local` (as root: `/usr/local`). Or copy the single-file build (`make yardcode` makes `dist/yardcode`)
anywhere on your PATH. nodeyard's own installer installs it too. Remove it: `sh install.sh --uninstall`.

```sh
yardcode login            # the model API address and the nodeyard server API key
yardcode                  # start working in this folder
yardcode "explain this project"
git diff | yardcode -p "review this"      # one answer, then exit
```

## What it can do

| | |
|---|---|
| **Tools** | Read, Write, Edit, MultiEdit, LS, Glob, Grep, Bash (background shells too), Python (a notebook-style interpreter that keeps state), TodoWrite, Task (sub-agents), AskUserQuestion, plan mode |
| **Web** | WebSearch (no API key: DuckDuckGo/Bing, or your SearXNG/Brave/Tavily), WebFetch (pages to readable text), Wikipedia, Arxiv, Weather |
| **Also** | Calculator, FileSearch (ranked search over a folder of documents), Memory (`#note`), Model (switch the cluster's model) |
| **Safety** | asks before edits, commands and unknown web pages; allow/deny rules; read-only commands never ask; risky commands always ask; project settings are untrusted until you run `/trust` |
| **Long chats** | automatic **context compression**: old tool output is trimmed, then the model summarizes the earlier conversation (`/compact`, `/context`) |
| **Control** | `--max-tokens none` (no reply limit), plan / accept-edits / bypass modes (shift+tab), `/rewind` and `/undo` put files back, esc interrupts, `--show-thinking` or `/think live` to watch the model's reasoning as it writes it |
| **Extend** | plugins (a Python file or a JSON file), MCP servers, hooks, custom `/commands`, custom sub-agents, `YARDCODE.md` instructions (`AGENTS.md` and `CLAUDE.md` work too) |
| **Cluster** | `/models`, `/model`, `/load`, `/unload`, `/download`, `/search`: manage models on your nodeyard cluster with one server API key |

Type `/help` for every command. `@file` includes a file, `!cmd` runs a command yourself, `#note` remembers something.

## Load models remotely

The nodeyard dashboard has a key-protected control API (`/api/v1`, using the server API key shared with nodeyard models), so this works from any machine that can
reach the dashboard (default: the API's host, port 9092; set another with `yardcode config control_url http://host:9092`):

If the key already works with a running split model but the dashboard rejects
it, use **Settings > Server API key > Use the running model's key**. This adopts
the current model key as the server key without restarting the model.

```sh
yardcode models                       # what is downloaded and what is loaded
yardcode models search qwen coder     # find GGUF models
yardcode models download Qwen/Qwen2.5-Coder-7B-Instruct-GGUF
yardcode models load qwen2.5-coder    # switch the cluster to it and wait until it answers
yardcode models unload                # free the cluster's memory
```

Or with curl: `curl -H "Authorization: Bearer $KEY" http://DASHBOARD:9092/api/v1/models` (see [the dashboard docs](../docs/dashboard.md#control-api)).

## Settings

`~/.config/yardcode/settings.json` (yours), `.yardcode/settings.json` (shared with a project), `.yardcode/settings.local.json` (yours,
this project). The API key lives in `~/.config/yardcode/credentials.json` (mode 600). Change things with `/config key value`.

```json
{
  "permissions": {"allow": ["Bash(git status:*)", "Edit(src/**)"], "deny": ["Bash(rm:*)"]},
  "max_tokens": 0, "context_window": 16384, "compact": {"auto": true, "threshold": 0.8},
  "search": {"engine": "auto", "searxng_url": ""},
  "hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "ruff format ."}]}]},
  "mcpServers": {"files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}}
}
```

Small models: with a context under 20k tokens the rarely used tools stay out of the prompt (`tools.profile`: `auto`, `full` or `lean`;
`/tools off Arxiv` switches one off). Servers that can't do tool calls get a text format automatically.

## Plugins

`/plugins new` writes an example into `~/.config/yardcode/plugins/`. A plugin is a Python file with `TOOLS = [{"name", "description",
"parameters", "run": lambda args, ctx: "text"}]`, or a JSON file naming a command to run (arguments arrive as JSON on standard input).
Project plugins (`.yardcode/plugins`) only load after `/trust`.

## For programs

`yardcode -p "..." --output-format json|stream-json`, and `yardcode --serve-json` (JSON lines on stdin and stdout: text, tool calls,
permission requests...). See `src/yardcode/serve.py` for the protocol.

## Research

`/research <topic>` (add `quick` or `deep`) runs the nodeyard server's Research Mode when yardcode is connected to a
dashboard: it plans searches, reads pages from the server, writes a report citing only what it read, and checks every
citation. The report and its sources are added to the chat so you can ask about them. Ctrl-C cancels it on the server.
`/research local <topic>` (or no dashboard) asks the agent to research with its own web tools instead.

