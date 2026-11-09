"""A rolling two-day record of the cluster, downloadable as one file so a problem can be reported after it happens.

The live dashboard only keeps about an hour of history in memory, lost on restart. This keeps two days on disk:
every POLL_SECONDS it reads the latest snapshot and

  - logs an EVENT whenever something notable changes — a node goes NotReady or recovers, an alert appears or
    clears, a pod's restart count climbs, the AI model's state changes, the dashboard itself (re)starts; and
  - writes a compact heartbeat SAMPLE (per-node ready/temp/cpu/mem/load, alert count, running pods) at most
    once per SAMPLE_SECONDS, so trends are visible without a huge file.

Entries are newline-delimited JSON in <state_dir>/diag/log.ndjson, pruned to RETAIN_SECONDS so the file stays
small and survives restarts. The download bundles a plain-language summary (how often each node flapped, what's
wrong now, the window covered), the two days of events and samples, the current snapshot, and — on the real
service — the tail of the dashboard's own journal.

  GET /api/diag/log   ->  one JSON file (Content-Disposition: attachment), for the user to keep or send on.
"""
import collections
import json
import os
import subprocess
import threading
import time

POLL_SECONDS = 15                  # how often we look (catches a flap down to this)
SAMPLE_SECONDS = 60                # heartbeat cadence
RETAIN_SECONDS = 2 * 24 * 3600     # two days
COMPACT_SECONDS = 900              # rewrite the file from the pruned log this often
MAX_ENTRIES = 60000                # a hard cap so an event storm can't grow memory without bound
UNIT = "nodeyard-dashboard.service"


def _f(x, nd):
    """Round to nd places, pass through None (keeps the file small and JSON-clean)."""
    try:
        return None if x is None else round(float(x), nd)
    except (TypeError, ValueError):
        return None


class Recorder:
    def __init__(self, ctx, path):
        self.ctx = ctx
        self.path = path
        self.lock = threading.Lock()
        self.entries = collections.deque()
        self.prev_ready = {}       # node name -> ready bool
        self.prev_alerts = set()   # (kind, ref, title)
        self.prev_restarts = {}    # pod ref -> restart count
        self.prev_model = None
        self.last_sample = 0.0
        self.last_compact = 0.0
        self.started = time.time()
        self.stop = threading.Event()
        self._load()

    # -- storage --------------------------------------------------------------
    def _load(self):
        cutoff = time.time() - RETAIN_SECONDS
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and float(e.get("t") or 0) >= cutoff:
                        self.entries.append(e)
        except OSError:
            pass
        while len(self.entries) > MAX_ENTRIES:
            self.entries.popleft()

    def _write_line(self, entry):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", mode=0o700, exist_ok=True)
            # O_CREAT with 0o600 keeps the file private on first creation (it lives in the root-only state dir)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def _append(self, entry):
        self.entries.append(entry)
        while len(self.entries) > MAX_ENTRIES:
            self.entries.popleft()
        self._write_line(entry)

    def _prune(self, now):
        cutoff = now - RETAIN_SECONDS
        changed = False
        while self.entries and float(self.entries[0].get("t") or 0) < cutoff:
            self.entries.popleft()
            changed = True
        return changed

    def _compact(self, now):
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", mode=0o700, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                for e in list(self.entries):
                    f.write(json.dumps(e, separators=(",", ":")) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass
        self.last_compact = now

    # -- recording ------------------------------------------------------------
    def note(self, kind, title, detail="", level="info", ref="", node="", now=None):
        """Record one event straight away (used for dashboard start/stop too)."""
        now = now or time.time()
        e = {"t": round(now, 2), "type": "event", "kind": kind, "level": level, "title": title}
        if detail:
            e["detail"] = detail
        if ref:
            e["ref"] = ref
        if node:
            e["node"] = node
        with self.lock:
            self._append(e)

    def record(self, now=None):
        now = now or time.time()
        snap = self.ctx.store.snapshot()
        state = snap.get("state")
        with self.lock:
            if state:
                self._detect(state, snap, now)
                if now - self.last_sample >= SAMPLE_SECONDS:
                    self._sample(state, snap, now)
                    self.last_sample = now
            if self._prune(now) or (now - self.last_compact >= COMPACT_SECONDS):
                self._compact(now)

    def _detect(self, state, snap, now):
        # node readiness transitions (the flaps)
        for n in state.get("nodes", []):
            name = n.get("name")
            if not name:
                continue
            ready = bool(n.get("ready"))
            was = self.prev_ready.get(name)
            self.prev_ready[name] = ready
            if was is None or ready == was:
                continue
            if ready:
                self._append({"t": round(now, 2), "type": "event", "kind": "node-ready", "level": "info",
                              "node": name, "title": "%s became Ready" % name})
            else:
                reason = (n.get("ready_reason") or "").strip()
                msg = (n.get("ready_message") or "").strip()
                detail = (reason + ": " + msg).strip(": ") if (reason or msg) else ""
                self._append({"t": round(now, 2), "type": "event", "kind": "node-notready", "level": "critical",
                              "node": name, "title": "%s went NotReady" % name, "detail": detail})
        # alerts appearing and clearing
        current = {}
        for a in (snap.get("alerts") or []):
            current[(a.get("kind"), a.get("ref"), a.get("title"))] = a
        keys = set(current)
        for key in keys - self.prev_alerts:
            a = current[key]
            self._append({"t": round(now, 2), "type": "event", "kind": "alert-on", "level": a.get("level") or "info",
                          "ref": a.get("ref") or "", "title": a.get("title") or "", "detail": a.get("detail") or ""})
        for key in self.prev_alerts - keys:
            self._append({"t": round(now, 2), "type": "event", "kind": "alert-off", "level": "info",
                          "ref": key[1] or "", "title": "cleared: %s" % (key[2] or "")})
        self.prev_alerts = keys
        # pods whose restart count climbed
        for p in state.get("pods", []):
            ref = "%s/%s" % (p.get("namespace"), p.get("name"))
            r = p.get("restarts") or 0
            prev = self.prev_restarts.get(ref)
            self.prev_restarts[ref] = r
            if prev is not None and r > prev:
                self._append({"t": round(now, 2), "type": "event", "kind": "pod-restart", "level": "warning",
                              "ref": ref, "title": "%s restarted" % p.get("name"),
                              "detail": "now %d restarts (was %d)" % (r, prev)})
        # the AI model's state
        split = (state.get("ai") or {}).get("split") or {}
        model = {"name": split.get("name") or "", "loaded": bool(split.get("loaded")),
                 "ready": bool(split.get("ready"))}
        if self.prev_model is not None and model != self.prev_model:
            label = model["name"] or "model"
            where = "ready" if model["ready"] else "loaded" if model["loaded"] else "unloaded"
            self._append({"t": round(now, 2), "type": "event", "kind": "model", "level": "info",
                          "title": "AI model is now %s" % where, "detail": label})
        self.prev_model = model

    def _sample(self, state, snap, now):
        nodes = {}
        for n in state.get("nodes", []):
            hw = n.get("hw") or {}
            load = hw.get("load")
            nodes[n.get("name")] = {
                "ready": bool(n.get("ready")),
                "temp": _f(hw.get("temp_c"), 1),
                "cpu": _f(n.get("cpu_used"), 3),
                "mem": _f(n.get("mem_used"), 0),
                "load": _f(load[0] if isinstance(load, (list, tuple)) and load else None, 2),
            }
        self._append({"t": round(now, 2), "type": "sample", "nodes": nodes,
                      "alerts": len(snap.get("alerts") or []),
                      "pods_running": sum(1 for p in state.get("pods", []) if p.get("status") == "Running")})

    def loop(self):
        while not self.stop.wait(POLL_SECONDS):
            try:
                self.record()
            except Exception:  # noqa: BLE001 -- the recorder must never take the dashboard down
                pass

    # -- the download ---------------------------------------------------------
    def _journal(self):
        """Best-effort tail of the dashboard's own journal (why it last stopped, OOMs). "" off the service."""
        su = getattr(self.ctx, "selfupdate", None)
        if su is None or not getattr(su, "service", False):
            return ""
        try:
            return subprocess.run(["journalctl", "-u", UNIT, "-n", "400", "--no-pager", "-q"],
                                  capture_output=True, text=True, timeout=8, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def _summary(self, events, samples, now):
        flaps = collections.Counter()
        hottest = {}
        for e in events:
            if e.get("kind") == "node-notready" and e.get("node"):
                flaps[e["node"]] += 1
        for s in samples:
            for name, v in (s.get("nodes") or {}).items():
                t = v.get("temp")
                if t is not None and (name not in hottest or t > hottest[name]):
                    hottest[name] = t
        snap = self.ctx.store.snapshot()
        state = snap.get("state") or {}
        notready_now = [n.get("name") for n in state.get("nodes", []) if not n.get("ready")]
        span = (now - float(samples[0]["t"])) if samples else 0
        return {
            "generated": round(now, 2),
            "window_hours": round(span / 3600.0, 1),
            "window_requested_hours": RETAIN_SECONDS // 3600,
            "events": len(events),
            "samples": len(samples),
            "not_ready_now": notready_now,
            "node_notready_counts": dict(flaps),
            "hottest_temp_c": hottest,
            "critical_alerts_now": sum(1 for a in (snap.get("alerts") or []) if a.get("level") == "critical"),
        }

    def bundle(self, now=None):
        now = now or time.time()
        with self.lock:
            self._prune(now)
            entries = list(self.entries)
        events = [e for e in entries if e.get("type") == "event"]
        samples = [e for e in entries if e.get("type") == "sample"]
        snap = self.ctx.store.snapshot()
        su = getattr(self.ctx, "selfupdate", None)
        return {
            "ok": True,
            "kind": "nodeyard-2day-log",
            "cluster": ((snap.get("state") or {}).get("cluster") or {}).get("name") or getattr(self.ctx, "mode", ""),
            "nodeyard_version": getattr(self.ctx, "nd_version", ""),
            "generated_at": round(now, 2),
            "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "recorder_started": round(self.started, 2),
            "summary": self._summary(events, samples, now),
            "dashboard": {"build": getattr(su, "build", ""), "last_restart": getattr(su, "last_exit", {}),
                          "oom_kills": getattr(su, "oom_seen", None)} if su else {},
            "journal_tail": self._journal(),
            "events": events,
            "samples": samples,
            "snapshot": {"updated": snap.get("updated"), "alerts": snap.get("alerts"),
                         "totals": snap.get("totals"), "state": snap.get("state")},
        }


def register(ctx, args):
    if getattr(args, "demo", False):
        state_dir = os.path.join("/tmp", "nodeyard-demo-dashboard-%d" % os.getuid())
    else:
        state_dir = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
    rec = Recorder(ctx, os.path.join(state_dir, "diag", "log.ndjson"))
    ctx.diaglog = rec

    # mark this run in the log, carrying why the previous one ended if it wasn't clean
    su = getattr(ctx, "selfupdate", None)
    why = ""
    if su is not None and getattr(su, "last_exit", None):
        why = su.last_exit.get("why") or ""
    rec.note("dashboard-start", "dashboard started", detail=("previous run ended because " + why) if why else "")

    threading.Thread(target=rec.loop, daemon=True).start()

    def download(h, q):
        data = rec.bundle()
        fname = "nodeyard-log-%s.json" % time.strftime("%Y%m%d-%H%M", time.localtime())
        h._json(data, extra={"Content-Disposition": 'attachment; filename="%s"' % fname})

    ctx.get_routes["/api/diag/log"] = download
