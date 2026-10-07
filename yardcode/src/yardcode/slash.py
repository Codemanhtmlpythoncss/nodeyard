"""The /commands. Each handler gets the App and the text after the command; returning a string sends it to the model as a prompt."""
import difflib
import json
import os
import platform
import shutil
import subprocess
import sys
import time

from . import __version__, compact, memory, ui, util
from .client import APIError
from .modelapi import ModelAPIError
from .perms import MODE_LABEL, MODES
from .session import Session

REGISTRY = {}
ALIASES = {}


def cmd(name, help, group="Session", aliases=(), hint=""):
    def deco(fn):
        REGISTRY[name] = (fn, help, group, hint)
        for a in aliases:
            ALIASES[a] = name
        return fn
    return deco


def names(app):
    out = set(REGISTRY) | set(ALIASES) | set(app.load_custom_commands())
    return sorted(out)


def dispatch(app, name, arg):
    name = ALIASES.get(name, name)
    custom = app.load_custom_commands()
    if name in REGISTRY:
        res = REGISTRY[name][0](app, arg)
        if res is False:
            return False
        if isinstance(res, str) and res.strip():
            app.send(res)
        return True
    if name in custom:
        prompt = custom[name]["prompt"]
        prompt = prompt.replace("$ARGUMENTS", arg)
        for i, a in enumerate(arg.split(), 1):
            prompt = prompt.replace("$%d" % i, a)
        app.send(prompt)
        return True
    guess = difflib.get_close_matches(name, list(REGISTRY) + list(custom), n=3, cutoff=0.5)
    app.tui.warn("Unknown command /%s.%s Try /help." % (name, (" Did you mean /" + ", /".join(guess) + "?") if guess else ""))
    return True


def pick(app, title, items, default=1):
    """Show a numbered list and read a choice; returns the index or None."""
    S = app.tui.style
    app.tui.w(S.bold(title))
    for i, it in enumerate(items, 1):
        app.tui.w("  %s %s" % (S.accent("%d." % i), it))
    try:
        raw = input(S.accent("  Choose [%d] " % default)).strip()
    except (EOFError, KeyboardInterrupt):
        app.tui.w()
        return None
    if not raw:
        return default - 1
    if raw.isdigit() and 1 <= int(raw) <= len(items):
        return int(raw) - 1
    return None


def confirm(app, question, default=False):
    S = app.tui.style
    try:
        raw = input(S.warn("  %s %s " % (question, "[Y/n]" if default else "[y/N]"))).strip().lower()
    except (EOFError, KeyboardInterrupt):
        app.tui.w()
        return False
    return default if not raw else raw in ("y", "yes")


# ---- help, exit, new ------------------------------------------------------------------------------------------------

@cmd("help", "Show this list", aliases=("?",))
def c_help(app, arg):
    S = app.tui.style
    groups = {}
    for n, (fn, h, g, hint) in sorted(REGISTRY.items()):
        groups.setdefault(g, []).append((n, h, hint))
    for g in ("Session", "Models", "Permissions and tools", "Project", "Workflows", "Settings", "Info"):
        if g not in groups:
            continue
        app.tui.w(S.bold(S.accent(g)))
        for n, h, hint in groups[g]:
            app.tui.w("  %s %s" % (S.bold(("/" + n + (" " + hint if hint else "")).ljust(26)), S.muted(h)))
    custom = app.load_custom_commands()
    if custom:
        app.tui.w(S.bold(S.accent("Your commands")))
        for n, c in sorted(custom.items()):
            app.tui.w("  %s %s" % (S.bold(("/" + n).ljust(26)), S.muted(c["description"])))
    app.tui.w(S.muted("\n  Type @ then a file name to include a file, ! then a command to run it yourself, # then a note to remember it."))
    app.tui.w(S.muted("  esc interrupts the model; shift+tab cycles ask / accept edits / plan; a line ending in \\ continues on the next line."))


@cmd("exit", "Leave yardcode", aliases=("quit", "q"))
def c_exit(app, arg):
    return False


@cmd("clear", "Start a new, empty conversation", aliases=("new", "reset"))
def c_clear(app, arg):
    window = app.agent.context_window
    app.new_agent(window=window)
    app.tui.w(app.tui.style.muted("  New conversation."))


@cmd("compact", "Compress the conversation to free up context (optional: what to keep)", hint="[focus]")
def c_compact(app, arg):
    try:
        app.agent.maybe_compact(force=True, instructions=arg, reason="manual")
    except ValueError as e:
        app.tui.warn(str(e))
    except APIError as e:
        app.tui.error(str(e))


@cmd("context", "Show how much of the model's context window is used", group="Info")
def c_context(app, arg):
    S, ag = app.tui.style, app.agent
    sysp = util.est_tokens(ag.system_prompt())
    tools = util.est_tokens(json.dumps(ag.schemas()))
    mem = sum(i[2] for i in ag.memory_info) // 4 if ag.memory_info else 0
    msgs = {"user": 0, "assistant": 0, "tool": 0}
    for m in ag.session.messages:
        msgs[m["role"]] = msgs.get(m["role"], 0) + compact.message_tokens(m)
    used = ag.context_used()
    window = ag.context_window
    total = window or max(used, 1)
    bar_w = 40
    fill = min(bar_w, int(bar_w * used / total))
    bar = S.accent("█" * fill if S.unicode else "#" * fill) + S.muted("░" * (bar_w - fill) if S.unicode else "." * (bar_w - fill))
    app.tui.w("%s  %s / %s%s" % (bar, util.human_tokens(used), util.human_tokens(window) if window else "?", ("  (%d%%)" % round(used * 100 / window)) if window else ""))
    rows = [("System prompt", sysp), ("Tool definitions (%d)" % len(ag.tools), tools), ("  of which instructions files", mem), ("Your messages", msgs["user"]),
            ("Model replies", msgs["assistant"]), ("Tool results", msgs["tool"])]
    for n, v in rows:
        app.tui.w("  %-34s %8s" % (n, util.human_tokens(v)))
    if window:
        app.tui.w("  %-34s %8s" % (S.bold("Free"), util.human_tokens(max(0, window - used))))
    else:
        app.tui.w(S.muted("  The server didn't say how big its context is. Set it: /config context_window 16384"))
    thr = (app.settings.get("compact") or {}).get("threshold", 0.8)
    app.tui.w(S.muted("  The conversation is compressed automatically at %d%% (/compact does it now)." % round(thr * 100)))


@cmd("cost", "Tokens and speed so far (it's your own hardware: no bill)", group="Info", aliases=("usage",))
def c_cost(app, arg):
    u = app.session.usage
    S = app.tui.style
    gen = (u["gen_tokens"] / u["gen_seconds"]) if u["gen_seconds"] > 0 else 0
    app.tui.w("  Requests:      %d" % u["requests"])
    app.tui.w("  Prompt tokens: %s" % util.human_tokens(u["prompt"]))
    app.tui.w("  Reply tokens:  %s" % util.human_tokens(u["completion"]))
    app.tui.w("  Model time:    %s" % util.human_duration(u["seconds"]))
    if gen:
        app.tui.w("  Average speed: %.1f tokens/s" % gen)
    app.tui.w(S.muted("  Running on your own cluster costs nothing per token."))


# ---- models ------------------------------------------------------------------------------------------------------------

def model_rows(app):
    try:
        d = app.modelapi.models()
        return d.get("models", []), d.get("downloads", [])
    except ModelAPIError as e:
        raise e


def show_models(app):
    S = app.tui.style
    try:
        rows, downloads = model_rows(app)
    except ModelAPIError as e:
        app.tui.warn(str(e))
        names_ = app.client.models() if app.client.base_url else []
        for n in names_:
            app.tui.w("  %s %s" % (S.ok(S.g("dot")), n))
        return []
    for i, m in enumerate(rows, 1):
        on = m.get("loaded") or m.get("active")
        mark = S.ok(S.g("dot")) if on else S.muted("○" if S.unicode else "o")
        state = "loaded" + (" · ready" if m.get("ready") else " · loading") if on else "downloaded" if m.get("downloaded", True) else ""
        size = util.human_bytes(m["size"]) if m.get("size") else ""
        app.tui.w("  %s %s %-52s %-7s %9s  %s %s" % (S.accent("%2d." % i), mark, (m.get("name") or m.get("file") or "")[:52], m.get("kind", ""), size, state, S.muted(",".join(m.get("nodes", [])))))
    for dl in downloads:
        app.tui.w("     %s downloading %s %s" % (S.warn("↓" if S.unicode else "v"), dl.get("file", ""), S.muted(str(dl.get("progress", "")))))
    if not rows:
        app.tui.w(S.muted("  No models are downloaded on the cluster. Find one with /search and get it with /download."))
    return rows


@cmd("models", "List the models on your cluster (loaded and downloaded)", group="Models")
def c_models(app, arg):
    show_models(app)


def follow_job(app, job, label):
    S = app.tui.style
    app.tui.spinner.begin(label, "esc to stop")
    try:
        state = app.modelapi.follow(job, on_line=lambda l: (app.tui.spinner.end(), app.tui.w("  " + S.muted(l)), app.tui.spinner.begin(label, "esc to stop")), timeout=3600)
    except KeyboardInterrupt:
        app.tui.spinner.end()
        app.tui.warn("Stopped watching; the task keeps running on the cluster.")
        return "stopped"
    finally:
        app.tui.spinner.end()
    return state


def wait_serving(app, name):
    S = app.tui.style
    t0 = time.time()
    app.tui.spinner.begin("Loading %s into memory" % name, "ctrl-c to stop waiting")
    try:
        ok = app.client.wait_ready(timeout=1800, tick=lambda st, left: app.tui.spinner.update("Loading %s into memory · %s" % (name, util.human_duration(time.time() - t0))))
    except KeyboardInterrupt:
        ok = False
    finally:
        app.tui.spinner.end()
    return ok


def load_model(app, name, ask=True):
    S = app.tui.style
    if ask and not confirm(app, "Switch the cluster to %s? Everyone using the model will be affected." % name):
        return
    try:
        job = app.modelapi.load(name)
    except ModelAPIError as e:
        app.tui.error(str(e))
        return
    if not job.get("job"):          # nothing to follow (an Ollama model: loaded straight away)
        app.tui.w(S.ok("  %s %s" % (S.g("check"), job.get("message") or "Done.")))
        return
    state = follow_job(app, job.get("job", ""), "Switching model")
    if state not in ("ok", "success", "stopped"):
        app.tui.error("Loading failed (%s)." % state)
        return
    if state == "stopped":
        return
    if wait_serving(app, name):
        app.client.model = ""
        app.discover()
        app.agent.context_window = int(app.settings.get("context_window", 0) or 0) or app.client.context_window()
        app.tui.w(S.ok("  %s %s is ready." % (S.g("check"), name)) + S.muted("  context %s" % util.human_tokens(app.agent.context_window)))
    else:
        app.tui.warn("It is still loading. Try again in a minute, or watch it on the dashboard.")


@cmd("model", "Pick or switch the model (loads it on the cluster when needed)", group="Models", hint="[name]")
def c_model(app, arg):
    S = app.tui.style
    rows = []
    try:
        rows, _ = model_rows(app)
    except ModelAPIError as e:
        app.tui.warn(str(e))
    if arg:
        # a name or number
        pick_row = None
        if arg.isdigit() and rows and 1 <= int(arg) <= len(rows):
            pick_row = rows[int(arg) - 1]
        else:
            hits = [r for r in rows if arg.lower() in (r.get("name") or r.get("file") or "").lower()]
            if len(hits) == 1:
                pick_row = hits[0]
            elif len(hits) > 1:
                app.tui.warn("%d models match %r: %s" % (len(hits), arg, ", ".join((h.get("name") or h.get("file")) for h in hits[:5])))
                return
        if pick_row is None:
            app.client.model = arg
            app.tui.w(S.muted("  Using the model name %r for requests." % arg))
            return
        arg_name = pick_row.get("file") or pick_row.get("name")
        if pick_row.get("loaded") or pick_row.get("active"):
            app.client.model = pick_row.get("name") or arg_name
            app.tui.w(S.muted("  %s is already loaded." % arg_name))
            return
        return load_model(app, arg_name)
    if not rows:
        return show_models(app)
    show_models(app)
    try:
        raw = input(S.accent("  Load which one? (number, Enter to cancel) ")).strip()
    except (EOFError, KeyboardInterrupt):
        app.tui.w()
        return
    if raw.isdigit() and 1 <= int(raw) <= len(rows):
        r = rows[int(raw) - 1]
        if r.get("loaded") or r.get("active"):
            app.tui.w(S.muted("  That one is already loaded."))
        else:
            load_model(app, r.get("file") or r.get("name"))


@cmd("load", "Load a downloaded model on the cluster", group="Models", hint="<model>")
def c_load(app, arg):
    if not arg:
        return c_model(app, "")
    return c_model(app, arg)


@cmd("unload", "Unload the model to free the cluster's memory", group="Models", hint="[model]")
def c_unload(app, arg):
    if not confirm(app, "Unload the model? Nothing can answer until you load one again."):
        return
    try:
        job = app.modelapi.unload(arg or None)
    except ModelAPIError as e:
        app.tui.error(str(e))
        return
    if not job.get("job"):
        app.tui.w(app.tui.style.ok("  %s %s" % (app.tui.style.g("check"), job.get("message") or "Unloaded.")))
        return
    state = follow_job(app, job.get("job", ""), "Unloading")
    if state in ("ok", "success"):
        app.tui.w(app.tui.style.ok("  %s Unloaded; the nodes have their memory back." % app.tui.style.g("check")))


@cmd("download", "Download a GGUF model to the cluster", group="Models", hint="<owner/repo> [file.gguf]")
def c_download(app, arg):
    S = app.tui.style
    parts = arg.split()
    if not parts:
        app.tui.w(S.muted("  Usage: /download owner/repo [file.gguf]   (find repos with /search)"))
        return
    repo = parts[0]
    file = parts[1] if len(parts) > 1 else ""
    try:
        if not file:
            files = app.modelapi.files(repo).get("files", [])
            if not files:
                app.tui.warn("No GGUF files in %s." % repo)
                return
            i = pick(app, "Files in %s" % repo, ["%-62s %9s  %s" % (f["file"], util.human_bytes(f["size"]), f.get("fits", "")) for f in files], default=1)
            if i is None:
                return
            file = files[i]["file"]
        if not confirm(app, "Download %s to the cluster's disks?" % file, True):
            return
        job = app.modelapi.download(repo, file)
    except ModelAPIError as e:
        app.tui.error(str(e))
        return
    app.tui.w(S.muted("  Started (task %s). It downloads in the background; check progress with /models." % job.get("job")))


@cmd("search", "Search Hugging Face for models to download", group="Models", hint="<words>")
def c_search(app, arg):
    S = app.tui.style
    if not arg:
        app.tui.w(S.muted("  Usage: /search qwen coder"))
        return
    try:
        res = app.modelapi.search(arg).get("results", [])
    except ModelAPIError as e:
        app.tui.error(str(e))
        return
    for i, r in enumerate(res, 1):
        app.tui.w("  %s %-60s %s" % (S.accent("%2d." % i), r.get("id", "")[:60], S.muted("%s downloads · %s likes" % (r.get("downloads"), r.get("likes")))))
    if res:
        app.tui.w(S.muted("  Get one with /download owner/repo"))


# ---- connection and settings ----------------------------------------------------------------------------------------------

def normalize_base(text):
    t = text.strip().rstrip("/")
    if not t:
        return ""
    if "://" not in t:
        t = "http://" + t
    from urllib.parse import urlsplit
    u = urlsplit(t)
    if not u.port:
        t = "%s://%s:31435%s" % (u.scheme, u.hostname if ":" not in (u.hostname or "") else "[%s]" % u.hostname, u.path.rstrip("/"))
    if not t.rstrip("/").endswith("/v1"):
        t = t.rstrip("/") + "/v1"
    return t


@cmd("login", "Connect to a model API (address and key)", group="Settings", hint="[address]")
def c_login(app, arg):
    S, st = app.tui.style, app.settings
    try:
        base = arg or input(S.accent("  API address (host, host:port or full URL) [%s]: " % (st.api_base or "none"))).strip() or st.api_base
        base = normalize_base(base)
        if not base:
            return
        key = input(S.accent("  API key (Enter to keep %s): " % ("the saved one" if st.get("api_key") else "none"))).strip()
    except (EOFError, KeyboardInterrupt):
        app.tui.w()
        return
    st.set("api_base", base, "user")
    if key:
        st.save_key(key)
    app.client.base_url, app.client.api_key = base, st.get("api_key", "")
    app.modelapi.base, app.modelapi.key = st.control_url(), st.get("api_key", "")
    app.client.model = ""
    state = app.client.health()
    window = app.discover()
    if app.agent:
        app.agent.context_window = window or app.agent.context_window
    if state == "ok" or app.client.models():
        app.tui.w(S.ok("  %s Connected to %s" % (S.g("check"), base)) + S.muted("  model %s · context %s" % (app.client.model or "?", util.human_tokens(window) if window else "unknown")))
    elif state == "loading":
        app.tui.w(S.warn("  Connected, but the model is still loading."))
    else:
        app.tui.warn("Saved, but %s didn't answer. Check the address and that the model is running." % base)


@cmd("logout", "Forget the saved API key", group="Settings")
def c_logout(app, arg):
    app.settings.save_key("")
    app.client.api_key = ""
    app.modelapi.key = ""
    app.tui.w(app.tui.style.muted("  Key removed."))


def coerce(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


@cmd("config", "Show or change settings", group="Settings", hint="[key [value]]")
def c_config(app, arg):
    S, st = app.tui.style, app.settings
    parts = arg.split(None, 1)
    if not parts:
        app.tui.w(json.dumps(st.dump(), indent=2, sort_keys=True))
        app.tui.w(S.muted("  user: %s\n  project: %s" % (st.user_path, st.project_path)))
        return
    key = parts[0]
    if len(parts) == 1:
        app.tui.w("  %s = %s" % (key, json.dumps(st.get(key))))
        return
    value = coerce(parts[1])
    scope = "user"
    if key in ("api_base",):
        value = normalize_base(str(value))
    st.set(key, value, scope)
    apply_live(app, key)
    app.tui.w(S.muted("  %s = %s (saved to %s)" % (key, json.dumps(st.get(key)), st.user_path)))


def apply_live(app, key):
    """Make a changed setting take effect now."""
    st = app.settings
    if key == "api_base":
        app.client.base_url = st.api_base
        app.modelapi.base = st.control_url()
    elif key == "control_url":
        app.modelapi.base = st.control_url()
    elif key == "model":
        app.client.model = st.get("model", "")
    elif key == "context_window" and app.agent:
        app.agent.context_window = int(st.get("context_window", 0) or 0)
    elif key == "tool_mode" and app.agent:
        app.agent.text_mode = st.get("tool_mode") == "text"
        app.agent._system_cache = app.agent._tools_cache = None
    elif key == "permission_mode" and app.agent:
        app.agent.perms.mode = st.get("permission_mode", "default")


@cmd("max", "Set the longest reply the model may write ('none' = no limit)", group="Settings", hint="[tokens|none]")
def c_max(app, arg):
    S, st = app.tui.style, app.settings
    if not arg:
        cur = int(st.get("max_tokens", 0) or 0)
        app.tui.w("  Max reply length: %s" % ("no limit" if not cur else "%d tokens" % cur))
        return
    if arg.lower() in ("none", "no", "off", "unlimited", "0", "no limit"):
        st.set("max_tokens", 0)
        app.tui.w(S.muted("  No limit: the model writes until it is done (or the context is full)."))
        return
    try:
        n = int(arg)
        assert n > 0
    except (ValueError, AssertionError):
        app.tui.warn("Give a number of tokens, or 'none'.")
        return
    st.set("max_tokens", n)
    app.tui.w(S.muted("  Replies are cut at %d tokens." % n))


@cmd("temperature", "Set how adventurous the model is (0 to 2)", group="Settings", hint="<number>")
def c_temp(app, arg):
    try:
        t = float(arg)
        assert 0 <= t <= 2
    except (ValueError, AssertionError):
        app.tui.w("  Temperature: %s (give a number from 0 to 2 to change it)" % app.settings.get("temperature"))
        return
    app.settings.set("temperature", t)


@cmd("think", "Show or hide the model's reasoning", group="Settings", hint="[show|hide]")
def c_think(app, arg):
    if arg in ("show", "hide"):
        app.settings.set("thinking", arg)
    app.tui.w("  Reasoning is %s." % ("shown" if app.settings.get("thinking") != "hide" else "hidden"))


@cmd("theme", "Change the colours", group="Settings", hint="[auto|dark|light|none]")
def c_theme(app, arg):
    if arg not in ("auto", "dark", "light", "none"):
        app.tui.w("  Theme: %s  (auto, dark, light, none)" % app.settings.get("theme"))
        return
    app.settings.set("theme", arg)
    app.tui.style = ui.detect_style(arg, app.tui.out)
    app.tui.spinner.S = app.tui.style
    app.tui.w(app.tui.style.accent("  Theme: %s" % arg))


@cmd("vim", "Switch the input line between emacs and vi keys", group="Settings")
def c_vim(app, arg):
    try:
        import readline
        on = not app.settings.get("vim")
        readline.parse_and_bind("set editing-mode %s" % ("vi" if on else "emacs"))
        app.settings.set("vim", on)
        app.tui.w("  Input keys: %s" % ("vi" if on else "emacs"))
    except Exception:
        app.tui.warn("This terminal's line editor can't switch modes.")


@cmd("trust", "Trust this project's own settings (hooks, MCP servers, allow rules)", group="Settings")
def c_trust(app, arg):
    st = app.settings
    if st.trusted:
        app.tui.w("  This project is already trusted.")
        return
    app.tui.w("  Files in %s can run commands for you (hooks, MCP servers, always-allow rules)." % st.project_dir)
    if confirm(app, "Trust them?"):
        st.trust_project()
        app.agent.refresh_prompt()
        app.tui.w(app.tui.style.ok("  Trusted. Restart yardcode so hooks and MCP servers start."))


@cmd("add-dir", "Let yardcode work in another folder too", group="Settings", hint="<path>")
def c_add_dir(app, arg):
    p = os.path.abspath(os.path.expanduser(arg))
    if not os.path.isdir(p):
        app.tui.warn("%s isn't a folder." % p)
        return
    app.agent.ctx.extra_dirs.append(p)
    app.tui.w("  Added %s for this session." % p)


# ---- permissions, tools, plugins ----------------------------------------------------------------------------------------------

@cmd("mode", "Change how much it asks before acting", group="Permissions and tools", hint="[default|acceptEdits|plan|bypassPermissions]")
def c_mode(app, arg):
    perms = app.agent.perms
    if not arg:
        mode = perms.cycle_mode()
    elif arg in MODES:
        if arg == "bypassPermissions" and not confirm(app, "Bypass every permission question? The model can then change or delete anything you can."):
            return
        perms.mode = arg
        mode = arg
    elif arg in ("accept", "edits", "auto"):
        perms.mode = mode = "acceptEdits"
    elif arg == "ask":
        perms.mode = mode = "default"
    else:
        app.tui.warn("Modes: %s" % ", ".join(MODES))
        return
    app.tui.w(app.tui.style.accent("  Mode: %s" % MODE_LABEL[mode]))


@cmd("plan", "Plan first: read-only until you approve the plan", group="Permissions and tools")
def c_plan(app, arg):
    app.agent.perms.mode = "plan"
    app.tui.w(app.tui.style.accent("  Plan mode: it can read and search but not change anything until you approve its plan."))
    if arg:
        return arg


@cmd("permissions", "Show or edit the allow / ask / deny rules", group="Permissions and tools", hint="[allow|deny|ask|remove RULE]")
def c_permissions(app, arg):
    S, st, perms = app.tui.style, app.settings, app.agent.perms
    parts = arg.split(None, 1)
    if parts and parts[0] in ("allow", "deny", "ask") and len(parts) == 2:
        st.add_permission(parts[0], parts[1].strip(), "local")
        app.tui.w(S.muted("  Added %s rule %s (saved in %s)" % (parts[0], parts[1], st.local_path)))
        return
    if parts and parts[0] == "remove" and len(parts) == 2:
        for k in ("allow", "deny", "ask"):
            st.remove_permission(k, parts[1].strip())
        if parts[1].strip() in perms.session_allow:
            perms.session_allow.remove(parts[1].strip())
        app.tui.w(S.muted("  Removed."))
        return
    app.tui.w("  Mode: %s" % S.bold(MODE_LABEL[perms.mode]))
    for kind in ("allow", "ask", "deny"):
        rules = list((st.get("permissions") or {}).get(kind, [])) + (perms.session_allow if kind == "allow" else [])
        app.tui.w("  %s: %s" % (S.bold(kind), ", ".join(rules) if rules else S.muted("none")))
    app.tui.w(S.muted("  Examples: /permissions allow Bash(git status:*)   /permissions deny Bash(rm:*)   /permissions allow WebFetch(domain:python.org)"))
    app.tui.w(S.muted("  Reading and searching never asks (inside the project); edits, commands and unknown web pages do."))


@cmd("tools", "List the tools, or switch one on or off", group="Permissions and tools", hint="[on|off NAME]")
def c_tools(app, arg):
    from .tools import GROUPS
    S, st = app.tui.style, app.settings
    parts = arg.split()
    disabled = set(st.get("tools.disabled") or [])
    if len(parts) == 2 and parts[0] in ("on", "off"):
        name = parts[1]
        known = {t for _, g in GROUPS for t in g}
        if name not in known and name not in app.agent.tools:
            app.tui.warn("No tool called %s." % name)
            return
        (disabled.discard if parts[0] == "on" else disabled.add)(name)
        st.set("tools.disabled", sorted(disabled))
        ag = app.agent
        ag.tools = {t.name: t for t in app.tool_list()}
        ag.refresh_prompt()
        app.tui.w(S.muted("  %s is now %s." % (name, parts[0])))
        return
    for g, members in GROUPS:
        app.tui.w(S.bold(S.accent(g)))
        for n in members:
            t = app.agent.tools.get(n)
            on = t is not None
            desc = (t.description if t else "").split(". ")[0][:80]
            app.tui.w("  %s %-16s %s" % (S.ok(S.g("check")) if on else S.muted(S.g("cross")), n, S.muted(desc if on else ("off" if n in disabled else "not available"))))
    extra = [t for n, t in app.agent.tools.items() if n not in {x for _, g in GROUPS for x in g}]
    if extra:
        app.tui.w(S.bold(S.accent("Plugins and MCP")))
        for t in extra:
            app.tui.w("  %s %-30s %s" % (S.ok(S.g("check")), t.name, S.muted(t.description[:70])))
    app.tui.w(S.muted("  Fewer tools means a shorter prompt, which is faster on a small model: /tools off Arxiv"))


@cmd("web", "Turn the web tools (search, fetch, Wikipedia...) on or off", group="Permissions and tools", hint="[on|off]")
def c_web(app, arg):
    from .tools import GROUPS
    web = dict(GROUPS)["Web"]
    if arg not in ("on", "off"):
        on = [n for n in web if n in app.agent.tools]
        app.tui.w("  Web tools: %s" % (", ".join(on) if on else "off"))
        return
    for n in web:
        c_tools(app, "%s %s" % (arg, n))


@cmd("plugins", "List plugin tools, or create an example plugin", group="Permissions and tools", hint="[new]")
def c_plugins(app, arg):
    from .plugins import plugin_dirs
    S = app.tui.style
    d = plugin_dirs(app.settings)[0]
    if arg == "new":
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "example.py")
        if os.path.exists(path):
            app.tui.warn("%s already exists." % path)
            return
        util.atomic_write(path, '''"""An example yardcode plugin: tools the model can call. Edit it, then restart yardcode."""
import random

TOOLS = [{
    "name": "Dice",
    "description": "Roll a die with the given number of sides (default 6). Use for random choices.",
    "parameters": {"type": "object", "properties": {"sides": {"type": "integer", "description": "Number of sides"}}},
    "read_only": True,
    "kind": "read",
    "run": lambda args, ctx: "Rolled %d" % random.randint(1, int(args.get("sides") or 6)),
}]
''')
        app.tui.w("  Created %s" % path)
        return
    core = {t for t in app.agent.tools if t.startswith("mcp__")}
    found = [t for t in app.agent.tools.values() if getattr(t, "source", None)]
    app.tui.w("  Plugin folder: %s" % d)
    for t in found:
        app.tui.w("  %s %s  %s" % (S.ok(S.g("check")), t.name, S.muted(util.shorten_path(t.source))))
    if not found:
        app.tui.w(S.muted("  No plugins yet. Create an example with /plugins new"))
    _ = core


@cmd("mcp", "List MCP servers, or add one", group="Permissions and tools", hint="[add NAME COMMAND ARGS...]")
def c_mcp(app, arg):
    S, st = app.tui.style, app.settings
    parts = arg.split()
    if parts and parts[0] == "add" and len(parts) >= 3:
        spec = {"command": parts[2], "args": parts[3:]}
        servers = dict(st.get("mcpServers") or {})
        servers[parts[1]] = spec
        st.set("mcpServers", servers, "user")
        app.tui.w(S.muted("  Saved. Restart yardcode to start it."))
        return
    if parts and parts[0] == "remove" and len(parts) == 2:
        servers = dict(st.get("mcpServers") or {})
        servers.pop(parts[1], None)
        st.set("mcpServers", servers, "user")
        app.tui.w(S.muted("  Removed. Restart yardcode."))
        return
    if not app.mcp_servers:
        app.tui.w(S.muted("  No MCP servers. Add one: /mcp add files npx -y @modelcontextprotocol/server-filesystem ."))
    for s in app.mcp_servers:
        ok = not s.error
        app.tui.w("  %s %s  %s" % (S.ok(S.g("check")) if ok else S.err(S.g("cross")), s.name, S.muted("%d tools" % len(s.tools) if ok else s.error)))


@cmd("hooks", "Show the configured hooks", group="Permissions and tools")
def c_hooks(app, arg):
    h = app.settings.get("hooks") or {}
    if not h:
        app.tui.w(app.tui.style.muted("  No hooks. Add them under \"hooks\" in %s" % app.settings.user_path))
    for ev, groups in h.items():
        for g in groups:
            for x in g.get("hooks", []):
                app.tui.w("  %s %s -> %s" % (app.tui.style.bold(ev), g.get("matcher", "*"), x.get("command")))


@cmd("agents", "List the sub-agents the model can start", group="Permissions and tools")
def c_agents(app, arg):
    from .agent import load_agents
    for n, a in load_agents(app.settings).items():
        app.tui.w("  %s  %s" % (app.tui.style.bold(n.ljust(18)), app.tui.style.muted(a["description"])))


@cmd("todos", "Show the current task list", group="Permissions and tools")
def c_todos(app, arg):
    if not app.agent.ctx.todos:
        app.tui.w(app.tui.style.muted("  No tasks."))
    else:
        app.tui.todos(app.agent.ctx.todos)


# ---- project and workflows -------------------------------------------------------------------------------------------------

@cmd("memory", "Show the instruction files in use, or edit them", group="Project", hint="[edit [user]]")
def c_memory(app, arg):
    S = app.tui.style
    parts = arg.split()
    if parts and parts[0] == "edit":
        path = memory.user_file() if len(parts) > 1 and parts[1] == "user" else os.path.join(app.agent.ctx.cwd, "YARDCODE.md")
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or shutil.which("nano") or "vi"
        if not os.path.exists(path):
            util.atomic_write(path, "# Notes for yardcode\n\n")
        subprocess.call([editor, path])
        app.agent.refresh_prompt()
        return
    info = app.agent.memory_info
    if not info:
        app.tui.w(S.muted("  No YARDCODE.md (or AGENTS.md / CLAUDE.md) found. Create one with /init, or save a note with #."))
    for path, scope, chars in info:
        app.tui.w("  %s %-8s %s" % (S.ok(S.g("check")), scope, util.shorten_path(path)) + S.muted("  %s chars" % chars))
    app.tui.w(S.muted("  /memory edit opens the project file; /memory edit user opens your own."))


@cmd("init", "Have the model study this project and write a YARDCODE.md for it", group="Project")
def c_init(app, arg):
    return ("Study this project (look at the folder layout, README, package/build files and a few key source files) and create a YARDCODE.md in the project root. "
            "It should contain: what the project is, how to build, test and lint it (exact commands), the architecture in a few bullets, code style conventions, "
            "and anything non-obvious a newcomer would get wrong. Keep it under 60 lines and don't repeat obvious things. If YARDCODE.md already exists, improve it instead of replacing it.")


@cmd("review", "Review the uncommitted changes", group="Workflows", hint="[focus]")
def c_review(app, arg):
    return ("Review the current uncommitted changes: run `git diff` and `git diff --staged` (and read changed files where you need context). "
            "Give a prioritized review: bugs and edge cases first, then security, then design and style, then missing tests. Reference file:line, and keep it short. "
            "Don't change anything." + ((" Focus on: " + arg) if arg else ""))


@cmd("commit", "Write a commit message and commit the changes", group="Workflows")
def c_commit(app, arg):
    return ("Look at `git status` and `git diff` (staged and unstaged). If nothing is staged, stage the tracked changes with `git add -u`. Write a clear commit message "
            "(imperative subject under 72 characters, then a short body only if needed) and run `git commit`. Don't push." + ((" " + arg) if arg else ""))


@cmd("research", "Deep research on a topic: several searches, reading sources, a cited report", group="Workflows", hint="<topic>")
def c_research(app, arg):
    if not arg:
        app.tui.w(app.tui.style.muted("  Usage: /research how does speculative decoding speed up llama.cpp"))
        return
    return ("Do thorough research on: %s\n\nProcess: 1) Plan 3-6 distinct search queries covering different angles. 2) Run WebSearch for each. 3) Use WebFetch to read the 3-6 most "
            "relevant and authoritative pages in full (prefer primary sources, official docs and papers; Arxiv and Wikipedia can help). 4) Cross-check claims between sources and note "
            "disagreements or uncertainty. 5) Write a structured report: a short summary first, then sections with the key findings, then caveats, then a numbered list of sources "
            "with their URLs. Cite sources inline like [1]. Don't state anything you didn't see in a source, and never invent URLs." % arg)


@cmd("resume", "Continue an earlier conversation", group="Session", hint="[id]")
def c_resume(app, arg):
    S = app.tui.style
    rows = Session.list(app.agent.ctx.cwd, 15)
    if arg:
        found = Session.find(arg, app.agent.ctx.cwd) or Session.find(arg)
        if not found:
            app.tui.warn("No conversation starts with %r." % arg)
            return
    else:
        rows = [r for r in rows if r["id"] != app.session.id]
        if not rows:
            app.tui.w(S.muted("  No earlier conversations in this folder."))
            return
        i = pick(app, "Earlier conversations here", ["%s  %-60s %s" % (time.strftime("%d %b %H:%M", time.localtime(r["updated"])), r["title"][:60], S.muted("%d msgs" % r["turns"])) for r in rows])
        if i is None:
            return
        found = rows[i]
    window = app.agent.context_window
    app.new_agent(Session(app.agent.ctx.cwd, found["id"], True), window)
    n = len(app.session.messages)
    app.tui.w(S.muted("  Resumed %r (%d messages)." % (found["title"][:50], n)))
    last = next((m for m in reversed(app.session.messages) if m["role"] == "assistant" and m.get("content")), None)
    if last:
        app.tui.w(S.muted("  Last reply:"))
        app.tui.w(ui.render_markdown(util.truncate_middle(last["content"], 1200), S))


@cmd("sessions", "List earlier conversations in this folder", group="Session")
def c_sessions(app, arg):
    S = app.tui.style
    for r in Session.list(app.agent.ctx.cwd, 20):
        app.tui.w("  %s  %s  %-58s %s" % (S.muted(r["id"]), time.strftime("%d %b %H:%M", time.localtime(r["updated"])), r["title"][:58], S.muted("%d msgs" % r["turns"])))


@cmd("rewind", "Go back to an earlier point and undo the file changes made since", group="Session", hint="[n]")
def c_rewind(app, arg):
    S, ses = app.tui.style, app.session
    turns = ses.user_turns()
    if not turns:
        app.tui.w(S.muted("  Nothing to rewind to."))
        return
    if arg.isdigit() and 1 <= int(arg) <= len(turns):
        n = int(arg)
    else:
        show = turns[-12:]
        base = len(turns) - len(show)
        items = []
        for k, (i, text) in enumerate(show, 1):
            changed = len(ses.checkpoints.files_since(base + k))
            items.append("%-64s %s" % (text.strip().split("\n")[0][:64], S.muted("(%d file%s changed after)" % (changed, "" if changed == 1 else "s") if changed else "")))
        i = pick(app, "Rewind to before which message?", items, default=len(items))
        if i is None:
            return
        n = base + i + 1
    mode = pick(app, "What to rewind?", ["Code and conversation", "Conversation only", "Code only"], 1)
    if mode is None:
        return
    restored, dropped = ses.rewind(n, files=mode in (0, 2), conversation=mode in (0, 1))
    app.agent.last_prompt_tokens = 0
    app.tui.w(S.muted("  Restored %d file%s, dropped %d message%s." % (len(restored), "" if len(restored) == 1 else "s", dropped, "" if dropped == 1 else "s")))
    for p in restored[:8]:
        app.tui.w(S.muted("    " + util.shorten_path(p)))


@cmd("undo", "Undo the last turn (conversation and file changes)", group="Session")
def c_undo(app, arg):
    turns = app.session.user_turns()
    if not turns:
        app.tui.w(app.tui.style.muted("  Nothing to undo."))
        return
    restored, dropped = app.session.rewind(len(turns), True, True)
    app.agent.last_prompt_tokens = 0
    app.tui.w(app.tui.style.muted("  Undid the last turn: %d file%s restored." % (len(restored), "" if len(restored) == 1 else "s")))


@cmd("diff", "Show uncommitted changes", group="Workflows")
def c_diff(app, arg):
    from .app import git_diff_text
    text = git_diff_text(app.agent.ctx.cwd)
    if not text.strip():
        app.tui.w(app.tui.style.muted("  No uncommitted changes."))
        return
    S = app.tui.style
    for l in text.split("\n")[:300]:
        if l.startswith("+") and not l.startswith("+++"):
            app.tui.w(S.ok(l))
        elif l.startswith("-") and not l.startswith("---"):
            app.tui.w(S.err(l))
        elif l.startswith("@@"):
            app.tui.w(S.accent(l))
        else:
            app.tui.w(S.muted(l) if l.startswith(("diff", "index", "+++", "---")) else l)


@cmd("export", "Save the conversation as a markdown file", group="Session", hint="[file]")
def c_export(app, arg):
    path = arg or "yardcode-%s.md" % app.session.id
    out = ["# yardcode conversation", "", "- date: %s" % util.iso(app.session.created), "- model: %s" % app.client.model, "- folder: %s" % app.agent.ctx.cwd, ""]
    for m in app.session.messages:
        if m["role"] == "user":
            out += ["## You", "", m.get("content", ""), ""]
        elif m["role"] == "assistant":
            if m.get("content"):
                out += ["## Assistant", "", m["content"], ""]
            for c in m.get("tool_calls") or []:
                out += ["> tool: `%s` %s" % (c["function"]["name"], c["function"].get("arguments", "")[:300]), ""]
        elif m["role"] == "tool":
            out += ["```", util.truncate_middle(m.get("content", ""), 3000), "```", ""]
    util.atomic_write(path, "\n".join(out), 0o644)
    app.tui.w(app.tui.style.muted("  Saved %s" % path))


@cmd("copy", "Copy the last answer to the clipboard", group="Session")
def c_copy(app, arg):
    last = app.agent.last_text or next((m.get("content") for m in reversed(app.session.messages) if m["role"] == "assistant" and m.get("content")), "")
    if not last:
        app.tui.w(app.tui.style.muted("  Nothing to copy."))
        return
    for tool in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["clip.exe"]):
        if shutil.which(tool[0]):
            subprocess.run(tool, input=last.encode(), timeout=5)
            app.tui.w(app.tui.style.muted("  Copied."))
            return
    import base64
    sys.stdout.write("\x1b]52;c;%s\x07" % base64.b64encode(last.encode()).decode())
    sys.stdout.flush()
    app.tui.w(app.tui.style.muted("  Sent to the terminal's clipboard (if it allows that)."))


# ---- info ---------------------------------------------------------------------------------------------------------------------

@cmd("status", "Show the connection, model, folder and session", group="Info")
def c_status(app, arg):
    S, ag, st = app.tui.style, app.agent, app.settings
    rows = [("yardcode", __version__), ("Model", app.client.model or "-"), ("API", app.client.base_url or "not set"), ("Dashboard", app.modelapi.base or "not set"),
            ("Health", app.client.health() if app.client.base_url else "-"), ("Folder", ag.ctx.cwd), ("Mode", MODE_LABEL[ag.perms.mode]),
            ("Context", "%s of %s" % (util.human_tokens(ag.context_used()), util.human_tokens(ag.context_window) if ag.context_window else "?")),
            ("Max reply", "no limit" if not int(st.get("max_tokens", 0) or 0) else "%s tokens" % st.get("max_tokens")),
            ("Tool calls", "text format" if ag.text_mode else "native"), ("Session", app.session.id),
            ("Project settings", ("trusted" if st.trusted else "not trusted yet: /trust to use its hooks and rules") if os.path.isdir(st.project_dir) else "none in this folder")]
    for k, v in rows:
        app.tui.w("  %s %s" % (S.bold(k.ljust(16)), v))


@cmd("doctor", "Check that everything works", group="Info")
def c_doctor(app, arg):
    S, st = app.tui.style, app.settings
    ok = lambda m, fix="": app.tui.w("  %s %s" % (S.ok(S.g("check")), m))
    bad = lambda m, fix="": app.tui.w("  %s %s%s" % (S.err(S.g("cross")), m, S.muted("\n      " + fix) if fix else ""))
    py = sys.version_info
    (ok if py >= (3, 8) else bad)("Python %d.%d.%d" % py[:3], "yardcode needs Python 3.8 or newer")
    if not st.api_base:
        bad("No model API address", "Run /login")
    else:
        state = app.client.health()
        if state == "ok":
            ok("Model API answers at %s" % st.api_base)
        elif state == "loading":
            bad("The model is still loading", "Wait a minute, or watch it on the dashboard")
        else:
            bad("Can't reach %s" % st.api_base, "Is the cluster on and are you on its network (Tailscale)?")
        names_ = app.client.models()
        (ok if names_ else bad)("Model list: %s" % (", ".join(names_[:3]) if names_ else "empty or refused (check the API key with /login)"))
        win = app.agent.context_window
        (ok if win else bad)("Context window: %s" % (util.human_tokens(win) if win else "unknown"), "Set it with /config context_window 16384")
    if app.modelapi.available:
        try:
            s = app.modelapi.status()
            ok("Dashboard control API at %s (model state: %s)" % (app.modelapi.base, (s.get("model") or {}).get("state", "?")))
        except ModelAPIError as e:
            bad("Dashboard control API: %s" % e, "Remote model loading needs nodeyard updated on the server")
    from .tools.web import web_search
    try:
        res, eng = web_search("test", 1, app.agent.ctx)
        ok("Web search works (%s)" % eng)
    except Exception as e:
        bad("Web search failed: %s" % str(e)[:140], "Check the internet connection, or set search.searxng_url / search.brave_key")
    for tool in ("git", "rg", "pdftotext"):
        found = shutil.which(tool)
        app.tui.w("  %s %s" % (S.ok(S.g("check")) if found else S.muted(S.g("bullet")), tool + (" found" if found else " not installed (optional)")))
    ok("Settings: %s" % util.shorten_path(st.user_path))
    ok("%d tools ready%s" % (len(app.agent.tools), ", tool calls: %s" % ("text format" if app.agent.text_mode else "native")))
    app.tui.w("  %s %s %s" % (S.muted(S.g("bullet")), "Python", platform.python_implementation() + " " + platform.python_version()))


@cmd("release-notes", "What's new", group="Info")
def c_notes(app, arg):
    app.tui.w("  yardcode %s. Plain-language notes live in the project's README." % __version__)


_ = MODES
