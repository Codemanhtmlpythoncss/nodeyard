"""Command line: yardcode [options] [prompt]  |  yardcode login | models | config | doctor | sessions | mcp"""
import argparse
import json
import os
import shlex
import sys

from . import __version__, util
from .client import APIError
from .config import Settings

USAGE_EXAMPLES = """examples:
  yardcode                                  start a conversation here
  yardcode "explain this project"           start with a question
  yardcode -p "list the TODOs in src/"      one answer, then exit (also reads standard input)
  git diff | yardcode -p "review this"      pipe something in
  yardcode -c                               continue the last conversation in this folder
  yardcode login                            set the model API address and key
  yardcode models                           see and load the models on your cluster
"""


def split_rules(text):
    """"Bash(git:*) Edit,Read" -> ["Bash(git:*)", "Edit", "Read"] (spaces inside parentheses stay)."""
    out, cur, depth = [], "", 0
    for ch in text or "":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch in " ,\n" and depth == 0:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def build_parser():
    p = argparse.ArgumentParser(prog="yardcode", description="A coding agent for your terminal, powered by your own model API.", epilog=USAGE_EXAMPLES,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("prompt", nargs="*", help="what to do (starts the conversation with it)")
    p.add_argument("-p", "--print", dest="print_mode", action="store_true", help="answer once, print it, and exit (no questions are asked)")
    p.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the most recent conversation in this folder")
    p.add_argument("-r", "--resume", nargs="?", const="?", metavar="ID", help="resume a conversation (pick from a list without ID)")
    p.add_argument("--model", help="model name to send with requests")
    p.add_argument("--api-base", help="the OpenAI-compatible API address, e.g. http://host:31435/v1")
    p.add_argument("--api-key", help="API key (better: YARDCODE_API_KEY or yardcode login)")
    p.add_argument("--control-url", help="the nodeyard dashboard address, for loading models (default: API host, port 9092)")
    p.add_argument("--context", type=int, help="the model's context size in tokens (default: asked from the server)")
    p.add_argument("--max-tokens", help="longest reply in tokens, or 'none' for no limit")
    p.add_argument("--max-turns", type=int, help="stop after this many model calls in one request")
    p.add_argument("--permission-mode", choices=["default", "acceptEdits", "plan", "bypassPermissions"], help="how much it asks first")
    p.add_argument("--dangerously-skip-permissions", action="store_true", help="never ask (same as --permission-mode bypassPermissions)")
    p.add_argument("--allowedTools", "--allowed-tools", dest="allowed", help='rules that need no asking, e.g. "Bash(git status:*) Edit"')
    p.add_argument("--disallowedTools", "--disallowed-tools", dest="disallowed", help="rules that are always refused")
    p.add_argument("--tools", help="only these tools, comma separated (e.g. Read,Grep,Glob)")
    p.add_argument("--add-dir", action="append", default=[], help="another folder it may work in (repeatable)")
    p.add_argument("--system-prompt", help="replace the built-in instructions")
    p.add_argument("--append-system-prompt", help="add to the built-in instructions")
    p.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text", help="with -p: how to print the result")
    p.add_argument("--theme", choices=["auto", "dark", "light", "none"], help="colours")
    p.add_argument("--no-color", action="store_true", help="plain text, no colours")
    p.add_argument("--no-session", action="store_true", help="don't save this conversation")
    p.add_argument("--cwd", help="work in this folder")
    p.add_argument("--serve-json", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("-V", "--version", action="store_true", help="show the version")
    return p


def sub_parser():
    p = argparse.ArgumentParser(prog="yardcode", add_help=False)
    p.add_argument("command")
    p.add_argument("rest", nargs="*")
    return p


def overrides_from(a):
    o = {}
    for key, val in (("model", a.model), ("api_base", a.api_base), ("control_url", a.control_url), ("context_window", a.context), ("max_turns", a.max_turns),
                     ("theme", "none" if a.no_color else a.theme), ("permission_mode", "bypassPermissions" if a.dangerously_skip_permissions else a.permission_mode),
                     ("api_key", a.api_key)):
        if val is not None:
            o[key] = val
    if a.max_tokens is not None:
        o["max_tokens"] = 0 if str(a.max_tokens).lower() in ("none", "no", "0", "unlimited") else int(a.max_tokens)
    if a.add_dir:
        o["additional_dirs"] = a.add_dir
    if a.system_prompt:
        o["system_prompt_extra"] = a.system_prompt
    if a.append_system_prompt:
        o["system_prompt_extra"] = (o.get("system_prompt_extra", "") + "\n" + a.append_system_prompt).strip()
    if a.tools:
        from .tools import GROUPS
        want = {t.strip() for t in a.tools.split(",") if t.strip()}
        o["tools"] = {"disabled": sorted({n for _, g in GROUPS for n in g} - want)}
    return o


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("login", "logout", "models", "config", "doctor", "sessions", "mcp", "version") and not any(x in argv[:1] for x in ("-p",)):
        return run_subcommand(argv[0], argv[1:])
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.version:
        print("yardcode %s" % __version__)
        return 0
    if a.cwd:
        try:
            os.chdir(a.cwd)
        except OSError as e:
            print("yardcode: can't go to %s: %s" % (a.cwd, e), file=sys.stderr)
            return 2
    settings = Settings(os.getcwd(), overrides=overrides_from(a))
    from .app import App
    app = App(settings, persist=not a.no_session)
    prompt = " ".join(a.prompt).strip()

    if a.serve_json:
        from . import serve
        window = app.discover()
        serve.serve(app, window)
        return 0

    if a.print_mode or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return run_print(app, a, prompt)

    return run_interactive(app, a, prompt)


def apply_rules(agent, a):
    if a.allowed:
        agent.perms.extra_allow += split_rules(a.allowed)
    if a.disallowed:
        agent.perms.extra_deny += split_rules(a.disallowed)


def run_interactive(app, a, prompt):
    from . import hooks
    from .session import Session
    S = app.tui.style
    st = app.settings
    if not st.api_base:
        app.tui.w(S.bold("\n  Welcome to yardcode."))
        app.tui.w(S.muted("  First, tell me where your model API is. For a nodeyard cluster that's its Tailscale or LAN address, e.g. 100.82.189.124.\n"))
        app.new_agent()
        from . import slash
        slash.c_login(app, "")
    window = app.discover()
    session = None
    if a.cont:
        rows = Session.list(os.getcwd(), 2)
        if rows:
            session = Session(os.getcwd(), rows[0]["id"], True)
    elif a.resume:
        found = Session.find(a.resume, os.getcwd()) if a.resume != "?" else None
        if found:
            session = Session(os.getcwd(), found["id"], True)
    app.new_agent(session, window)
    apply_rules(app.agent, a)
    if a.resume == "?" and not session:
        from . import slash
        slash.c_resume(app, "")
    app.banner()
    r = hooks.run_hooks(st, "SessionStart", {}, app.agent.ctx.cwd, session_id=app.session.id)
    for w in r.warnings:
        app.tui.warn(w)
    if r.context:
        app.session.add({"role": "user", "content": "\n".join(r.context), "_synthetic": "hook"})
    try:
        if prompt:
            app.send(prompt)
        app.repl()
    except (BrokenPipeError, KeyboardInterrupt):
        app.shutdown()
    return 0


def read_piped_input(have_prompt):
    """Text piped into yardcode (git diff | yardcode -p "review"). With a prompt given, an input that never says anything (an
    inherited pipe in cron or CI) is ignored after a few seconds instead of hanging."""
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        import select
        if have_prompt and not select.select([sys.stdin], [], [], 3.0)[0]:
            return ""
        return sys.stdin.read().strip()
    except (OSError, ValueError):
        return ""


def run_print(app, a, prompt):
    """-p: no screen, no questions. Output is the answer (text), one JSON object (json) or events (stream-json)."""
    from . import hooks, serve
    from .frontend import Frontend
    st = app.settings
    piped = read_piped_input(bool(prompt))
    if piped:
        prompt = (prompt + "\n\n" + piped).strip() if prompt else piped
    if not prompt:
        print("yardcode: nothing to do. Give a prompt: yardcode -p \"what to do\"", file=sys.stderr)
        return 2
    if not st.api_base:
        print("yardcode: no model API address. Run: yardcode login   (or set YARDCODE_API_BASE)", file=sys.stderr)
        return 2
    window = app.discover()
    fmt = a.output_format
    if fmt == "stream-json":
        emit = serve.make_emitter()
        fe = serve.JsonFrontend(emit, interactive=False)
    else:
        class Quiet(Frontend):
            interactive = False
            errors = []

            def error(self, text):
                self.errors.append(text)
                print("yardcode: " + text, file=sys.stderr)

            def warn(self, text):
                print("yardcode: " + text, file=sys.stderr)
        fe = Quiet()
    app.tui = fe
    ag = app.new_agent(window=window)
    ag.fe = fe
    ag.ctx.frontend = fe
    if hasattr(fe, "agent"):
        fe.agent = ag
    ag.perms.interactive = False
    apply_rules(ag, a)
    hooks.run_hooks(st, "SessionStart", {}, ag.ctx.cwd, session_id=app.session.id)
    app.ensure_ready()
    final = ag.run(prompt)
    failed = bool(getattr(fe, "errors", []))
    if fmt == "json":
        u = app.session.usage
        print(json.dumps({"type": "result", "is_error": failed, "result": final, "session_id": app.session.id, "model": app.client.model,
                          "num_requests": u["requests"], "usage": {"prompt_tokens": u["prompt"], "completion_tokens": u["completion"]}}, ensure_ascii=False))
    elif fmt == "text":
        print(final)
    app.shutdown()
    return 1 if failed else 0


def run_subcommand(name, rest):
    settings = Settings(os.getcwd())
    from .app import App
    app = App(settings, persist=False)
    app.new_agent()
    from . import slash
    if name == "version":
        print("yardcode %s" % __version__)
    elif name == "login":
        slash.c_login(app, " ".join(rest))
    elif name == "logout":
        slash.c_logout(app, "")
    elif name == "config":
        slash.c_config(app, " ".join(rest))
    elif name == "doctor":
        app.agent.context_window = app.discover() or app.agent.context_window
        slash.c_doctor(app, "")
    elif name == "sessions":
        slash.c_sessions(app, "")
    elif name == "mcp":
        app.mcp_servers = []
        slash.c_mcp(app, " ".join(rest))
    elif name == "models":
        app.agent.context_window = app.discover() or app.agent.context_window
        sub = rest[0] if rest else "list"
        arg = " ".join(rest[1:])
        try:
            if sub == "list":
                slash.show_models(app)
            elif sub == "load":
                slash.load_model(app, arg, ask=sys.stdin.isatty())
            elif sub == "unload":
                slash.c_unload(app, arg)
            elif sub == "search":
                slash.c_search(app, arg)
            elif sub == "download":
                slash.c_download(app, arg)
            elif sub == "status":
                print(json.dumps(app.modelapi.status(), indent=2))
            else:
                print("Usage: yardcode models [list|load NAME|unload|search WORDS|download REPO [FILE]|status]")
                return 2
        except (APIError, Exception) as e:
            print("yardcode: %s" % e, file=sys.stderr)
            return 1
    return 0


_ = (shlex, util)
