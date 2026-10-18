"""Chat answers that outlive the connection that asked for them.

Each answer (a plain chat or one with AI skills) is produced by a worker thread into a Turn: a numbered list of
server-sent events. The page reads it over one request. If that request drops (a Wi-Fi blip, a laptop waking up,
a proxy closing a quiet stream), the page reconnects with GET /api/ai/stream?id=ID&from=N and carries on from the
next event while the model keeps working, so nothing is asked twice. Posting the same id again attaches to the
running turn instead of sending the prompt again.

Every frame is "id: N" plus "data: JSON". While nothing new arrives, a comment line goes out every PING_SECONDS so
proxies and browsers see a live stream. The last event is always {"type": "turn_end", "result": ...}: a stream that
ends without it was cut, and the page reconnects.

  GET /api/ai/stream?id=ID&from=N    replay from event N and follow the turn (404 when the server doesn't know it,
                                     with "boot" so the page can tell a dashboard restart from an expired answer)
"""
import json
import secrets
import sys
import threading
import time

PING_SECONDS = 15
KEEP_DONE = 15 * 60        # a finished answer can still be picked up for this long
ORPHAN_SECONDS = 5 * 60    # nobody has read it for this long: stop the work (it only blocks other chats)
MAX_BYTES = 8 << 20        # per turn; an answer is text, so this only stops a runaway
MAX_TURNS = 64
BOOT = secrets.token_hex(8)  # new each time the dashboard starts


def log(msg):
    sys.stderr.write("chat: %s\n" % msg)
    sys.stderr.flush()


class Turn:
    def __init__(self, tid, owner, kind):
        self.id, self.owner, self.kind = tid, owner, kind
        self.frames = []           # encoded "data: ...\n\n" payloads, numbered by position
        self.size = 0
        self.result = ""           # "" while working; then done / cut / error / stopped / orphaned
        self.cond = threading.Condition()
        self.created = self.read_at = time.time()
        self.ended_at = 0.0
        self.readers = 0
        self.cancel = None         # set by the worker: stops the model or the agent

    @property
    def finished(self):
        return bool(self.result)

    def push(self, event):
        """Adds one event (a dict, or the model's own JSON text as bytes)."""
        data = event if isinstance(event, bytes) else json.dumps(event).encode("utf-8")
        with self.cond:
            if self.result:
                return
            if self.size + len(data) > MAX_BYTES:
                return
            self.frames.append(data)
            self.size += len(data)
            self.cond.notify_all()

    def finish(self, result, detail=""):
        with self.cond:
            if self.result:
                return
            end = {"type": "turn_end", "result": result}
            if detail:
                end["detail"] = detail
            self.frames.append(json.dumps(end).encode("utf-8"))
            self.result = result
            self.ended_at = time.time()
            self.cond.notify_all()
        if result not in ("done", "stopped"):
            log("turn %s (%s) ended: %s%s" % (self.id, self.kind, result, (" - " + detail) if detail else ""))

    def follow(self, h, start=0):
        """Writes the events from `start` on, then follows the turn until it ends. Returns "done" or "gone"
        (the page went away; the work carries on for a while in case it comes back)."""
        n = max(0, int(start))
        with self.cond:
            self.readers += 1
            self.read_at = time.time()
        try:
            while True:
                with self.cond:
                    if n >= len(self.frames) and not self.result:
                        self.cond.wait(PING_SECONDS)
                    batch = self.frames[n:]
                    ended = bool(self.result)
                    self.read_at = time.time()
                try:
                    if batch:
                        out = b"".join(b"id: %d\ndata: %s\n\n" % (n + i, f) for i, f in enumerate(batch))
                        n += len(batch)
                    elif not ended:
                        out = b": ping\n\n"
                    else:
                        out = b""
                    if out:
                        h.wfile.write(out)
                        h.wfile.flush()
                except (OSError, ValueError) as e:
                    if not self.result:
                        log("turn %s (%s): the connection dropped at event %d (%s)%s" % (self.id, self.kind, n, getattr(e, "strerror", None) or e.__class__.__name__,
                                                                                      "" if self.kind == "api" else "; still working, it can reconnect"))
                    return "gone"
                if ended and n >= len(self.frames):
                    return "done"
        finally:
            with self.cond:
                self.readers -= 1
                self.read_at = time.time()


class Turns:
    def __init__(self):
        self.lock = threading.Lock()
        self.turns = {}
        threading.Thread(target=self._reaper, daemon=True).start()

    def get(self, tid, owner):
        with self.lock:
            t = self.turns.get(tid)
        return t if t is not None and t.owner == owner else None

    def start(self, tid, owner, kind):
        """A new turn under this id, or (None, the existing one) when this id is already being answered."""
        with self.lock:
            t = self.turns.get(tid)
            if t is not None:
                return None, (t if t.owner == owner else None)
            if len(self.turns) >= MAX_TURNS:
                gone = sorted((x for x in self.turns.values() if x.finished), key=lambda x: x.ended_at)
                for x in gone[:len(self.turns) - MAX_TURNS + 1]:
                    self.turns.pop(x.id, None)
            t = Turn(tid, owner, kind)
            self.turns[tid] = t
            return t, None

    def busy(self):
        with self.lock:
            return sum(1 for t in self.turns.values() if not t.finished)

    def _reaper(self):
        while True:
            time.sleep(20)
            try:
                self.reap()
            except Exception as e:  # noqa: BLE001 -- never take the dashboard down over housekeeping
                log("reaper: %s" % e)

    def reap(self, now=None):
        now = now or time.time()
        with self.lock:
            items = list(self.turns.values())
        for t in items:
            if t.finished:
                if now - t.ended_at > KEEP_DONE:
                    with self.lock:
                        self.turns.pop(t.id, None)
            elif t.readers == 0 and now - t.read_at > ORPHAN_SECONDS:
                log("turn %s (%s): nobody has read it for %d s; stopping it" % (t.id, t.kind, now - t.read_at))
                if t.cancel:
                    try:
                        t.cancel()
                    except Exception:  # noqa: BLE001
                        pass
                t.finish("orphaned", "Nobody was reading the answer, so it was stopped.")


def of(ctx):
    """The context's turns (made on first use, so a module works without register())."""
    t = getattr(ctx, "turns", None)
    if t is None:
        t = ctx.turns = Turns()
    return t


def register(ctx, args):
    turns = of(ctx)

    def stream(h, q):
        tid = (q.get("id") or [""])[0][:64]
        try:
            start = max(0, int((q.get("from") or ["0"])[0]))
        except ValueError:
            start = 0
        t = turns.get(tid, h._token() or "local")
        if t is None:
            su = getattr(ctx, "selfupdate", None)
            return h._json({"ok": False, "error": "The dashboard doesn't have that answer (it restarted, or the answer expired).",
                            "boot": BOOT, "gone": True, "restart": getattr(su, "last_exit", None) or {}}, 404)
        send_headers(h, t)
        t.follow(h, start)

    ctx.get_routes["/api/ai/stream"] = stream


def send_headers(h, turn):
    h.send_response(200)
    for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                 ("X-Accel-Buffering", "no"), ("Connection", "close"), ("X-Nodeyard-Turn", turn.id), ("X-Nodeyard-Boot", BOOT)):
        h.send_header(k, v)
    h.end_headers()
    h.close_connection = True
    # a first byte straight away: some proxies only pass a response on once its body starts
    h.wfile.write(b": turn\n\n")
    h.wfile.flush()

