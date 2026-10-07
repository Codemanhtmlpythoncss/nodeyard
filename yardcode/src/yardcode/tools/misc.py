"""The smaller tools: TodoWrite, Calculator, FileSearch, Memory, AskUserQuestion, ExitPlanMode and Model."""
import ast
import math
import operator
import os
import re
from collections import Counter

from .. import memory as mem
from .. import util
from ..modelapi import ModelAPIError
from .base import Result, Tool, ToolError, need
from .files import SKIP_DIRS, glob_to_regex, walk_files

# ---- TodoWrite ------------------------------------------------------------------------------------


class TodoWrite(Tool):
    name = "TodoWrite"
    kind = "read"
    description = "Keep a task list for multi-step work: send the whole list each time, one item in_progress."
    parameters = {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {
        "content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
        "required": ["content", "status"]}}}, "required": ["todos"]}

    def run(self, args, ctx):
        todos = args.get("todos")
        if not isinstance(todos, list):
            raise ToolError("todos must be a list.")
        clean = []
        for t in todos:
            if not isinstance(t, dict) or not str(t.get("content", "")).strip():
                continue
            st = t.get("status") if t.get("status") in ("pending", "in_progress", "completed") else "pending"
            clean.append({"content": str(t["content"]).strip(), "status": st, "activeForm": str(t.get("activeForm") or t["content"]).strip()})
        ctx.todos = clean
        if ctx.frontend and hasattr(ctx.frontend, "todos"):
            ctx.frontend.todos(clean)
        done = sum(1 for t in clean if t["status"] == "completed")
        busy = sum(1 for t in clean if t["status"] == "in_progress")
        note = " Only one task should be in progress at a time." if busy > 1 else ""
        return Result("Todos updated (%d of %d done).%s Carry on with the current task." % (done, len(clean), note), summary="%d of %d done" % (done, len(clean)))


# ---- Calculator --------------------------------------------------------------------------------------

FUNCS = {n: getattr(math, n) for n in ("sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh", "tanh", "log", "log2", "log10", "exp",
                                       "floor", "ceil", "factorial", "gcd", "lcm", "radians", "degrees", "hypot", "comb", "perm", "isqrt", "fabs", "copysign") if hasattr(math, n)}
FUNCS.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow, "int": int, "float": float})
CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}
BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
          ast.Mod: operator.mod, ast.Pow: operator.pow, ast.LShift: operator.lshift, ast.RShift: operator.rshift, ast.BitAnd: operator.and_,
          ast.BitOr: operator.or_, ast.BitXor: operator.xor}
UNOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Invert: operator.invert}
CMPS = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne}


def calc_eval(node):
    if isinstance(node, ast.Expression):
        return calc_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)):
        return node.value
    if isinstance(node, ast.Name) and node.id in CONSTS:
        return CONSTS[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in BINOPS:
        a, b = calc_eval(node.left), calc_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(b) > 10000 and abs(a) > 1:
            raise ToolError("That power is too large to compute.")
        if isinstance(node.op, ast.LShift) and b > 4096:
            raise ToolError("That shift is too large.")
        return BINOPS[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and type(node.op) in UNOPS:
        return UNOPS[type(node.op)](calc_eval(node.operand))
    if isinstance(node, ast.Compare) and all(type(o) in CMPS for o in node.ops):
        left = calc_eval(node.left)
        for op, c in zip(node.ops, node.comparators):
            right = calc_eval(c)
            if not CMPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS and not node.keywords:
        args = [calc_eval(a) for a in node.args]
        if node.func.id == "factorial" and args and args[0] > 5000:
            raise ToolError("That factorial is too large.")
        return FUNCS[node.func.id](*args)
    if isinstance(node, (ast.Tuple, ast.List)):
        return [calc_eval(x) for x in node.elts]
    raise ToolError("That expression uses something the calculator doesn't allow. Use the Python tool for anything beyond arithmetic.")


class Calculator(Tool):
    name = "Calculator"
    kind = "read"
    description = "Exact arithmetic (+ - * / ** << >> & | ^, hex/binary, sqrt, log, sin...). Use it instead of mental math."
    parameters = {"type": "object", "properties": {"expression": {"type": "string", "description": "e.g. 0b1010100101 << 2  or  sqrt(2) * 10**3"}}, "required": ["expression"]}

    def specifier(self, args, ctx):
        return args.get("expression", "")

    def run(self, args, ctx):
        expr = need(args, "expression").strip().replace("^^", "**").replace("×", "*").replace("÷", "/")
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as e:
            raise ToolError("Can't read that expression: %s" % e.msg)
        try:
            v = calc_eval(tree)
        except (ZeroDivisionError, OverflowError, ValueError, TypeError) as e:
            raise ToolError("Can't calculate that: %s" % e)
        if isinstance(v, float):
            text = repr(round(v, 12)) if abs(v) < 1e15 else "%.12g" % v
            text = text[:-2] if text.endswith(".0") else text
        else:
            text = str(v)
        extra = ""
        if isinstance(v, int) and not isinstance(v, bool) and abs(v) >= 10:
            extra = "  (hex %s, binary %s)" % (hex(v), bin(v)) if abs(v) < 2 ** 64 else ""
        return Result("%s = %s%s" % (expr, text, extra), summary=text)


# ---- FileSearch: ranked search over a folder of documents --------------------------------------------------

WORD = re.compile(r"[A-Za-z0-9_]{2,}")
TEXT_EXT = {".md", ".txt", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".sh", ".json", ".yaml", ".yml", ".toml",
            ".html", ".css", ".csv", ".log", ".ini", ".cfg", ".conf", ".sql", ".tex", ".org", ".adoc", ""}


class FileSearch(Tool):
    name = "FileSearch"
    kind = "read"
    description = "Ranked keyword search over documents or code in a folder; returns file:lines."
    parameters = {"type": "object", "properties": {
        "query": {"type": "string", "description": "Words or a question"},
        "path": {"type": "string", "description": "Folder to search (default: the working directory)"},
        "glob": {"type": "string", "description": "Only files matching this glob, e.g. *.md"},
        "max_results": {"type": "integer", "description": "How many passages (default 6)"}}, "required": ["query"]}

    def specifier(self, args, ctx):
        return ctx.resolve(args.get("path") or ".")

    def run(self, args, ctx):
        q = need(args, "query")
        root = ctx.resolve(args.get("path") or ".")
        if not os.path.isdir(root):
            raise ToolError("%s isn't a folder." % root)
        terms = [w.lower() for w in WORD.findall(q)]
        stop = {"the", "and", "for", "are", "how", "what", "where", "does", "with", "that", "this", "from", "can", "you", "use"}
        terms = [t for t in terms if t not in stop] or terms
        if not terms:
            raise ToolError("Give some words to search for.")
        grx = glob_to_regex(args["glob"] if "/" in args.get("glob", "") else "**/" + args["glob"]) if args.get("glob") else None
        chunks, df, nfiles = [], Counter(), 0
        for p in walk_files(root, SKIP_DIRS):
            if os.path.splitext(p)[1].lower() not in TEXT_EXT or (grx and not grx.match(os.path.relpath(p, root).replace(os.sep, "/"))):
                continue
            try:
                if os.path.getsize(p) > 1024 * 1024:
                    continue
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            if util.is_binary(data):
                continue
            nfiles += 1
            if nfiles > 4000:
                break
            lines = data.decode("utf-8", "replace").split("\n")
            step, size = 20, 40
            for i in range(0, max(1, len(lines)), step):
                block = lines[i:i + size]
                words = [w.lower() for w in WORD.findall("\n".join(block))]
                if not words:
                    continue
                cnt = Counter(words)
                if not any(t in cnt for t in terms):
                    continue
                chunks.append((p, i + 1, block, cnt, len(words)))
                for t in set(terms):
                    if t in cnt:
                        df[t] += 1
                if i + size >= len(lines):
                    break
        if not chunks:
            return Result("Nothing in %s matches %r." % (util.shorten_path(root, ctx.cwd), q), summary="No matches")
        n = len(chunks)
        scored = []
        for p, ln, block, cnt, length in chunks:
            s = 0.0
            for t in set(terms):
                f = cnt.get(t, 0)
                if f:
                    idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                    s += idf * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * length / 120.0))
            if sum(1 for t in set(terms) if t in cnt) == len(set(terms)):
                s *= 1.5
            scored.append((s, p, ln, block))
        scored.sort(key=lambda x: -x[0])
        out, seen = [], {}
        limit = max(1, min(int(args.get("max_results") or 6), 15))
        for s, p, ln, block in scored:
            if seen.get(p, 0) >= 2:
                continue
            seen[p] = seen.get(p, 0) + 1
            best = max(range(len(block)), key=lambda k: sum(1 for t in terms if t in block[k].lower()))
            a = max(0, best - 2)
            snip = "\n".join("    %d: %s" % (ln + k, block[k][:200]) for k in range(a, min(len(block), a + 6)))
            out.append("%s:%d-%d  (score %.1f)\n%s" % (util.shorten_path(p, ctx.cwd), ln, ln + len(block) - 1, s, snip))
            if len(out) >= limit:
                break
        return Result("\n\n".join(out), summary="%d passage%s from %d files" % (len(out), "" if len(out) == 1 else "s", nfiles))


# ---- Memory --------------------------------------------------------------------------------------------------

class Memory(Tool):
    name = "Memory"
    kind = "edit"
    read_only = False
    description = "Save, list or remove facts for future conversations (kept in YARDCODE.md)."
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["add", "list", "remove"]},
        "text": {"type": "string", "description": "The fact (add) or part of it (remove)"},
        "scope": {"type": "string", "enum": ["project", "user"], "description": "project: this folder's YARDCODE.md; user: all projects"}}, "required": ["action"]}

    def specifier(self, args, ctx):
        return (args.get("scope") or "project") + ":" + (args.get("text") or args.get("action", ""))

    def run(self, args, ctx):
        action, scope = need(args, "action"), (args.get("scope") or "project")
        if scope not in ("project", "user"):
            raise ToolError("scope must be project or user.")
        if action == "list":
            lines = []
            for sc in ("user", "project"):
                path, items = mem.notes(sc, ctx.cwd)
                lines += ["[%s] %s" % (sc, i) for i in items]
            return Result("\n".join(lines) or "(no saved notes)", summary="%d notes" % len(lines))
        text = need(args, "text").strip()
        if action == "add":
            path, added = mem.add_note(text, scope, ctx.cwd)
            return Result(("Saved to %s." if added else "Already saved in %s.") % path, summary="Saved" if added else "Already saved")
        if action == "remove":
            path, n = mem.remove_note(text, scope, ctx.cwd)
            if n != 1:
                raise ToolError("%d notes match %r; give more of the text so exactly one does." % (n, text))
            return Result("Removed from %s." % path, summary="Removed")
        raise ToolError("action must be add, list or remove.")


# ---- AskUserQuestion / ExitPlanMode -------------------------------------------------------------------------------

class AskUserQuestion(Tool):
    name = "AskUserQuestion"
    kind = "read"
    description = "Ask the user a question with 2-4 choices when you need a decision."
    parameters = {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "object", "properties": {
        "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, "required": ["question", "options"]}}}, "required": ["questions"]}

    def run(self, args, ctx):
        qs = args.get("questions")
        if not isinstance(qs, list) or not qs:
            raise ToolError("questions must be a list.")
        fe = ctx.frontend
        if fe is None or not hasattr(fe, "ask_user") or not getattr(fe, "interactive", False):
            raise ToolError("The user can't be asked right now (non-interactive run). Make a sensible assumption, say so, and carry on.")
        for q in qs:
            q["options"] = [o if isinstance(o, dict) else {"label": str(o)} for o in (q.get("options") or [])]
        answers = fe.ask_user(qs)
        if answers is None:
            raise ToolError("The user skipped the question. Carry on with your best judgement.")
        return Result("\n".join("%s -> %s" % (q, a) for q, a in answers.items()), summary="Answered %d question%s" % (len(answers), "" if len(answers) == 1 else "s"))


class ExitPlanMode(Tool):
    name = "ExitPlanMode"
    kind = "read"
    description = "Plan mode only: present the finished plan for approval."
    parameters = {"type": "object", "properties": {"plan": {"type": "string", "description": "The plan, in markdown"}}, "required": ["plan"]}

    def run(self, args, ctx):
        plan = need(args, "plan")
        fe = ctx.frontend
        if fe is None or not hasattr(fe, "approve_plan"):
            raise ToolError("Plans can't be approved in this mode.")
        decision = fe.approve_plan(plan)
        if decision == "no" or decision is None:
            return Result("The user did not approve the plan. Revise it based on what they say next, and stay in plan mode.", error=True, summary="Not approved")
        if ctx.agent is not None:
            ctx.agent.perms.mode = "acceptEdits" if decision == "auto" else "default"
        return Result("The plan was approved%s. You can now make changes. Start with the first step." % (" (edits are accepted automatically)" if decision == "auto" else ""),
                      summary="Plan approved")


# ---- Model: loading models on the nodeyard cluster ----------------------------------------------------------------

class Model(Tool):
    name = "Model"
    kind = "cluster"
    read_only = False
    description = "Manage the cluster's model: status, list, search, files, download, load, unload (ask the user before load)."
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["status", "list", "search", "files", "download", "load", "unload"]},
        "model": {"type": "string", "description": "load/unload: the model file or name from list"},
        "query": {"type": "string", "description": "search: words to look for"},
        "repo": {"type": "string", "description": "files/download: owner/name of the Hugging Face repo"},
        "file": {"type": "string", "description": "download: the .gguf file name"}}, "required": ["action"]}

    def specifier(self, args, ctx):
        return "%s:%s" % (args.get("action", ""), args.get("model") or args.get("repo") or args.get("query") or "")

    def run(self, args, ctx):
        api = ctx.modelapi
        if api is None or not api.available:
            raise ToolError("No nodeyard dashboard is configured. Set it with: yardcode config control_url http://HOST:9092")
        action = need(args, "action")
        try:
            if action == "status":
                return Result(fmt_status(api.status()), summary="Status")
            if action == "list":
                return Result(fmt_models(api.models()), summary="Models")
            if action == "search":
                return Result(fmt_search(api.search(need(args, "query"))), summary="Search")
            if action == "files":
                return Result(fmt_files(api.files(need(args, "repo"))), summary="Files")
            if action == "download":
                job = api.download(need(args, "repo"), need(args, "file"))
                return Result("Download started (task %s). It continues in the background; check with action=list." % job.get("job"), summary="Download started")
            if action == "unload":
                job = api.unload(args.get("model"))
                lines = []
                api.follow(job["job"], lines.append, timeout=300)
                return Result("\n".join(lines[-12:]) or "Unloaded.", summary="Unloaded")
            if action == "load":
                return self._load(api, need(args, "model"), ctx)
        except ModelAPIError as e:
            raise ToolError(str(e))
        raise ToolError("Unknown action %r." % action)

    def _load(self, api, model, ctx):
        job = api.load(model)
        lines = []
        state = api.follow(job["job"], lines.append, timeout=600, stop=ctx.abort)
        if state not in ("ok", "success"):
            raise ToolError("Loading %s failed (%s):\n%s" % (model, state, "\n".join(lines[-10:])))
        return Result("Switched to %s. The cluster is loading it now; it can take a few minutes before it answers.\n%s" % (model, "\n".join(lines[-6:])),
                      summary="Loading %s" % model)


def fmt_status(st):
    m = st.get("model") or {}
    return "State: %s\nModel: %s\nFile: %s\nContext: %s" % (m.get("state", "unknown"), m.get("alias", "-"), m.get("file", "-"), m.get("ctx", "-"))


def fmt_models(d):
    rows = []
    for m in d.get("models", []):
        flag = "LOADED" if m.get("loaded") or m.get("active") else "      "
        size = util.human_bytes(m.get("size", 0)) if m.get("size") else ""
        rows.append("%s  %-9s %-60s %10s  %s" % (flag, m.get("kind", ""), m.get("name") or m.get("file", ""), size, ",".join(m.get("nodes", []))))
    for dl in d.get("downloads", []):
        rows.append("DOWNLOADING  %s  %s" % (dl.get("file", ""), dl.get("progress", "")))
    return "\n".join(rows) or "(no models downloaded)"


def fmt_search(d):
    return "\n".join("%s  (%s downloads, %s likes)" % (r.get("id"), r.get("downloads"), r.get("likes")) for r in d.get("results", [])) or "(nothing found)"


def fmt_files(d):
    return "\n".join("%-70s %9s  %s" % (f.get("file"), util.human_bytes(f.get("size", 0)), f.get("fits", "")) for f in d.get("files", [])) or "(no GGUF files)"


MISC_TOOLS = [TodoWrite, Calculator, FileSearch, Memory, AskUserQuestion, ExitPlanMode, Model]
_ = Counter
