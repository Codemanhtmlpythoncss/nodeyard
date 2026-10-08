"""Sharing chats with the nodeyard dashboard: each finished turn is saved on the server, so the conversation shows in the dashboard's
AI tab (and can be continued there), and any chat in the AI tab that came from yardcode can be continued here with /chats.

Needs the dashboard's control API (the server API key); it never gets in the way: when the server can't be reached the chat simply
isn't shared this time. Turn it off with: yardcode config sync_chats false
"""
import socket
import threading

from .modelapi import ModelAPIError
from .session import Session

LOCK = threading.Lock()


def remote_id(session):
    return session.remote_id or ("yc-" + session.id)


def portable(messages):
    """The conversation as the dashboard stores it (OpenAI chat format; bookkeeping keys folded into plain ones)."""
    out = []
    for m in messages:
        d = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("_synthetic"):
            d["synthetic"] = str(m["_synthetic"])
        if m.get("tool_calls"):
            d["tool_calls"] = m["tool_calls"]
        if m["role"] == "tool":
            d["tool_call_id"] = m.get("tool_call_id", "")
            if m.get("_name"):
                d["name"] = m["_name"]
        out.append(d)
    return out


def enabled(app):
    return bool(app.settings.get("sync_chats", True) and app.modelapi.available and app.settings.get("api_key") and app.persist)


def push(app):
    """Send what is new in this conversation to the server. Returns True when the server has it."""
    if not enabled(app):
        return False
    ses = app.session
    if not ses.messages:
        return False
    with LOCK:
        meta = {"id": remote_id(ses), "title": ses.title, "source": "yardcode", "cwd": ses.cwd, "host": socket.gethostname(), "model": app.client.model}
        try:
            if ses.reset_count != ses.synced_reset or ses.synced > len(ses.messages):    # a compaction or rewind changed history: send it all again
                app.modelapi.request("POST", "/chats/save", {"chat": dict(meta, messages=portable(ses.messages))}, timeout=60)
            elif ses.synced < len(ses.messages):
                app.modelapi.request("POST", "/chats/append", dict(meta, messages=portable(ses.messages[ses.synced:])), timeout=60)
            else:
                return True
        except ModelAPIError:
            return False
        ses.set_synced(len(ses.messages), ses.reset_count, remote_id(ses))
        return True


def push_in_background(app):
    t = threading.Thread(target=lambda: push(app), daemon=True)
    t.start()
    app._sync_thread = t


def list_remote(app):
    return app.modelapi.request("GET", "/chats", timeout=30).get("chats", [])


def pull(app, chat_id):
    """Make a local conversation from a chat on the server, so it can be continued here."""
    data = app.modelapi.request("GET", "/chat?id=" + chat_id, timeout=60)["chat"]
    ses = Session(app.agent.ctx.cwd, persist=app.persist)
    msgs = []
    for m in data.get("messages", []):
        d = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("synthetic"):
            d["_synthetic"] = m["synthetic"]
        if m.get("tool_calls"):
            d["tool_calls"] = m["tool_calls"]
        if m["role"] == "tool":
            d["tool_call_id"] = m.get("tool_call_id", "")
            d["_name"] = m.get("name", "")
        msgs.append(d)
    ses.title = data.get("title") or ""
    ses.reset(msgs)
    ses.set_synced(len(msgs), ses.reset_count, data["id"])
    return ses, data
