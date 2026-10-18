"""Temperature history for every machine: each sensor the node agents report (CPU, GPU, disks, board, Wi-Fi, battery),
kept for a day so a node's page can chart the last 15 minutes, hour, 6 hours or 24 hours.

It reads the readings the agents already send with each update, every SAMPLE_SECONDS: no extra polling of the hardware.
Two resolutions per sensor: 15-second points for the last hour, and 2-minute buckets (lowest, highest and mean) for the
day, packed in small integer arrays (tenths of a degree), so a whole cluster's day is a few hundred KB. It is saved to
the dashboard's state folder every few minutes, so a restart doesn't lose the day.

A sensor is "ok" while it reports; "stale" when the machine's agent hasn't answered for a while (the last reading is
kept, with its age); "error" when the agent answers but the sensor that used to be there no longer reads (a driver
or read error, or the device went away). A kind of sensor a machine never had is simply listed as not available.

  GET /api/temps?node=NAME&range=SECONDS    sensors (now, lowest, highest, state) and their series for that range
"""
import array
import json
import math
import os
import re
import sys
import threading
import time

SAMPLE_SECONDS = 15
FINE = (15, 240)        # bucket seconds, buckets: the last hour
COARSE = (120, 720)     # the last 24 hours
STALE_SECONDS = 90      # no reading from a sensor for this long: say how old the last one is
FORGET_SECONDS = 86400  # a sensor gone for a day is dropped
SAVE_SECONDS = 300
RANGES = (900, 3600, 21600, 86400)
KINDS = [("gpu", re.compile(r"gpu|amdgpu|radeon|nouveau|nvidia")),
         ("cpu", re.compile(r"x86_pkg|coretemp|k10temp|zenpower|cpu|soc|tctl|tdie|package|cluster")),
         ("disk", re.compile(r"nvme|drivetemp|sata|hdd|ssd|disk|mmc")),
         ("wifi", re.compile(r"iwlwifi|wifi|wlan|ath\d|mt76|rtw|brcmf")),
         ("battery", re.compile(r"bat|battery")),
         ("board", re.compile(r"acpi|pch|board|chipset|sys|ambient|mainboard|nct|it87|ec"))]
KIND_LABEL = {"cpu": "CPU", "gpu": "GPU", "disk": "Disk", "wifi": "Wi-Fi", "battery": "Battery", "board": "Board", "other": "Other"}


def kind_of(name):
    n = (name or "").lower()
    for k, rx in KINDS:
        if rx.search(n):
            return k
    return "other"


def _t10(c):
    return max(-32000, min(32000, int(round(float(c) * 10))))


class Series:
    """One sensor's readings at the two resolutions."""
    __slots__ = ("f_id", "f_v", "c_id", "c_min", "c_max", "c_sum", "c_n", "last", "last_at", "kind", "label")

    def __init__(self, kind, label):
        n1, n2 = FINE[1], COARSE[1]
        self.f_id, self.f_v = array.array("i", [-1]) * n1, array.array("h", [0]) * n1
        self.c_id, self.c_min, self.c_max = array.array("i", [-1]) * n2, array.array("h", [0]) * n2, array.array("h", [0]) * n2
        self.c_sum, self.c_n = array.array("i", [0]) * n2, array.array("H", [0]) * n2
        self.last, self.last_at, self.kind, self.label = None, 0.0, kind, label

    def add(self, t, c):
        v = _t10(c)
        b = int(t // FINE[0])
        i = b % FINE[1]
        self.f_id[i], self.f_v[i] = b, v
        b = int(t // COARSE[0])
        i = b % COARSE[1]
        if self.c_id[i] != b:
            self.c_id[i], self.c_min[i], self.c_max[i], self.c_sum[i], self.c_n[i] = b, v, v, v, 1
        else:
            self.c_min[i] = min(self.c_min[i], v)
            self.c_max[i] = max(self.c_max[i], v)
            self.c_sum[i] += v
            self.c_n[i] = min(65535, self.c_n[i] + 1)
        self.last, self.last_at = c, t

    def points(self, now, span):
        """[(t, mean, low, high)] over the last `span` seconds (None where there was no reading)."""
        fine = span <= FINE[0] * FINE[1]
        res, n = FINE if fine else COARSE
        step = max(1, int(span / res / 240))   # (at most ~240 points to draw)
        out = []
        first, last = int((now - span) // res), int(now // res)
        for b0 in range(first, last + 1, step):
            vals, lows, highs = [], [], []
            for b in range(b0, min(b0 + step, last + 1)):
                i = b % n
                if fine:
                    if self.f_id[i] == b:
                        vals.append(self.f_v[i])
                elif self.c_id[i] == b and self.c_n[i]:
                    vals.append(self.c_sum[i] / self.c_n[i])
                    lows.append(self.c_min[i])
                    highs.append(self.c_max[i])
            if vals:
                mean = sum(vals) / len(vals) / 10
                out.append((b0 * res, round(mean, 1), round(min(lows or vals) / 10, 1), round(max(highs or vals) / 10, 1)))
            else:
                out.append((b0 * res, None, None, None))
        return out

    def dump(self):
        return {"kind": self.kind, "label": self.label, "last": self.last, "last_at": self.last_at,
                "f": [self.f_id.tolist(), self.f_v.tolist()],
                "c": [self.c_id.tolist(), self.c_min.tolist(), self.c_max.tolist(), self.c_sum.tolist(), self.c_n.tolist()]}

    @classmethod
    def load(cls, d):
        s = cls(d.get("kind") or "other", d.get("label") or "")
        f, c = d["f"], d["c"]
        if len(f[0]) == FINE[1] and len(c[0]) == COARSE[1]:
            s.f_id, s.f_v = array.array("i", f[0]), array.array("h", f[1])
            s.c_id, s.c_min, s.c_max, s.c_sum, s.c_n = (array.array("i", c[0]), array.array("h", c[1]), array.array("h", c[2]),
                                                       array.array("i", c[3]), array.array("H", c[4]))
        s.last, s.last_at = d.get("last"), float(d.get("last_at") or 0)
        return s


def readings(n):
    """[(key, kind, label, °C or None)] from one node of the cluster state (None: listed but unreadable)."""
    hw = n.get("hw") or {}
    out, seen = [], set()
    for t in hw.get("temps") or []:
        name = str(t.get("name") or "sensor")[:60]
        key = name
        k = 2
        while key in seen:
            key, k = "%s #%d" % (name, k), k + 1
        seen.add(key)
        out.append((key, kind_of(name), name, t.get("c")))
    for g in hw.get("gpu_live") or []:
        key = "gpu%s" % g.get("index", len(out))
        out.append((key, "gpu", ("GPU %s %s" % (g.get("index", ""), g.get("name") or "")).strip(), g.get("temp_c")))
    return out


class Temps:
    def __init__(self, store, state_dir, clock=time.time, demo=False):
        self.store = store
        self.demo = demo
        self.clock = clock
        self.path = os.path.join(state_dir, "temps.json") if state_dir else ""
        self.lock = threading.Lock()
        self.nodes = {}          # node -> {key: Series}
        self.seen = {}           # node -> when its agent last answered
        self.saved_at = clock()
        self._read()

    def _read(self):
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            now = self.clock()
            for node, sensors in (raw.get("nodes") or {}).items():
                for key, d in sensors.items():
                    if now - float(d.get("last_at") or 0) < FORGET_SECONDS:
                        self.nodes.setdefault(node, {})[key] = Series.load(d)
            self.seen = {k: float(v) for k, v in (raw.get("seen") or {}).items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as e:
            sys.stderr.write("temps: couldn't read %s (%s); starting a new history\n" % (self.path, e))

    def save(self):
        if not self.path:
            return
        with self.lock:
            data = {"v": 1, "seen": dict(self.seen), "nodes": {n: {k: s.dump() for k, s in ss.items()} for n, ss in self.nodes.items()}}
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp, self.path)
            self.saved_at = self.clock()
        except OSError as e:
            sys.stderr.write("temps: couldn't save %s (%s)\n" % (self.path, e.strerror or e))

    def backfill(self, state, hours=24):
        """(the demo: a made-up day around the current readings, so its charts aren't empty)"""
        now = self.clock()
        with self.lock:
            for n in state.get("nodes") or []:
                for key, kind, label, c in readings(n):
                    if c is None:
                        continue
                    s = self.nodes.setdefault(n["name"], {}).setdefault(key, Series(kind, label))
                    phase = sum(map(ord, n["name"] + key)) % 7
                    for t in range(int(now - hours * 3600), int(now) - SAMPLE_SECONDS, SAMPLE_SECONDS):
                        s.add(t, c - 3 + 6 * math.sin(t / 2700 + phase) + 2 * math.sin(t / 300 + phase))
                    self.seen[n["name"]] = now

    def sample(self, state=None):
        if state is None:
            state = (self.store.snapshot() or {}).get("state")
        if not state:
            return
        if self.demo and not self.nodes:
            self.backfill(state)
        now = self.clock()
        with self.lock:
            for n in state.get("nodes") or []:
                if not n.get("hw"):
                    continue          # (no agent answer this time: stale, not an error)
                self.seen[n["name"]] = now
                ss = self.nodes.setdefault(n["name"], {})
                for key, kind, label, c in readings(n):
                    if c is None:
                        continue
                    try:
                        c = float(c)
                    except (TypeError, ValueError):
                        continue
                    if not -40 <= c <= 150:     # a sensor reading nonsense (unplugged probe, driver glitch)
                        continue
                    s = ss.get(key)
                    if s is None:
                        s = ss[key] = Series(kind, label)
                    s.add(now, c)
            for node in list(self.nodes):
                for key in [k for k, s in self.nodes[node].items() if now - s.last_at > FORGET_SECONDS]:
                    del self.nodes[node][key]
        if now - self.saved_at > SAVE_SECONDS:
            self.save()

    def loop(self):
        while True:
            time.sleep(SAMPLE_SECONDS)
            try:
                self.sample()
            except Exception as e:  # noqa: BLE001 -- never take the dashboard down over a chart
                sys.stderr.write("temps: %r\n" % e)

    def view(self, node, span):
        span = min(RANGES, key=lambda r: abs(r - span))
        now = self.clock()
        state = (self.store.snapshot() or {}).get("state") or {}
        n = next((x for x in state.get("nodes") or [] if x["name"] == node), None)
        live = {key: c for key, _, _, c in readings(n)} if n and n.get("hw") else None
        unreadable = {key for key, c in (live or {}).items() if c is None}
        with self.lock:
            ss = dict(self.nodes.get(node) or {})
            seen = self.seen.get(node, 0)
            sensors = []
            for key, s in sorted(ss.items(), key=lambda kv: (list(KIND_LABEL).index(kv[1].kind), kv[0])):
                pts = s.points(now, span)
                vals = [p for p in pts if p[1] is not None]
                if live is None:
                    st = "stale"
                elif key in unreadable or key not in live:
                    st = "error"
                else:
                    st = "ok" if now - s.last_at <= STALE_SECONDS else "stale"
                sensors.append({"key": key, "kind": s.kind, "kind_label": KIND_LABEL[s.kind], "label": s.label, "now": s.last if st == "ok" else None,
                                "last": s.last, "last_at": s.last_at, "state": st,
                                "low": min(p[2] for p in vals) if vals else None, "high": max(p[3] for p in vals) if vals else None,
                                "points": [[p[0], p[1]] for p in pts], "band": [[p[2], p[3]] for p in pts] if span > FINE[0] * FINE[1] else None})
        kinds = {s["kind"] for s in sensors}
        missing = [KIND_LABEL[k] for k in ("cpu", "gpu") if k not in kinds]
        agent = "ok" if live is not None else ("stale" if seen else "none")
        return {"node": node, "range": span, "ranges": RANGES, "now": now, "sensors": sensors, "missing": missing, "agent": agent, "agent_seen": seen,
                "unit": "C"}


def register(ctx, args):
    state_dir = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
    t = Temps(ctx.store, state_dir, demo=bool(getattr(args, "demo", False)))
    ctx.temps = t
    threading.Thread(target=t.loop, name="temps", daemon=True).start()

    def route(h, q):
        if t.demo and not t.nodes:
            t.sample()
        node = (q.get("node") or [""])[0][:253]
        try:
            span = int((q.get("range") or ["3600"])[0])
        except ValueError:
            span = 3600
        if not node:
            return h._json({"ok": False, "error": "Which machine?"}, 400)
        h._json(dict(t.view(node, span), ok=True))

    ctx.get_routes["/api/temps"] = route
    if hasattr(ctx, "shutdown_hooks"):
        ctx.shutdown_hooks.append(t.save)
