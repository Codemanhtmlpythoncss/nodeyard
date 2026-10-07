"""Chats kept on the server, so a conversation started in yardcode shows up in the dashboard's AI tab (and the other way round) and
can be continued in either place.

Protected like the rest of /api/v1: the model's API key, or a signed-in dashboard session.

  GET  /api/v1/chats                  every chat (id, title, source, updated, count...)
  GET  /api/v1/chat?id=ID             one chat with all its messages
  POST /api/v1/chats/save   {chat}    create or replace a chat
  POST /api/v1/chats/append {id, messages, title?, ...}   add messages to the end (creates the chat if it is new)
  POST /api/v1/chats/delete {id}

Messages are in the OpenAI chat format (role, content, tool_calls, tool_call_id), as yardcode keeps them, so yardcode can carry on
exactly where it left off. Files live in <state dir>/chats, one JSON file per chat, readable only by the dashboard.
"""
import json
import os
import re
import tempfile
import threading
import time

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_CHATS = 300
MAX_MESSAGES = 4000
MAX_CONTENT = 200000
MAX_BYTES = 6 * 1024 * 1024
ROLES = ("system", "user", "assistant", "tool")


class ChatError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def clean_message(m):
    if not isinstance(m, dict) or m.get("role") not in ROLES:
        raise ChatError("Every message needs a role (system, user, assistant or tool).")
    content = m.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise ChatError("A message's content must be text.")
    out = {"role": m["role"], "content": content[:MAX_CONTENT]}
    if m.get("synthetic"):
        out["synthetic"] = str(m["synthetic"])[:20]
    if m["role"] == "assistant" and isinstance(m.get("tool_calls"), list):
        calls = []
        for c in m["tool_calls"][:40]:
            fn = (c or {}).get("function") or {}
            if isinstance(c, dict) and isinstance(fn.get("name"), str):
                calls.append({"id": str(c.get("id", ""))[:80], "type": "function", "function": {"name": fn["name"][:80], "arguments": str(fn.get("arguments", ""))[:20000]}})
        if calls:
            out["tool_calls"] = calls
    if m["role"] == "tool":
        out["tool_call_id"] = str(m.get("tool_call_id", ""))[:80]
        if isinstance(m.get("name"), str):
            out["name"] = m["name"][:80]
    return out


def clean_chat(c, existing=None):
    if not isinstance(c, dict):
        raise ChatError("Send a chat object.")
    cid = str(c.get("id", ""))
    if not ID_RE.match(cid):
        raise ChatError("The chat id must be letters, numbers, dots, dashes or underscores (64 at most).")
    msgs = c.get("messages", [])
    if not isinstance(msgs, list) or len(msgs) > MAX_MESSAGES:
        raise ChatError("A chat holds up to %d messages." % MAX_MESSAGES)
    now = time.time()
    return {"id": cid, "title": str(c.get("title") or (existing or {}).get("title") or "Chat")[:120], "source": str(c.get("source") or (existing or {}).get("source") or "yardcode")[:20],
            "cwd": str(c.get("cwd") or (existing or {}).get("cwd") or "")[:300], "host": str(c.get("host") or (existing or {}).get("host") or "")[:80],
            "model": str(c.get("model") or (existing or {}).get("model") or "")[:120], "created": (existing or {}).get("created") or float(c.get("created") or now), "updated": now,
            "messages": [clean_message(m) for m in msgs]}


class Store:
    def __init__(self, directory):
        self.dir = directory
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self.lock = threading.Lock()

    def _path(self, cid):
        if not ID_RE.match(cid or ""):
            raise ChatError("That isn't a chat id.", 400)
        return os.path.join(self.dir, cid + ".json")

    def get(self, cid):
        try:
            with open(self._path(cid), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write(self, chat):
        data = json.dumps(chat, ensure_ascii=False)
        if len(data) > MAX_BYTES:
            raise ChatError("That chat is too big to keep (over %d MB)." % (MAX_BYTES // (1024 * 1024)), 413)
        fd, tmp = tempfile.mkstemp(prefix=".chat-", dir=self.dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path(chat["id"]))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _count(self):
        return sum(1 for n in os.listdir(self.dir) if n.endswith(".json"))

    def list(self):
        out = []
        for n in os.listdir(self.dir):
            if not n.endswith(".json"):
                continue
            c = self.get(n[:-5])
            if c:
                out.append({k: c.get(k) for k in ("id", "title", "source", "cwd", "host", "model", "created", "updated")} | {"count": len(c.get("messages", []))})
        out.sort(key=lambda c: -(c.get("updated") or 0))
        return out

    def save(self, chat):
        with self.lock:
            old = self.get(chat.get("id", "")) if isinstance(chat, dict) else None
            if old is None and self._count() >= MAX_CHATS:
                raise ChatError("%d chats are kept already. Delete some first." % MAX_CHATS, 409)
            c = clean_chat(chat, old)
            self._write(c)
            return c

    def append(self, body):
        with self.lock:
            cid = str(body.get("id", ""))
            old = self.get(cid)
            if old is None and self._count() >= MAX_CHATS:
                raise ChatError("%d chats are kept already. Delete some first." % MAX_CHATS, 409)
            new = body.get("messages", [])
            if not isinstance(new, list) or not new:
                raise ChatError("Send the messages to add.")
            merged = dict(old or {}, id=cid)
            for k in ("title", "source", "cwd", "host", "model"):
                if body.get(k):
                    merged[k] = body[k]
            merged["messages"] = list((old or {}).get("messages", [])) + new
            c = clean_chat(merged, old)
            self._write(c)
            return c

    def delete(self, cid):
        with self.lock:
            try:
                os.unlink(self._path(cid))
                return True
            except OSError:
                return False


def register(ctx, args):
    base = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
    directory = os.path.join(base, "chats")
    if getattr(args, "demo", False):
        directory = tempfile.mkdtemp(prefix="nodeyard-demo-chats-")
    try:
        store = Store(directory)
    except OSError:
        store = Store(tempfile.mkdtemp(prefix="nodeyard-chats-"))
    ctx.chats = store

    def fail(h, e):
        h._json({"ok": False, "error": str(e)}, getattr(e, "code", 400))

    def listing(h, q):
        h._json({"ok": True, "chats": store.list()})

    def one(h, q):
        try:
            c = store.get(q.get("id", [""])[0])
        except ChatError as e:
            return fail(h, e)
        if c is None:
            return h._json({"ok": False, "error": "No such chat."}, 404)
        h._json({"ok": True, "chat": c})

    def save(h, body):
        try:
            c = store.save(body.get("chat"))
            h._json({"ok": True, "updated": c["updated"], "count": len(c["messages"])})
        except ChatError as e:
            fail(h, e)

    def append(h, body):
        try:
            c = store.append(body)
            h._json({"ok": True, "updated": c["updated"], "count": len(c["messages"])})
        except ChatError as e:
            fail(h, e)

    def delete(h, body):
        try:
            h._json({"ok": True, "deleted": store.delete(str(body.get("id", "")))})
        except ChatError as e:
            fail(h, e)

    ctx.get_routes.update({"/api/v1/chats": listing, "/api/v1/chat": one})
    ctx.post_routes.update({"/api/v1/chats/save": save, "/api/v1/chats/append": append, "/api/v1/chats/delete": delete})
    ctx.post_limits["/api/v1/chats/save"] = ctx.post_limits["/api/v1/chats/append"] = MAX_BYTES + 65536
