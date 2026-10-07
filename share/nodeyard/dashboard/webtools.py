"""Web research for the dashboard chat: search the web, read the best pages, hand the text to the model.

Uses yardcode's web tools (no API keys; private network addresses are never fetched). Without yardcode next to nodeyard this
answers "not installed". The demo returns canned results and never touches the internet.
"""
import os
import sys
import threading
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "..", "..", "yardcode", "src"), os.path.join(HERE, "yardcode")):
    if os.path.isdir(os.path.join(cand, "yardcode")):
        sys.path.insert(0, os.path.abspath(cand))
        break

MAX_PAGES, PAGE_CHARS, TOTAL_CHARS = 3, 3000, 9000
_busy = threading.BoundedSemaphore(2)


class WebError(Exception):
    pass


def research(query, demo=False):
    query = " ".join(str(query or "").split())[:300]
    if not query:
        raise WebError("Nothing to search for.")
    if demo:
        return {"ok": True, "text": "[1] Demo result (https://example.com/demo)\nThis is a demo: nothing was searched.", "sources": [{"title": "Demo result", "url": "https://example.com/demo"}]}
    try:
        from yardcode import config
        from yardcode.tools import base, web
    except ImportError:
        raise WebError("The web tools need yardcode, which isn't installed next to nodeyard on this server.")
    if not _busy.acquire(blocking=False):
        raise WebError("Another web search is running. Try again in a moment.")
    try:
        settings = config.Settings("/", overrides={"web": {"allow_private": False, "timeout": 15}}, environ={})
        ctx = base.Context(settings, "/")
        try:
            results, engine = web.web_search(query, 6, ctx)
        except base.ToolError as e:
            raise WebError(str(e))
        keep, seen = [], set()
        for r in results:
            if r["url"].startswith("http") and r["url"] not in seen:
                seen.add(r["url"])
                keep.append(r)
            if len(keep) >= 5:
                break
        parts, budget = [], TOTAL_CHARS
        for i, r in enumerate(keep, 1):
            text = ""
            if i <= MAX_PAGES:
                try:
                    status, headers, body, final = web.http_fetch(r["url"], timeout=12, max_bytes=1500000)
                    if status < 400 and "html" in headers.get("content-type", "html").lower():
                        _, text = web.html_to_text(web.decode_body(body, headers), final)
                except base.ToolError:
                    text = ""
            piece = "[%d] %s (%s)\n%s" % (i, r["title"], r["url"], (text[:min(PAGE_CHARS, budget)] if text else r["snippet"]).strip())
            budget -= len(piece)
            parts.append(piece)
            if budget <= 0:
                break
        sources = [{"title": r["title"], "url": r["url"]} for r in keep[:len(parts)]]
        return {"ok": True, "text": "Search engine: %s\n\n%s" % (engine, "\n\n".join(parts)), "sources": sources}
    finally:
        _busy.release()


ALLOWED = ("WebSearch", "WebFetch", "Wikipedia", "Arxiv", "Weather")
_tool_slots = threading.BoundedSemaphore(3)


def run_tool(name, args):
    """One of yardcode's web tools, run from THIS machine's internet connection (so a client behind a school or work filter, or on a
    poor connection, searches through the server). Private network addresses are never fetched."""
    if name not in ALLOWED or not isinstance(args, dict):
        raise WebError("That isn't a web tool I can run.")
    try:
        from yardcode import config
        from yardcode.tools import base, web
    except ImportError:
        raise WebError("The web tools need yardcode, which isn't installed next to nodeyard on this server.")
    settings = config.Settings("/", overrides={"web": {"allow_private": False, "timeout": 20}}, environ={})
    ctx = base.Context(settings, "/")
    clean = {k: v for k, v in args.items() if not str(k).startswith("_")}
    try:
        r = getattr(web, name)().run(clean, ctx)
    except base.ToolError as e:
        raise WebError(str(e))
    return {"text": r.text, "summary": r.summary, "preview": r.preview, "error": bool(r.error)}


def register(ctx, args):
    demo = bool(getattr(args, "demo", False))

    def tool(h, body):
        name, a = str(body.get("tool", "")), body.get("args")
        if name not in ALLOWED or not isinstance(a, dict):
            return h._json({"ok": False, "error": "Say which web tool (%s) and its arguments." % ", ".join(ALLOWED)}, 400)
        if demo:
            return h._json({"ok": True, "text": "(demo) %s ran on the server; nothing was fetched." % name, "summary": "demo result", "preview": ["demo"], "error": False})
        if not _tool_slots.acquire(blocking=False):
            return h._json({"ok": False, "error": "The server is busy with other searches. Try again in a moment."}, 429)
        try:
            h._json(dict(run_tool(name, a), ok=True))
        except WebError as e:
            h._json({"ok": False, "error": str(e)}, 502)
        finally:
            _tool_slots.release()

    ctx.post_routes["/api/v1/web/tool"] = tool


_ = urllib.parse
