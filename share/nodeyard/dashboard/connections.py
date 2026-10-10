"""Device connections: each machine's Wi-Fi/LAN and Tailscale addresses, checked separately and followed across
address changes, with per-device manual overrides.

Where addresses come from (nothing registers itself, so nothing unauthenticated can claim to be a device):
  - the node agent (read-only DaemonSet, token-protected, host network): every interface with its IPv4 address,
    whether it is wireless, and Tailscale's interface;
  - Kubernetes' own node addresses (InternalIP, ExternalIP);
  - the addresses you type in (overrides), which automatic discovery never replaces.

A device is identified by its machine ID (from Kubernetes' nodeInfo), not by its name or address, so a new DHCP lease
or Tailscale address doesn't make it a different device. A changed address is only used once it has answered; until
then the last address that worked is kept (and shown as last known good).

Each path is checked on its own by connecting to what the dashboard really uses on that machine: the node agent
(port 9093) and, to tell "machine up, agent down" apart from "unreachable", the kubelet (port 10250). Checks run in
parallel with short timeouts, so one offline device doesn't slow the others. ICMP ping is not used: a firewall that
drops ping says nothing about whether the services work.
"""
import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time

AGENT_PORT, KUBELET_PORT = 9093, 10250
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*\.?$")
DEFAULT_SETTINGS = {"discovery": True, "auto_update": True, "failover": True, "preference": "auto", "monitor": True,
                    "interval": 60, "timeout": 2.0, "lan_enabled": True, "tailscale_enabled": True, "lan_override": "", "tailscale_override": ""}
PATHS = ("lan", "tailscale")
TICK = 15


class ConnectionError_(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def is_tailscale(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip in (TAILSCALE_V6 if ip.version == 6 else TAILSCALE_V4)


def valid_address(value):
    """An IP address or host name someone may type; never a URL, port, loopback, multicast or 'any' address."""
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        if HOST_RE.match(value) and not value.lower().startswith("localhost"):
            return value.rstrip(".")
        raise ConnectionError_("“%s” isn't an IP address or host name." % value[:80])
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local:
        raise ConnectionError_("%s can't be a device's address (loopback, multicast, link-local or unspecified)." % value)
    return str(ip)


def identity(node):
    return node.get("machine_id") or node.get("system_uuid") or ("name:" + node.get("name", ""))


def discover(node):
    """{'lan': [...], 'tailscale': [...]} addresses, best first, each {'address', 'iface', 'wireless', 'source'}."""
    found = {"lan": [], "tailscale": []}
    seen = set()

    def add(path, address, iface, wireless, source):
        if not address or address in seen:
            return
        seen.add(address)
        found[path].append({"address": address, "iface": iface, "wireless": bool(wireless), "source": source})

    nics = (((node.get("hw") or {}).get("hardware") or {}).get("nics")) or []
    for nic in sorted(nics, key=lambda n: (not n.get("cluster"), bool(n.get("wireless")), n.get("name", ""))):
        address = nic.get("ipv4") or ""
        if not address or nic.get("up") is False:
            continue
        name = nic.get("name", "")
        if name.startswith("tailscale") or is_tailscale(address):
            add("tailscale", address, name, False, "node agent")
        else:
            add("lan", address, name, nic.get("wireless"), "node agent")
    for a in node.get("addresses") or []:
        if a.get("type") in ("InternalIP", "ExternalIP"):
            address = a.get("address", "")
            add("tailscale" if is_tailscale(address) else "lan", address, "", False, "Kubernetes " + a["type"])
    return found


def tcp_probe(address, port, timeout):
    """(ok, milliseconds, error) for one TCP connection."""
    t0 = time.monotonic()
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True, round((time.monotonic() - t0) * 1000, 1), ""
    except OSError as e:
        return False, None, (e.strerror or str(e) or e.__class__.__name__)[:120]


class Connections:
    def __init__(self, store, state_dir, probe=None, clock=time.time, demo=False):
        self.store = store
        self.path = os.path.join(state_dir, "devices.json")
        self.probe = probe or (self._demo_probe if demo else tcp_probe)
        self.clock = clock
        self.lock = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=12, thread_name_prefix="nodeyard-connections")
        self.records = self._load()
        self.stop = threading.Event()
        self.thread = None
        self.active = None        # plugins.py: is device monitoring turned on?

    # -- storage ------------------------------------------------------------------------------------------------

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("devices", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".devices-", dir=os.path.dirname(self.path))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"devices": self.records}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _record(self, node):
        did = identity(node)
        rec = self.records.get(did)
        if rec is None:
            # A device seen before under another identity (e.g. before machine IDs were read) keeps its settings.
            old = next((k for k, r in self.records.items() if k.startswith("name:") and r.get("name") == node.get("name")), None)
            rec = self.records.pop(old) if old else {"settings": dict(DEFAULT_SETTINGS), "history": []}
            rec.update({"id": did})
            self.records[did] = rec
        rec["name"] = node.get("name", "")
        rec.setdefault("settings", dict(DEFAULT_SETTINGS))
        for k, v in DEFAULT_SETTINGS.items():
            rec["settings"].setdefault(k, v)
        for path in PATHS:
            rec.setdefault(path, {"address": "", "last_good": "", "verified_at": None, "candidate": "", "state": "unknown", "checked_at": None,
                                  "error": "", "ms": None, "agent": None, "kubelet": None, "detected": []})
        rec.setdefault("history", [])
        return rec

    def _event(self, rec, text):
        rec["history"].append({"time": self.clock(), "text": text})
        del rec["history"][:-20]

    # -- checking ------------------------------------------------------------------------------------------------

    def tick(self, now=None, force_ids=None):
        """Discover addresses and check every path that is due (or every path of FORCE_IDS)."""
        now = self.clock() if now is None else now
        nodes = (self.store.snapshot().get("state") or {}).get("nodes") or []
        jobs = []
        with self.lock:
            for node in nodes:
                rec = self._record(node)
                s = rec["settings"]
                rec["k8s_ready"] = bool(node.get("ready"))
                detected = discover(node) if s["discovery"] else {"lan": [], "tailscale": []}
                for path in PATHS:
                    p = rec[path]
                    p["detected"] = detected[path]
                    if not s[path + "_enabled"]:
                        p.update(state="off", candidate="", error="")
                        continue
                    override = s[path + "_override"]
                    if override:
                        candidate = override
                    elif p["address"] and not s["auto_update"]:
                        candidate = p["address"]          # keep the address it has; changes are shown, not followed
                    else:
                        candidate = (detected[path][0]["address"] if detected[path] else "") or p["last_good"]
                    if candidate != p["candidate"]:
                        if p["candidate"] and candidate:
                            self._event(rec, "%s address %s → %s (%s); checking it" % (path_name(path), p["candidate"], candidate, "your override" if override else "discovered"))
                        p["candidate"] = candidate
                        p["checked_at"] = None          # check a new address now
                    if not candidate:
                        p.update(state="no address", error="")
                        continue
                    due = p["checked_at"] is None or now - p["checked_at"] >= s["interval"] or (force_ids and rec["id"] in force_ids)
                    if s["monitor"] and due or (force_ids and rec["id"] in force_ids):
                        jobs.append((rec["id"], path, candidate, float(s["timeout"])))
        results = {}
        futs = {(rid, path): self.pool.submit(self._check, address, timeout) for rid, path, address, timeout in jobs}
        for key, fut in futs.items():
            try:
                results[key] = fut.result(timeout=30)
            except Exception as e:  # noqa: BLE001
                results[key] = {"agent": [False, None, str(e)], "kubelet": [False, None, str(e)]}
        with self.lock:
            for (rid, path), r in results.items():
                rec = self.records.get(rid)
                if rec:
                    self._apply(rec, path, r, now)
            for rec in self.records.values():
                if rec.get("name") in {n.get("name") for n in nodes}:
                    self._summarise(rec)
            self._save()
        return len(jobs)

    def _check(self, address, timeout):
        return {"agent": list(self.probe(address, AGENT_PORT, timeout)), "kubelet": list(self.probe(address, KUBELET_PORT, timeout))}

    def _apply(self, rec, path, r, now):
        p, s = rec[path], rec["settings"]
        agent_ok, kubelet_ok = r["agent"][0], r["kubelet"][0]
        p.update(checked_at=now, agent=agent_ok, kubelet=kubelet_ok, ms=r["agent"][1] if agent_ok else r["kubelet"][1])
        if agent_ok or kubelet_ok:
            p["state"] = "reachable" if agent_ok else "partial"
            p["error"] = "" if agent_ok else "The machine answers, but the node agent doesn't (%s)." % r["agent"][2]
            if p["candidate"] != p["address"]:
                self._event(rec, ("%s address verified: now %s (was %s)" % (path_name(path), p["candidate"], p["address"])) if p["address"]
                            else "%s address found: %s" % (path_name(path), p["candidate"]))
                p["address"] = p["candidate"]
            p["last_good"], p["verified_at"] = p["candidate"], now
        else:
            p["state"] = "unreachable"
            p["error"] = "No answer on %s (agent: %s; kubelet: %s)." % (p["candidate"], r["agent"][2], r["kubelet"][2])

    def _summarise(self, rec):
        lan, ts = rec["lan"]["state"], rec["tailscale"]["state"]
        ok = {path for path in PATHS if rec[path]["state"] == "reachable"}
        partial = {path for path in PATHS if rec[path]["state"] == "partial"}
        if ok == set(PATHS):
            status = "both"
        elif ok:
            status = next(iter(ok))
        elif partial:
            status = "partial"
        elif "unreachable" in (lan, ts):
            status = "unreachable"
        else:
            status = "unknown"
        pref = rec["settings"]["preference"]
        order = ["lan", "tailscale"] if pref in ("auto", "lan") else ["tailscale", "lan"]
        chosen = next((path for path in order if rec[path]["state"] == "reachable"), None)
        if pref in ("lan", "tailscale") and not rec["settings"]["failover"] and chosen != pref:
            chosen = pref if rec[pref]["state"] == "reachable" else None
        if rec.get("preferred") and chosen and rec["preferred"] != chosen:
            self._event(rec, "Switched to %s (%s is %s)" % (path_name(chosen), path_name(rec["preferred"]), rec[rec["preferred"]]["state"]))
        rec["preferred"] = chosen
        rec["status"] = status

    # -- what other parts and the pages use ----------------------------------------------------------------------

    def candidates(self, node_name):
        """Addresses to reach a node's agent, best first (for the agent poller's failover)."""
        with self.lock:
            rec = next((r for r in self.records.values() if r.get("name") == node_name), None)
            if not rec or not rec["settings"]["failover"]:
                return []
            order = [rec.get("preferred")] + [p for p in PATHS if p != rec.get("preferred")]
            return [rec[p]["address"] or rec[p]["last_good"] for p in order if p and rec[p].get("state") in ("reachable", "partial") and (rec[p]["address"] or rec[p]["last_good"])]

    def view(self):
        nodes = {n.get("name") for n in ((self.store.snapshot().get("state") or {}).get("nodes") or [])}
        with self.lock:
            out = [json.loads(json.dumps(r)) for r in self.records.values() if r.get("name") in nodes]
        return sorted(out, key=lambda r: r.get("name", ""))

    def update(self, did, body):
        with self.lock:
            rec = self.records.get(str(did))
            if not rec:
                raise ConnectionError_("No such device.", 404)
            s = dict(rec["settings"])
            if body.get("reset") is True:
                s = dict(DEFAULT_SETTINGS)
                self._event(rec, "Reset to automatic discovery")
            for key in ("discovery", "auto_update", "failover", "monitor", "lan_enabled", "tailscale_enabled"):
                if key in body:
                    if not isinstance(body[key], bool):
                        raise ConnectionError_("%s must be on or off." % key.replace("_", " ").capitalize())
                    s[key] = body[key]
            if "preference" in body:
                if body["preference"] not in ("auto", "lan", "tailscale"):
                    raise ConnectionError_("Choose automatic, Wi-Fi/LAN or Tailscale.")
                s["preference"] = body["preference"]
            for key, low, high in (("interval", 15, 3600), ("timeout", 0.5, 10)):
                if key in body:
                    v = body[key]
                    if isinstance(v, bool) or not isinstance(v, (int, float)) or not low <= v <= high:
                        raise ConnectionError_("%s must be between %s and %s seconds." % (key.capitalize(), low, high))
                    s[key] = int(v) if key == "interval" else float(v)
            for path in PATHS:
                key = path + "_override"
                if key in body:
                    value = valid_address(body[key])
                    if value and path == "tailscale" and re.match(r"^[\d.]+$", value) and not is_tailscale(value):
                        raise ConnectionError_("%s isn't a Tailscale address (they are in 100.64.0.0/10)." % value)
                    if value != s[key]:
                        self._event(rec, "%s override %s" % (path_name(path), ("set to " + value) if value else "removed"))
                    s[key] = value
            rec["settings"] = s
            for path in PATHS:
                rec[path]["checked_at"] = None     # check with the new settings right away
            self._save()
        self.tick(force_ids={did})
        with self.lock:
            return json.loads(json.dumps(self.records[did]))

    def test(self, did):
        with self.lock:
            if str(did) not in self.records:
                raise ConnectionError_("No such device.", 404)
        self.tick(force_ids={str(did)})
        with self.lock:
            return json.loads(json.dumps(self.records[str(did)]))

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="nodeyard-connections", daemon=True)
            self.thread.start()

    def _loop(self):
        delay = 5
        while not self.stop.wait(delay):
            delay = TICK
            if self.active is not None and not self.active():
                continue          # turned off in Settings › Plugins
            try:
                self.tick()
            except Exception:  # noqa: BLE001 -- one bad round mustn't stop monitoring
                pass

    @staticmethod
    def _demo_probe(address, port, timeout):
        # The demo cluster isn't real: its fourth machine is only on Tailscale, everything else answers.
        if address.startswith("192.168.1.13"):
            return False, None, "timed out"
        return True, 3.0 if is_tailscale(address) else 0.8, ""


def path_name(path):
    return {"lan": "Wi-Fi/LAN", "tailscale": "Tailscale"}.get(path, path)


def register(ctx, args):
    store = getattr(ctx, "store", None)
    if store is None:
        return None
    demo = bool(getattr(args, "demo", False))
    state_dir = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
    if demo:
        state_dir = os.path.join(tempfile.gettempdir(), "nodeyard-demo-dashboard-%d" % os.getuid())
    c = Connections(store, state_dir, demo=demo)
    ctx.connections = c
    agents = getattr(store, "agents", None)
    if agents is not None and hasattr(agents, "fallback"):
        agents.fallback = c.candidates

    def fail(h, e):
        h._json({"ok": False, "error": str(e)}, getattr(e, "code", 400))

    def view(h, q):
        h._json({"ok": True, "devices": c.view(), "defaults": DEFAULT_SETTINGS})

    def update(h, body):
        try:
            h._json({"ok": True, "device": c.update(str(body.get("id") or ""), body)})
        except ConnectionError_ as e:
            fail(h, e)

    def test(h, body):
        try:
            h._json({"ok": True, "device": c.test(str(body.get("id") or ""))})
        except ConnectionError_ as e:
            fail(h, e)

    ctx.get_routes["/api/devices/connections"] = view
    ctx.post_routes.update({"/api/devices/connections": update, "/api/devices/test": test})
    c.start()
    return c
