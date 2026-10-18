"""Container cleanup: pods (Kubernetes' containers) nobody has used for a while, removed by a policy you control.

What "not used" means here, and nothing looser:
  - Last used is when the pod's containers last stopped: the latest status.containerStatuses[].state.terminated.finishedAt
    (high confidence). Without that, when the pod stopped being ready (its Ready condition: medium confidence). With
    neither it is "unknown", and the pod is never removed. A pod's age (creationTimestamp) is never taken as last use.
  - Candidates are pods that have stopped: phase Succeeded or Failed (finished jobs, crashed one-off pods, evicted pods).
    With "stopped containers only" off, also bare pods (no controller to restart them) that have never run a container
    for the whole period: image or config errors; the evidence is that no container ever started.
  - Never removed: running or starting pods; kube-system, kube-public and kube-node-lease; protected pods (your list,
    or the label/annotation nodeyard.io/keep=true); pods that mount a persistent volume claim or a host folder, unless
    the policy allows it. Removing a pod deletes only that pod: its volumes, Job, images and everything else stay.

Off by default. You can preview (a dry run) and clean up the listed pods by hand at any time. Automatic cleanup
(every 30 minutes) runs only after you've turned it on having seen the policy and what it would remove; changing the
policy pauses it until you confirm again. Its own dry-run mode logs what it would remove without removing anything.
Every decision is logged (cleanup-log.jsonl in the dashboard's state folder), and each removal re-reads the pod first
and deletes it with its uid as a precondition, so a new pod that took the same name is never touched.

  GET  /api/cleanup            policy, last run, preview: candidates and kept pods, each with its reason
  GET  /api/cleanup/log        recent decisions
  POST /api/cleanup/policy     {hours, stopped_only, include_volumes, protect: ["ns/name", "ns/*", "name"], dry_run}
  POST /api/cleanup/auto       {enabled, confirm, policy}   (turning it on needs confirm and the policy you reviewed)
  POST /api/cleanup/run        {uids: [...], confirm}       remove these (each is checked again first)
"""
import calendar
import fnmatch
import hashlib
import json
import os
import re
import sys
import threading
import time

SYSTEM_NS = ("kube-system", "kube-public", "kube-node-lease")
KEEP_KEY = "nodeyard.io/keep"
DEFAULT = {"hours": 12, "stopped_only": True, "include_volumes": False, "protect": [], "dry_run": False}
EVERY = 30 * 60
LOG_MAX = 512 * 1024
PROTECT_RE = re.compile(r"^[a-z0-9*?][a-z0-9*?.-]{0,62}(/[a-z0-9*?][a-z0-9*?.-]{0,252})?$")


class CleanupError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def ts(s):
    """RFC 3339 (as Kubernetes writes it) to a Unix time, or None."""
    if not s:
        return None
    try:
        return float(calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def policy_hash(p):
    return hashlib.sha256(json.dumps({k: p[k] for k in DEFAULT}, sort_keys=True).encode()).hexdigest()[:16]


def last_used(pod):
    """(when its containers last stopped, confidence, where that came from) - or (None, "unknown", why)."""
    st = pod.get("status") or {}
    ends = []
    for cs in (st.get("containerStatuses") or []) + (st.get("initContainerStatuses") or []):
        for state in ((cs.get("state") or {}).get("terminated"), (cs.get("lastState") or {}).get("terminated")):
            t = ts((state or {}).get("finishedAt"))
            if t:
                ends.append(t)
    if ends:
        return max(ends), "high", "when its containers stopped"
    ready = next((c for c in st.get("conditions") or [] if c.get("type") == "Ready" and c.get("status") == "False"), None)
    t = ts((ready or {}).get("lastTransitionTime"))
    if t:
        return t, "medium", "when it stopped being ready (no container stop times recorded)"
    return None, "unknown", "Kubernetes recorded no stop time for it"


def never_ran(pod):
    """True when no container in it has ever started (all waiting, no restarts, no earlier run)."""
    css = (pod.get("status") or {}).get("containerStatuses") or []
    if not css:
        return bool((pod.get("spec") or {}).get("containers"))
    # (by key: Kubernetes may send an empty {"running": {}})
    return all("running" not in (cs.get("state") or {}) and "terminated" not in (cs.get("state") or {}) and not cs.get("restartCount")
               and "terminated" not in (cs.get("lastState") or {}) for cs in css)


def protected(pod, patterns):
    meta = pod.get("metadata") or {}
    ns, name = meta.get("namespace", ""), meta.get("name", "")
    for src in (meta.get("labels") or {}, meta.get("annotations") or {}):
        if str(src.get(KEEP_KEY, "")).lower() in ("true", "yes", "1"):
            return "marked %s=true" % KEEP_KEY
    for p in patterns:
        if ("/" in p and fnmatch.fnmatchcase(ns + "/" + name, p)) or ("/" not in p and (fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(ns, p))):
            return "on your protected list (%s)" % p
    return ""


def persistent_mounts(pod):
    out = []
    for v in (pod.get("spec") or {}).get("volumes") or []:
        if v.get("persistentVolumeClaim"):
            out.append("volume claim %s" % v["persistentVolumeClaim"].get("claimName", "?"))
        elif v.get("hostPath"):
            out.append("host folder %s" % v["hostPath"].get("path", "?"))
    return out


def evaluate(pod, now, pol):
    """One pod against the policy: a dict with "remove" (bool) and "reason" (always), and what it was based on."""
    meta, st, spec = pod.get("metadata") or {}, pod.get("status") or {}, pod.get("spec") or {}
    owners = meta.get("ownerReferences") or []
    phase = st.get("phase", "Unknown")
    row = {"uid": meta.get("uid", ""), "namespace": meta.get("namespace", ""), "name": meta.get("name", ""), "phase": phase,
           "reason_k8s": st.get("reason", ""), "node": spec.get("nodeName", ""), "owner": ("%s/%s" % (owners[0].get("kind"), owners[0].get("name"))) if owners else "",
           "last_used": None, "confidence": "", "basis": "", "idle_h": None, "mounts": persistent_mounts(pod), "remove": False}

    def keep(why):
        row["reason"] = why
        return row
    if meta.get("deletionTimestamp"):
        return keep("already being removed")
    if row["namespace"] in SYSTEM_NS:
        return keep("system namespace")
    why = protected(pod, pol["protect"])
    if why:
        return keep("protected: " + why)
    if phase in ("Succeeded", "Failed"):
        t, conf, basis = last_used(pod)
        row.update(last_used=t, confidence=conf, basis=basis)
        if t is None:
            return keep("not sure when it was last used (%s), so it stays" % basis)
    elif phase == "Pending" and not pol["stopped_only"] and not owners and never_ran(pod):
        t = ts(meta.get("creationTimestamp"))
        if t is None:
            return keep("not sure how long it has waited")
        row.update(last_used=None, confidence="never ran", basis="no container in it has ever started (it was created then, and nothing ran since)")
        idle = now - t
        row["idle_h"] = round(idle / 3600, 1)
        if idle < pol["hours"] * 3600:
            return keep("hasn't run, but only for %s (the limit is %d h)" % (_dur(idle), pol["hours"]))
        if row["mounts"] and not pol["include_volumes"]:
            return keep("uses persistent data (%s)" % ", ".join(row["mounts"]))
        row["remove"] = True
        row["reason"] = "never started a container in %s, and nothing will restart it (no controller)" % _dur(idle)
        return row
    else:
        return keep("running" if phase == "Running" else "starting" if phase == "Pending" else "state %s" % phase)
    idle = now - row["last_used"]
    row["idle_h"] = round(idle / 3600, 1)
    if idle < pol["hours"] * 3600:
        return keep("stopped %s ago (the limit is %d h)" % (_dur(idle), pol["hours"]))
    if row["mounts"] and not pol["include_volumes"]:
        return keep("uses persistent data (%s); allow that in the policy to include it" % ", ".join(row["mounts"]))
    row["remove"] = True
    row["reason"] = "stopped %s ago (%s)%s" % (_dur(idle), row["reason_k8s"] or phase.lower(), "" if row["confidence"] == "high" else "; based on " + row["basis"])
    return row


def _dur(s):
    s = max(0, int(s))
    return "%d min" % (s // 60) if s < 3600 else "%.1f h" % (s / 3600) if s < 172800 else "%d days" % (s // 86400)


class Cleanup:
    def __init__(self, ctx, args, clock=time.time):
        self.ctx = ctx
        self.demo = bool(getattr(args, "demo", False))
        self.clock = clock
        sd = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
        self.path, self.log_path = os.path.join(sd, "cleanup.json"), os.path.join(sd, "cleanup-log.jsonl")
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.state = {"policy": dict(DEFAULT), "auto": False, "reviewed": "", "last": None}
        self._read()
        if self.demo:
            self.demo_pods = demo_pods(clock())

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.state["policy"] = self._clean_policy(dict(DEFAULT, **(raw.get("policy") or {})))
            self.state.update(auto=raw.get("auto") is True, reviewed=str(raw.get("reviewed") or ""), last=raw.get("last"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, CleanupError, TypeError) as e:
            sys.stderr.write("cleanup: couldn't read %s (%s); automatic cleanup stays off\n" % (self.path, e))

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:
            raise CleanupError("Couldn't save the cleanup settings: %s" % (e.strerror or e), 500)

    def log(self, entries):
        if not entries:
            return
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > LOG_MAX:
                os.replace(self.log_path, self.log_path + ".1")
            with open(self.log_path, "a", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e) + "\n")
        except OSError as e:
            sys.stderr.write("cleanup: couldn't write the log (%s)\n" % (e.strerror or e))

    @staticmethod
    def _clean_policy(p):
        try:
            hours = int(p.get("hours"))
        except (TypeError, ValueError):
            raise CleanupError("The inactivity period is a whole number of hours.")
        if not 1 <= hours <= 24 * 90:
            raise CleanupError("The inactivity period can be 1 hour to 90 days.")
        prot = p.get("protect") or []
        if isinstance(prot, str):
            prot = [x for x in re.split(r"[\s,]+", prot) if x]
        if not isinstance(prot, list) or len(prot) > 100 or not all(isinstance(x, str) and PROTECT_RE.match(x) for x in prot):
            raise CleanupError("Protected pods are names like namespace/pod, namespace/* or pod-name (lower case, * and ? allowed).")
        return {"hours": hours, "stopped_only": p.get("stopped_only") is not False, "include_volumes": p.get("include_volumes") is True,
                "protect": sorted(set(prot)), "dry_run": p.get("dry_run") is True}

    # ---- the cluster ------------------------------------------------------------------------------------------
    def pods(self):
        if self.demo:
            return list(self.demo_pods)
        src = getattr(self.ctx, "source", None) or getattr(self.ctx.store, "source", None)
        client = getattr(src, "client", None)
        if client is None:
            raise CleanupError("The dashboard isn't connected to the cluster.", 503)
        return (client.get("/api/v1/pods", timeout=15) or {}).get("items") or []

    def _get(self, ns, name):
        if self.demo:
            return next((p for p in self.demo_pods if p["metadata"]["namespace"] == ns and p["metadata"]["name"] == name), None)
        client = (getattr(self.ctx, "source", None) or self.ctx.store.source).client
        try:
            return client.get("/api/v1/namespaces/%s/pods/%s" % (ns, name), timeout=10)
        except Exception as e:  # noqa: BLE001 -- KubeError: gone (404) or unreachable
            if getattr(e, "status", None) == 404:
                return None
            raise

    def _delete(self, pod):
        meta = pod["metadata"]
        if self.demo:
            self.demo_pods = [p for p in self.demo_pods if p["metadata"]["uid"] != meta["uid"]]
            return
        client = (getattr(self.ctx, "source", None) or self.ctx.store.source).client
        client.delete("/api/v1/namespaces/%s/pods/%s" % (meta["namespace"], meta["name"]), {"preconditions": {"uid": meta["uid"]}, "propagationPolicy": "Background"})

    # ---- the page ---------------------------------------------------------------------------------------------
    def preview(self):
        now = self.clock()
        pol = self.state["policy"]
        rows = [evaluate(p, now, pol) for p in self.pods()]
        cand = sorted((r for r in rows if r["remove"]), key=lambda r: -(r["idle_h"] or 0))
        stopped_kept = [r for r in rows if not r["remove"] and r["phase"] in ("Succeeded", "Failed", "Pending")]
        return cand, stopped_kept, len(rows)

    def view(self):
        out = {"policy": self.state["policy"], "auto": self.state["auto"], "reviewed": self.state["reviewed"], "hash": policy_hash(self.state["policy"]),
               "last": self.state["last"], "demo": self.demo, "every_min": EVERY // 60}
        out["auto_paused"] = bool(self.state["auto"] and self.state["reviewed"] != out["hash"])
        try:
            cand, kept, total = self.preview()
            out.update(candidates=cand, kept=kept, total=total, error="")
        except Exception as e:  # noqa: BLE001 -- the page still shows the policy and the last run
            out.update(candidates=[], kept=[], total=0, error="Couldn't read the pods: %s" % e)
        return out

    def set_policy(self, body):
        pol = self._clean_policy(dict(self.state["policy"], **{k: body[k] for k in DEFAULT if k in body}))
        with self.lock:
            changed = pol != self.state["policy"]
            self.state["policy"] = pol
            self._save()
        if changed:
            self.log([{"t": self.clock(), "event": "policy", "policy": pol}])
        return self.view()

    def set_auto(self, body):
        on = body.get("enabled") is True
        with self.lock:
            if on:
                if body.get("confirm") is not True or body.get("policy") != policy_hash(self.state["policy"]):
                    raise CleanupError("Review the policy and what it would remove, then confirm to turn automatic cleanup on.", 428)
                self.state.update(auto=True, reviewed=policy_hash(self.state["policy"]))
            else:
                self.state["auto"] = False
            self._save()
        self.log([{"t": self.clock(), "event": "automatic " + ("on" if on else "off"), "policy": self.state["policy"]}])
        return self.view()

    def run(self, uids=None, auto=False):
        """Remove the candidates (all of them on an automatic run; only `uids` when asked by hand). Each pod is read again
        and checked against the policy right before it's removed."""
        if not self.run_lock.acquire(blocking=False):
            raise CleanupError("A cleanup is already running.", 409)
        try:
            pol = self.state["policy"]
            dry = auto and pol["dry_run"]
            now = self.clock()
            cand, _, total = self.preview()
            if uids is not None:
                want = set(uids)
                cand = [r for r in cand if r["uid"] in want]
            removed, errors, log = [], [], []
            for r in cand:
                entry = {"t": now, "mode": "auto" if auto else "manual", "pod": "%s/%s" % (r["namespace"], r["name"]), "uid": r["uid"],
                         "idle_h": r["idle_h"], "confidence": r["confidence"], "reason": r["reason"]}
                try:
                    fresh = self._get(r["namespace"], r["name"])
                    if fresh is None or (fresh.get("metadata") or {}).get("uid") != r["uid"]:
                        log.append(dict(entry, decision="skipped", why="it's gone or was replaced by a new pod"))
                        continue
                    again = evaluate(fresh, self.clock(), pol)
                    if not again["remove"]:
                        log.append(dict(entry, decision="kept", why="changed since the preview: " + again["reason"]))
                        continue
                    if dry:
                        log.append(dict(entry, decision="would remove (dry run)"))
                        removed.append(dict(r, dry_run=True))
                        continue
                    self._delete(fresh)
                    log.append(dict(entry, decision="removed"))
                    removed.append(r)
                except Exception as e:  # noqa: BLE001 -- one pod's failure doesn't stop the others; it's reported
                    errors.append("%s/%s: %s" % (r["namespace"], r["name"], e))
                    log.append(dict(entry, decision="error", why=str(e)))
            if uids is not None:
                for u in set(uids) - {r["uid"] for r in cand}:
                    log.append({"t": now, "mode": "manual", "uid": u, "decision": "kept", "why": "no longer a candidate"})
            last = {"at": now, "mode": "auto" if auto else "manual", "dry_run": dry, "removed": [{"pod": "%s/%s" % (r["namespace"], r["name"]), "idle_h": r["idle_h"]} for r in removed],
                    "checked": total, "errors": errors}
            with self.lock:
                self.state["last"] = last
                self._save()
            self.log(log)
            return last
        finally:
            self.run_lock.release()

    def loop(self):
        while True:
            time.sleep(EVERY)
            st = self.state
            if not st["auto"] or st["reviewed"] != policy_hash(st["policy"]):
                continue
            try:
                self.run(auto=True)
            except CleanupError:
                pass
            except Exception as e:  # noqa: BLE001 -- record it; the next run tries again
                with self.lock:
                    self.state["last"] = {"at": self.clock(), "mode": "auto", "dry_run": False, "removed": [], "checked": 0, "errors": [str(e)]}
                sys.stderr.write("cleanup: automatic run failed: %s\n" % e)

    def recent_log(self, n=200):
        lines = []
        for p in (self.log_path + ".1", self.log_path):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    lines += f.readlines()
            except OSError:
                pass
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return list(reversed(out))


def demo_pods(now):
    def iso(t):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

    def pod(ns, name, phase, finished=None, owner=None, created=None, volumes=None, labels=None, reason="", ready_at=None, waiting=False):
        cs = []
        if finished:
            cs = [{"name": "main", "state": {"terminated": {"finishedAt": iso(now - finished * 3600), "reason": "Completed" if phase == "Succeeded" else "Error", "exitCode": 0 if phase == "Succeeded" else 1}}, "restartCount": 0}]
        elif waiting:
            cs = [{"name": "main", "state": {"waiting": {"reason": "ImagePullBackOff"}}, "restartCount": 0}]
        elif phase == "Running":
            cs = [{"name": "main", "state": {"running": {"startedAt": iso(now - 50 * 3600)}}, "restartCount": 0}]
        st = {"phase": phase, "containerStatuses": cs}
        if reason:
            st["reason"] = reason
        if ready_at:
            st["conditions"] = [{"type": "Ready", "status": "False", "lastTransitionTime": iso(now - ready_at * 3600)}]
        return {"metadata": {"namespace": ns, "name": name, "uid": "demo-" + name, "creationTimestamp": iso(now - (created or 400) * 3600), "labels": labels or {},
                             "ownerReferences": [{"kind": owner.split("/")[0], "name": owner.split("/")[1]}] if owner else []},
                "spec": {"nodeName": "yard-3", "containers": [{"name": "main"}], "volumes": volumes or []}, "status": st}
    return [pod("ai-split", "model-download-qwen3-8bk2", "Succeeded", 30, "Job/model-download-qwen3"),
            pod("default", "backup-28713440-x7d", "Succeeded", 20, "Job/backup-28713440", volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "backups"}}]),
            pod("default", "migrate-once", "Failed", 50, reason=""),
            pod("home", "uptime-kuma-6d9c-evict", "Failed", None, "ReplicaSet/uptime-kuma-6d9c", reason="Evicted", ready_at=40),
            pod("home", "old-test", "Failed", None, None),
            pod("nodeyard-shell", "shell-yard-2-a1b2", "Succeeded", 3, None),
            pod("kube-system", "helm-install-traefik-crd-l9q", "Succeeded", 900, "Job/helm-install-traefik-crd"),
            pod("default", "keepsake", "Succeeded", 200, labels={KEEP_KEY: "true"}),
            pod("default", "typo-image", "Pending", None, None, created=30, waiting=True),
            pod("home", "homeassistant-0", "Running", None, "StatefulSet/homeassistant")]


def register(ctx, args):
    c = Cleanup(ctx, args)
    ctx.cleanup = c
    threading.Thread(target=c.loop, name="cleanup", daemon=True).start()

    def wrap(fn, write=False):
        def route(h, arg):
            if write and h._forwarded() is not None:
                return h._json({"ok": False, "error": "Cleanup is changed over Tailscale or your own network only."}, 403)
            try:
                r = fn(h, arg)
            except CleanupError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
            return h._json(dict(r or {}, ok=True))
        return route

    def run(h, b):
        if b.get("confirm") is not True:
            raise CleanupError("Confirm the pods to remove.", 428)
        uids = b.get("uids")
        if not isinstance(uids, list) or not uids or not all(isinstance(u, str) for u in uids):
            raise CleanupError("Say which pods to remove.")
        return {"result": c.run(uids=uids[:500])}

    ctx.get_routes["/api/cleanup"] = wrap(lambda h, q: c.view())
    ctx.get_routes["/api/cleanup/log"] = wrap(lambda h, q: {"log": c.recent_log()})
    ctx.post_routes.update({
        "/api/cleanup/policy": wrap(lambda h, b: c.set_policy(b), True),
        "/api/cleanup/auto": wrap(lambda h, b: c.set_auto(b), True),
        "/api/cleanup/run": wrap(run, True),
    })
