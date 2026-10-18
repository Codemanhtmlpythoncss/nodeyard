"""Notices when the dashboard's own program is updated on disk while it runs, and restarts on the new version.

install.sh and `nodeyard update` put new files in place while the old program keeps running. The page files are read
from disk on every request, so the browser would get the new page talking to the old program (new buttons, missing
routes). This watches the program's files; once they have changed and stayed the same for a little while, it asks
systemd to restart the service as soon as nothing is in use (no answer being written, no terminal open, no model task
or research running). Signed-in users can also restart it from the page. Only when running as the systemd service.

It also says why the service last stopped without being asked to (systemd's journal: out of memory, a crash) and
logs it when the kernel stops a process inside the service for memory, so a dropped chat or page can be explained.

  GET  /api/dashboard/version   {"build", "update_pending", "busy", "can_restart", "last_restart", "oom_kills"}
  POST /api/dashboard/restart   restart now (the page reloads itself when it's back)
"""
import hashlib
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UNIT = "nodeyard-dashboard.service"
CHECK_SECONDS = 10
SETTLE_SECONDS = 20     # an install copies files one by one: wait until they stop changing
RESULTS = {"oom-kill": "it ran out of memory", "signal": "it was killed or crashed", "core-dump": "it crashed",
           "exit-code": "it stopped with an error", "watchdog": "it stopped responding", "timeout": "it took too long to start or stop"}


def last_restart(journal=None):
    """Why the service's previous run ended, when it wasn't a clean stop: {"at", "why", "result"} or {}.
    Reads systemd's own lines for the unit (newest "Started" is this run; what came just before it says why)."""
    if journal is None:
        try:
            journal = subprocess.run(["journalctl", "-u", UNIT, "-n", "300", "-o", "short-unix", "--no-pager", "-q"],
                                     capture_output=True, text=True, timeout=8, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
    lines = journal.splitlines()
    starts = [i for i, line in enumerate(lines) if "Started " in line]
    if not starts:
        return {}
    for line in reversed(lines[:starts[-1]]):
        m = re.search(r"Failed with result '([^']+)'", line)
        if m:
            try:
                at = float(line.split()[0])
            except (ValueError, IndexError):
                at = 0.0
            return {"at": at, "result": m.group(1), "why": RESULTS.get(m.group(1), "it failed (%s)" % m.group(1))}
        if "Started " in line or "Stopped " in line or "Deactivated successfully" in line:
            return {}
    return {}


def own_memory_events():
    """This process's cgroup memory.events (cgroup v2), or ""."""
    try:
        with open("/proc/self/cgroup", "r", encoding="utf-8") as f:
            path = next((line.strip().split("::", 1)[1] for line in f if line.startswith("0::")), "")
    except (OSError, IndexError):
        return ""
    return "/sys/fs/cgroup%s/memory.events" % path if path else ""


def oom_kills(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.partition(" ")
                if k == "oom_kill":
                    return int(v)
    except (OSError, ValueError):
        pass
    return None


def stamp(folder=HERE):
    """A fingerprint of the program (its .py files: names, sizes, change times)."""
    h = hashlib.sha256()
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".py"))
    except OSError:
        return ""
    for n in names:
        try:
            st = os.stat(os.path.join(folder, n))
        except OSError:
            continue
        h.update(("%s:%d:%d;" % (n, st.st_size, int(st.st_mtime))).encode())
    return h.hexdigest()[:12]


class SelfUpdate:
    def __init__(self, ctx, args, folder=HERE):
        self.ctx = ctx
        self.folder = folder
        self.build = stamp(folder)
        self.seen, self.seen_at = self.build, time.time()
        self.pending_since = None
        self.restarting = False
        self.service = bool(os.environ.get("INVOCATION_ID")) and not getattr(args, "demo", False)
        self.last_exit = {}
        self.events = own_memory_events() if self.service else ""
        self.oom = oom_kills(self.events) if self.events else None
        self.oom_seen = self.oom

    def look_back(self):
        """(once, in the background: journalctl can be slow)"""
        if not self.service:
            return
        self.last_exit = last_restart()
        if self.last_exit:
            print("The dashboard's previous run ended because %s; systemd started it again." % self.last_exit["why"], flush=True)

    def watch_memory(self):
        """Says so in the log when the kernel stops a process in this service for memory (yt-dlp, a voice, an AI helper)."""
        if not self.events:
            return
        n = oom_kills(self.events)
        if n is not None and self.oom_seen is not None and n > self.oom_seen:
            print("Memory: the kernel stopped %d process(es) in the dashboard's service for using too much memory "
                  "(MemoryMax); the dashboard itself kept running." % (n - self.oom_seen), file=sys.stderr, flush=True)
        if n is not None:
            self.oom_seen = n

    def busy(self):
        """What would be cut off by a restart right now ("" = nothing)."""
        ctx = self.ctx
        lc = getattr(ctx, "lifecycle", None)
        if lc is not None and sum(getattr(lc, "inflight", {}).values()) > 0:
            return "an answer is being written"
        tu = getattr(ctx, "turns", None)
        if tu is not None and tu.busy():
            return "an answer is being written"
        t = getattr(ctx, "terminals", None)
        if t is not None and any(s.alive for s in list(t.sessions.values())):
            return "a terminal is open"
        ai = getattr(ctx, "ai", None)
        try:
            if ai is not None and hasattr(ai, "jobs_running") and ai.jobs_running():
                return "a model task is running"
        except Exception:  # noqa: BLE001
            pass
        r = getattr(ctx, "research", None)
        if r is not None and any(s.get("status") not in ("done", "failed", "cancelled") for s in list(getattr(r, "sessions", {}).values())):
            return "a research report is being written"
        return ""

    def check(self, now=None):
        """One look at the files; returns True when it's time to restart."""
        now = now or time.time()
        cur = stamp(self.folder)
        if not cur or cur == self.build:
            self.pending_since = None
            self.seen, self.seen_at = cur or self.build, now
            return False
        if cur != self.seen:
            self.seen, self.seen_at = cur, now        # still changing
            return False
        if now - self.seen_at < SETTLE_SECONDS:
            return False
        self.pending_since = self.pending_since or now
        return self.service and not self.busy()

    def loop(self):
        while not self.restarting:
            time.sleep(CHECK_SECONDS)
            try:
                self.watch_memory()
                if self.check():
                    self.restart("its program was updated")
            except Exception:  # noqa: BLE001 -- never take the dashboard down over this
                pass

    def restart(self, why):
        if self.restarting:
            return
        self.restarting = True
        print("Restarting the dashboard: %s." % why, flush=True)
        try:
            subprocess.Popen(["systemctl", "--no-block", "restart", UNIT], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(20)        # systemd stops this process; if it hasn't, exit and let Restart= start it again
        except OSError:
            pass
        os._exit(75)

    def view(self):
        return {"build": self.build, "update_pending": self.pending_since is not None, "busy": self.busy(),
                "can_restart": self.service, "last_restart": self.last_exit,
                "oom_kills": (self.oom_seen - self.oom) if self.oom is not None and self.oom_seen is not None else None}


def register(ctx, args):
    su = SelfUpdate(ctx, args)
    ctx.selfupdate = su
    threading.Thread(target=su.loop, daemon=True).start()
    threading.Thread(target=su.look_back, daemon=True).start()

    def version(h, q):
        h._json(dict(su.view(), ok=True))

    def restart(h, body):
        if h._forwarded() is not None:
            return h._json({"ok": False, "error": "Restart it over Tailscale or your own network, not through public access."}, 403)
        if not su.service:
            return h._json({"ok": False, "error": "This dashboard isn't running as the system service, so it can't restart itself."}, 409)
        h._json({"ok": True, "restarting": True})
        threading.Thread(target=lambda: (time.sleep(0.5), su.restart("asked from the page")), daemon=True).start()

    ctx.get_routes["/api/dashboard/version"] = version
    ctx.post_routes["/api/dashboard/restart"] = restart
