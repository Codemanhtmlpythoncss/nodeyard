"""Research Mode: answer a question from sources that were actually read, with citations that can be checked.

A session runs in the background on this server (so the dashboard, the Mac app and yardcode can all start and follow
one, and a closed page doesn't stop it):

  1. plan      the model splits the question into a few web searches (or the question itself is searched)
  2. search    each search runs from this server's internet connection; results are de-duplicated
  3. read      the best pages are fetched; the passages that match the question are kept, with when they were read
  4. write     the model writes a report from those passages only, citing them as [n]
  5. check     every [n] is checked against the sources that were really read; the source list is written by this
               code, not by the model, so it can't contain made-up links

Sessions are saved under the dashboard's state folder (research/ID.json) and can be read again or exported as Markdown.
"""
import json
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.parse

DEPTHS = {"quick": (1, 3), "standard": (3, 6), "deep": (4, 10)}   # searches, pages read
MAX_RUNNING = 2
KEEP_SESSIONS = 60
EXCERPT_CHARS = 1400
STOPWORDS = set("the a an and or of to in on for with is are was were be by as at from that this what which who how why when "
                "does do did it its into about than then there their they them can could should would will".split())


class ResearchError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


class Cancelled(Exception):
    pass


def normal_url(url):
    """The same page with or without a #fragment, trailing slash or tracking parameters counts once."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid"))])
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", query, ""))


def keywords(text):
    return [w for w in re.findall(r"[a-z0-9][a-z0-9.+#-]{1,}", text.lower()) if w not in STOPWORDS][:24]


def best_passages(text, question, limit=EXCERPT_CHARS):
    """The paragraphs of a page that share the most words with the question, in page order."""
    words = set(keywords(question))
    paras = [p.strip() for p in re.split(r"\n\s*\n|(?<=[.!?])\s{2,}", text) if len(p.strip()) > 40]
    if not paras:
        return text[:limit].strip()
    scored = sorted(range(len(paras)), key=lambda i: -len(words & set(keywords(paras[i]))))
    keep, total = set(), 0
    for i in scored:
        if total >= limit:
            break
        keep.add(i)
        total += len(paras[i])
    out = "\n\n".join(paras[i] for i in sorted(keep))
    return out[:limit].rstrip() + ("…" if len(out) > limit else "")


def parse_queries(text, question, limit):
    """The model's search plan: a JSON list of strings, else one query per line; the question itself as a fallback."""
    found = []
    match = re.search(r"\[[\s\S]*\]", text or "")
    if match:
        try:
            value = json.loads(match.group(0))
            found = [str(q) for q in value if isinstance(q, str)]
        except ValueError:
            found = []
    if not found:
        found = [re.sub(r"^[\s\-*\d.)]+", "", line).strip(' "') for line in (text or "").splitlines()]
    clean, seen = [], set()
    for q in found:
        q = " ".join(q.split())[:200]
        if len(q) >= 3 and q.lower() not in seen:
            seen.add(q.lower())
            clean.append(q)
    return clean[:limit] or [" ".join(question.split())[:200]]


def check_citations(report, sources):
    """Which [n] the report uses, and which point at no source that was actually read."""
    read = {s["n"] for s in sources if s["status"] == "read"}
    listed = {s["n"] for s in sources}
    used = set()
    for group in re.findall(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]", report):
        for part in re.split(r"\s*,\s*", group):
            if re.match(r"^\d+\s*[–-]\s*\d+$", part):
                a, b = [int(x) for x in re.split(r"\s*[–-]\s*", part)]
                used.update(range(a, b + 1) if 0 < b - a < 20 else (a, b))
            elif part.strip().isdigit():
                used.add(int(part))
    return {"used": sorted(used), "invalid": sorted(used - listed), "unread": sorted((used & listed) - read), "uncited": not used}


class Researcher:
    def __init__(self, backend, state_dir, search=None, fetch=None, complete=None, clock=time.time, demo=False):
        self.backend = backend
        self.dir = os.path.join(state_dir, "research")
        self.search = search or self._web_search
        self.fetch = fetch or self._web_fetch
        self.complete = complete or self._model_complete
        self.clock = clock
        self.demo = demo
        self.lock = threading.Lock()
        self.sessions = {}
        self.cancelled = set()
        self.conns = {}
        self._load()

    # -- storage ---------------------------------------------------------------------------------------------

    def _load(self):
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return
        for name in names:
            if not re.match(r"^[0-9a-f]{12}\.json$", name):
                continue
            try:
                with open(os.path.join(self.dir, name), "r", encoding="utf-8") as f:
                    s = json.load(f)
                if s.get("status") not in ("done", "failed", "cancelled"):
                    s["status"], s["error"] = "failed", "The dashboard restarted while this ran. Start it again."
                self.sessions[s["id"]] = s
            except (OSError, ValueError, KeyError):
                continue

    def _save(self, s):
        try:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".r-", dir=self.dir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(s, f)
            os.replace(tmp, os.path.join(self.dir, s["id"] + ".json"))
        except OSError:
            pass   # the session still shows from memory

    def _prune(self):
        done = sorted((s for s in self.sessions.values() if s["status"] in ("done", "failed", "cancelled")), key=lambda s: s["created"])
        for s in done[:max(0, len(self.sessions) - KEEP_SESSIONS)]:
            self.sessions.pop(s["id"], None)
            try:
                os.unlink(os.path.join(self.dir, s["id"] + ".json"))
            except OSError:
                pass

    # -- the routes' operations ----------------------------------------------------------------------------------

    def start(self, question, target, depth="standard"):
        question = " ".join(str(question or "").split())
        if not 3 <= len(question) <= 1000:
            raise ResearchError("Ask a question of 3 to 1000 characters.")
        target = str(target or "split")[:240]
        if target != "split" and not target.startswith("ollama:"):
            raise ResearchError("Choose a model for the report.")
        if depth not in DEPTHS:
            raise ResearchError("Depth must be quick, standard or deep.")
        with self.lock:
            if sum(1 for s in self.sessions.values() if s["status"] not in ("done", "failed", "cancelled")) >= MAX_RUNNING:
                raise ResearchError("Two research sessions are already running. Wait for one to finish or cancel it.", 429)
            sid = secrets.token_hex(6)
            s = {"id": sid, "question": question, "target": target, "depth": depth, "status": "planning", "created": self.clock(),
                 "updated": self.clock(), "steps": [], "queries": [], "sources": [], "report": "", "citations": None, "error": ""}
            self.sessions[sid] = s
            self._prune()
        threading.Thread(target=self._run, args=(sid,), name="nodeyard-research-" + sid, daemon=True).start()
        return sid

    def view(self, sid):
        with self.lock:
            s = self.sessions.get(sid)
            return json.loads(json.dumps(s)) if s else None

    def listing(self):
        with self.lock:
            return [{"id": s["id"], "question": s["question"], "status": s["status"], "created": s["created"], "sources": len(s["sources"])}
                    for s in sorted(self.sessions.values(), key=lambda s: -s["created"])]

    def cancel(self, sid):
        with self.lock:
            s = self.sessions.get(sid)
            if not s:
                raise ResearchError("No such research session.", 404)
            if s["status"] in ("done", "failed", "cancelled"):
                raise ResearchError("That research session has already finished.", 409)
            self.cancelled.add(sid)
            conn = self.conns.get(sid)
        if conn is not None:
            try:
                conn.close()   # stops the model working on it
            except Exception:  # noqa: BLE001
                pass
        return True

    def markdown(self, sid):
        s = self.view(sid)
        if not s:
            raise ResearchError("No such research session.", 404)
        lines = ["# %s" % s["question"], "", "_Research by Nodeyard, %s. Status: %s._" % (time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(s["created"])), s["status"]), ""]
        if s["report"]:
            lines += [s["report"].strip(), ""]
        if s["error"]:
            lines += ["**Error:** %s" % s["error"], ""]
        lines += ["## Sources", ""]
        for src in s["sources"]:
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(src["retrieved_at"])) if src.get("retrieved_at") else "not retrieved"
            state = ("read " + when) if src["status"] == "read" else (("could not be read (%s)" % src["error"]) if src.get("error") else "search result only (not read)")
            lines.append("%d. [%s](%s) — %s" % (src["n"], src["title"] or src["url"], src["url"], state))
        c = s.get("citations") or {}
        if c.get("invalid") or c.get("unread") or c.get("uncited"):
            lines += ["", "## Citation check", ""]
            if c.get("uncited"):
                lines.append("- The report cites no sources: treat it as unverified.")
            if c.get("invalid"):
                lines.append("- Cited numbers that match no source: %s." % ", ".join("[%d]" % n for n in c["invalid"]))
            if c.get("unread"):
                lines.append("- Cited sources that could not be read (only their search snippet was available): %s." % ", ".join("[%d]" % n for n in c["unread"]))
        return "\n".join(lines) + "\n"

    # -- the pipeline -------------------------------------------------------------------------------------------

    def _step(self, sid, kind, text, ok=True, status=None):
        with self.lock:
            s = self.sessions[sid]
            if sid in self.cancelled:
                raise Cancelled()
            s["steps"].append({"time": self.clock(), "kind": kind, "text": text, "ok": ok})
            if status:
                s["status"] = status
            s["updated"] = self.clock()
            snapshot = json.loads(json.dumps(s))
        self._save(snapshot)

    def _run(self, sid):
        try:
            self._pipeline(sid)
        except Cancelled:
            with self.lock:
                s = self.sessions[sid]
                s["status"], s["updated"] = "cancelled", self.clock()
                s["steps"].append({"time": self.clock(), "kind": "cancel", "text": "Cancelled.", "ok": False})
                snapshot = json.loads(json.dumps(s))
            self._save(snapshot)
        except Exception as e:  # noqa: BLE001 -- the page must show why, never a stuck "running"
            with self.lock:
                s = self.sessions[sid]
                s["status"], s["error"], s["updated"] = "failed", str(e)[:500] or e.__class__.__name__, self.clock()
                s["steps"].append({"time": self.clock(), "kind": "error", "text": s["error"], "ok": False})
                snapshot = json.loads(json.dumps(s))
            self._save(snapshot)
        finally:
            with self.lock:
                self.cancelled.discard(sid)
                self.conns.pop(sid, None)

    def _pipeline(self, sid):
        s = self.view(sid)
        question, target = s["question"], s["target"]
        n_queries, n_pages = DEPTHS[s["depth"]]

        # 1. plan
        queries = [question]
        if n_queries > 1:
            self._step(sid, "plan", "Asking the model how to search for this…", status="planning")
            try:
                plan = self.complete(sid, target, [
                    {"role": "system", "content": "You plan web searches. Reply with a JSON array of short search queries and nothing else."},
                    {"role": "user", "content": "Question: %s\n\nWrite %d different web search queries that together would find the facts needed "
                                                "to answer it well, preferring official and primary sources. JSON array only." % (question, n_queries)},
                ], 160)
                queries = parse_queries(plan, question, n_queries)
                self._step(sid, "plan", "Searches: " + "; ".join(queries))
            except (Cancelled, KeyboardInterrupt):
                raise
            except Exception as e:  # noqa: BLE001 -- a plan is nice to have; the question itself still works
                self._step(sid, "plan", "The model couldn't plan the searches (%s); searching for the question itself." % e, ok=False)
        with self.lock:
            self.sessions[sid]["queries"] = queries

        # 2. search
        self._step(sid, "search", "Searching the web…", status="searching")
        found, seen = [], set()
        per_host = {}
        for q in queries:
            try:
                results, engine = self.search(q, 8)
            except Exception as e:  # noqa: BLE001
                self._step(sid, "search", "Search failed for “%s”: %s" % (q, e), ok=False)
                continue
            self._step(sid, "search", "“%s”: %d result%s from %s" % (q, len(results), "" if len(results) == 1 else "s", engine))
            for r in results:
                url = str(r.get("url") or "")
                if not url.startswith(("http://", "https://")):
                    continue
                key = normal_url(url)
                host = urllib.parse.urlsplit(key).netloc
                if key in seen or per_host.get(host, 0) >= 2:
                    continue
                seen.add(key)
                per_host[host] = per_host.get(host, 0) + 1
                found.append({"url": url, "title": str(r.get("title") or "")[:200], "snippet": str(r.get("snippet") or "")[:400], "query": q, "engine": engine})
        if not found:
            raise ResearchError("No search worked, so there is nothing to read. Check the server's internet connection (Settings › Web) and try again.", 502)

        # 3. read (the first n_pages that can be read; the rest stay as search results)
        self._step(sid, "read", "Reading up to %d pages…" % n_pages, status="reading")
        sources = []
        for r in found:
            if sum(1 for x in sources if x["status"] == "read") >= n_pages or len(sources) >= n_pages + 4:
                break
            src = {"n": len(sources) + 1, "url": r["url"], "title": r["title"], "query": r["query"], "engine": r["engine"], "snippet": r["snippet"],
                   "retrieved_at": None, "status": "snippet", "excerpt": "", "error": ""}
            try:
                title, text = self.fetch(r["url"])
                src["retrieved_at"] = self.clock()
                if text and len(text.strip()) > 80:
                    src.update(status="read", excerpt=best_passages(text, question), title=(title or r["title"])[:200])
                else:
                    src["error"] = "the page had no readable text"
            except (Cancelled, KeyboardInterrupt):
                raise
            except Exception as e:  # noqa: BLE001
                src["error"] = str(e)[:200] or "could not be read"
            sources.append(src)
            with self.lock:
                self.sessions[sid]["sources"] = json.loads(json.dumps(sources))
            self._step(sid, "read", ("Read [%d] %s" % (src["n"], src["title"] or src["url"])) if src["status"] == "read" else
                       "Couldn't read [%d] %s: %s" % (src["n"], src["url"], src["error"]), ok=src["status"] == "read")
        if not any(x["status"] == "read" for x in sources):
            raise ResearchError("None of the pages could be read (blocked, too large or not text). The search results are listed below.", 502)

        # 4. write
        self._step(sid, "write", "Writing the report from %d sources (a large model on CPU nodes can take several minutes)…" % sum(1 for x in sources if x["status"] == "read"), status="writing")
        material = "\n\n".join("[%d] %s\nURL: %s\n%s" % (x["n"], x["title"], x["url"], x["excerpt"] if x["status"] == "read" else "(search snippet only) " + x["snippet"])
                               for x in sources)
        report = self.complete(sid, target, [
            {"role": "system", "content": "You write careful research reports. Use ONLY the numbered sources the user gives you. After every factual "
                                          "claim, cite its source as [n]. Never cite a number that isn't listed and never invent facts, quotes or links. "
                                          "If the sources disagree, say so. If they don't answer the question, say that plainly."},
            {"role": "user", "content": "Question: %s\n\nSources (passages read just now):\n\n%s\n\nWrite the report in Markdown with these sections: "
                                        "## Answer (2-5 sentences), ## Key findings (bullets, each cited), ## Where sources disagree (or 'No conflicts "
                                        "found'), ## Not verified (what the sources don't establish). Do not add a source list; it is added for you." % (question, material)},
        ], 1100)
        report = re.sub(r"<think>[\s\S]*?</think>", "", report or "").strip()
        report = re.split(r"\n#+\s*(Sources|References)\s*\n", report)[0].strip()   # the real list is ours
        if not report:
            raise ResearchError("The model returned an empty report.", 502)

        # 5. check
        citations = check_citations(report, sources)
        with self.lock:
            s = self.sessions[sid]
            s["report"], s["citations"] = report, citations
        problems = []
        if citations["uncited"]:
            problems.append("the report cites no sources, so treat it as unverified")
        if citations["invalid"]:
            problems.append("it cites numbers that match no source: " + ", ".join("[%d]" % n for n in citations["invalid"]))
        if citations["unread"]:
            problems.append("it cites sources that couldn't be read: " + ", ".join("[%d]" % n for n in citations["unread"]))
        self._step(sid, "check", ("Citation check: " + "; ".join(problems) + ".") if problems else
                   "Citation check: every citation points at a source that was read (%d cited)." % len(citations["used"]), ok=not problems, status="done")

    # -- the real search, fetch and model calls -----------------------------------------------------------------------

    def _web_search(self, query, n):
        if self.demo:
            return ([{"title": "Demo source %d for %s" % (i, query[:40]), "url": "https://example.com/demo/%d?q=%s" % (i, urllib.parse.quote(query[:40])),
                      "snippet": "A demo search result: nothing was searched."} for i in (1, 2)], "demo")
        import webtools   # (puts yardcode on the path)
        try:
            from yardcode import config
            from yardcode.tools import base, web
        except ImportError:
            raise ResearchError("Research needs yardcode's web tools, which aren't installed next to nodeyard on this server.", 501)
        _ = webtools
        settings = config.Settings("/", overrides={"web": {"allow_private": False, "timeout": 15}}, environ={})
        try:
            return web.web_search(query, n, base.Context(settings, "/"))
        except base.ToolError as e:
            raise ResearchError(str(e), 502)

    def _web_fetch(self, url):
        if self.demo:
            return "Demo page", ("This demo page stands in for a real source. In demo mode nothing is fetched from the internet, "
                                 "so the report below only shows how Research Mode lays out findings and citations.\n\n" * 2)
        import webtools
        from yardcode.tools import base, web
        _ = webtools
        try:
            status, headers, body, final = web.http_fetch(url, timeout=15, max_bytes=2000000)
        except base.ToolError as e:
            raise ResearchError(str(e), 502)
        if status >= 400:
            raise ResearchError("HTTP %d" % status, 502)
        ctype = headers.get("content-type", "text/html").lower()
        if "html" not in ctype and not ctype.startswith("text/"):
            raise ResearchError("not a web page (%s)" % ctype.split(";")[0], 415)
        text_body = web.decode_body(body, headers)
        if "html" in ctype:
            title, text = web.html_to_text(text_body, final)
            return title, text
        return "", text_body

    def _model_complete(self, sid, target, messages, max_tokens):
        if self.demo:
            if "JSON array" in messages[-1]["content"]:
                return '["%s", "%s official documentation"]' % (messages[-1]["content"].split("\n")[0][10:60], messages[-1]["content"].split("\n")[0][10:50])
            return ("## Answer\nThis is a demo report: the sources are placeholders, so nothing here is a real finding [1].\n\n"
                    "## Key findings\n- Research Mode lists every source it read with the time it was read [1][2].\n\n"
                    "## Where sources disagree\nNo conflicts found.\n\n## Not verified\nEverything: this is the demo.")
        conns = {}

        def on_conn(conn):
            with self.lock:
                self.conns[sid] = conn
            conns["c"] = conn
        conn, resp = self.backend.open_chat(target, {"messages": messages, "temperature": 0.2, "max_tokens": max_tokens}, on_conn=on_conn)
        text, deadline = [], self.clock() + 25 * 60
        try:
            for line in iter(resp.readline, b""):
                if sid in self.cancelled:
                    raise Cancelled()
                if self.clock() > deadline:
                    raise ResearchError("The model took more than 25 minutes; stopped.", 504)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if chunk.get("error"):
                    err = chunk["error"]
                    raise ResearchError("The model reported an error: %s" % (err.get("message") if isinstance(err, dict) else err), 502)
                for ch in chunk.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        text.append(piece)
        except OSError:
            if sid in self.cancelled:
                raise Cancelled()
            raise
        finally:
            conn.close()
            with self.lock:
                self.conns.pop(sid, None)
        if sid in self.cancelled:
            raise Cancelled()
        return "".join(text)


def register(ctx, args):
    backend = getattr(ctx, "ai", None)
    if backend is None:
        return None
    state_dir = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
    demo = bool(getattr(args, "demo", False))
    if demo:
        state_dir = os.path.join(tempfile.gettempdir(), "nodeyard-demo-dashboard-%d" % os.getuid())
    r = Researcher(backend, state_dir, demo=demo)
    ctx.research = r

    def fail(h, e):
        h._json({"ok": False, "error": str(e)}, getattr(e, "code", 400))

    def start(h, body):
        try:
            h._json({"ok": True, "id": r.start(body.get("question"), body.get("target"), str(body.get("depth") or "standard"))})
        except ResearchError as e:
            fail(h, e)

    def get(h, q):
        sid = q.get("id", [""])[0]
        if not sid:
            return h._json({"ok": True, "sessions": r.listing()})
        v = r.view(sid)
        if v is None:
            return h._json({"ok": False, "error": "No such research session."}, 404)
        h._json(dict(v, ok=True))

    def cancel(h, body):
        try:
            r.cancel(str(body.get("id") or ""))
            h._json({"ok": True})
        except ResearchError as e:
            fail(h, e)

    def export(h, q):
        try:
            text = r.markdown(q.get("id", [""])[0])
        except ResearchError as e:
            return fail(h, e)
        h._send(200, text.encode("utf-8"), "text/markdown; charset=utf-8", {"Content-Disposition": 'attachment; filename="research.md"'})

    for prefix in ("/api/ai/research", "/api/v1/research"):
        ctx.get_routes.update({prefix: get, prefix + "/export": export})
        ctx.post_routes.update({prefix: start, prefix + "/cancel": cancel})
    return r
