"""Whether the AI may use web search (AI › Permissions › Web search): "auto" (it may, when it helps) or "off".

Kept with the dashboard's settings and read on every request, so a change applies to the next message without a
restart. It is enforced on this server, not just in the page or the prompt:
  - /api/ai/web (the chat's "search first") refuses;
  - the AI with skills gets no search tools (web, Wikipedia, arXiv) and yardcode is told they are disallowed; when its
    Python or shell tools are on, their sandbox may only reach this machine and the model (no internet), so it can't
    search through them either;
  - Research (web reports) refuses to start;
  - the native apps' chat API drops their browser search / open-page tools;
  - your registered search APIs are left out of the AI's tools and can't be called;
  - every chat answer gets a system note: no searching, and never claim to have checked anything online.
A single message can also skip search while it's on ("/nosearch …" in the chat): the same rules, for that message only.

  GET  /api/ai/websearch     {"mode": "auto"|"off"}
  POST /api/ai/websearch     {"mode": "auto"|"off"}
"""
KEY = "ai_web_search"
MODES = ("auto", "off")
SEARCH_PLUGINS = ("web", "wikipedia", "arxiv")
SEARCH_TOOLS = ("WebSearch", "WebFetch", "Wikipedia", "Arxiv")
WEB_CHAT_TOOLS = ("browser_search", "browser_open", "page_open")
NOTE = ("Web search is turned off for this conversation: answer from the conversation, any attached files and what you already know. "
        "Don't search or browse, and never say you checked or looked anything up online. If the answer needs current information, "
        "say that you can't check it right now.")


def mode(ctx):
    s = getattr(ctx, "settings", None)
    try:
        v = (s.prefs() if s else {}).get(KEY)
    except Exception:  # noqa: BLE001 -- unreadable settings: the default
        v = None
    return v if v in MODES else "auto"


def allowed(ctx, body=None):
    """May this request search? (the dashboard's setting, and the message's own "no search")"""
    if body is not None and body.get("no_search") is True:
        return False
    return mode(ctx) != "off"


def with_note(messages):
    """The chat's messages with the no-search note added to its system message (or as a new first one)."""
    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system" and isinstance(out[0].get("content"), str):
        out[0]["content"] = out[0]["content"] + "\n\n" + NOTE
    else:
        out.insert(0, {"role": "system", "content": NOTE})
    return out


def register(ctx, args):
    def get(h, q):
        h._json({"ok": True, "mode": mode(ctx)})

    def post(h, body):
        m = body.get("mode")
        if m not in MODES:
            return h._json({"ok": False, "error": "Pick auto or off."}, 400)
        if h._forwarded() is not None:
            return h._json({"ok": False, "error": "Change this over Tailscale or your own network, not through public access."}, 403)
        s = getattr(ctx, "settings", None)
        if s is None:
            return h._json({"ok": False, "error": "Settings aren't available."}, 503)
        with s.lock:
            prefs = s.prefs()
            prefs[KEY] = m
            s._save_prefs(prefs)
        h._json({"ok": True, "mode": m})

    ctx.get_routes["/api/ai/websearch"] = get
    ctx.post_routes["/api/ai/websearch"] = post
