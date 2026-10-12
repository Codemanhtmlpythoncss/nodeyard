"""The dashboard's https:// address on the tailnet (Tailscale Serve, `nodeyard dashboard https on`)."""
import json
import os
import shutil
import subprocess
import threading
import time

_cache = {"at": 0.0, "url": "", "port": None, "via": ""}
_busy = threading.Semaphore(1)    # one background look-up at a time
_first = threading.Lock()


def _lookup(port):
    url, via = "", ""
    try:
        ts = shutil.which("tailscale") or ("/usr/bin/tailscale" if os.path.exists("/usr/bin/tailscale") else "")
        if ts:
            out = subprocess.run([ts, "serve", "status", "--json"], capture_output=True, text=True, timeout=4, check=False)
            web = (json.loads(out.stdout or "{}") or {}).get("Web") or {}
            for hostport, conf in web.items():
                for handler in ((conf or {}).get("Handlers") or {}).values():
                    proxy = str((handler or {}).get("Proxy") or "")
                    if proxy.rstrip("/").endswith(":%d" % port):
                        host, _, p = hostport.rpartition(":")
                        url = "https://%s/" % (host if p == "443" else hostport)
                        via = proxy.split("://", 1)[-1].rsplit(":", 1)[0].strip("[]")   # Serve's own requests come from here
    except (OSError, ValueError, subprocess.SubprocessError):
        url = ""
    _cache.update(at=time.time(), url=url, port=port, via=via)


def _refresh(port):
    try:
        _lookup(port)
    finally:
        _busy.release()


def https_url(port, max_age=60):
    """https://NAME.TAILNET.ts.net/ when Tailscale Serve sends an https port to this dashboard, else "".

    A known answer that has gone stale is returned at once and looked up again in the background: the
    `tailscale` command can take a while, and the live updates and pages must not wait for it."""
    known = _cache["port"] == port and _cache["at"]
    if known and time.time() - _cache["at"] < max_age:
        return _cache["url"]
    if known:
        if _busy.acquire(blocking=False):
            threading.Thread(target=_refresh, args=(port,), daemon=True).start()
        return _cache["url"]
    with _first:
        if not (_cache["port"] == port and _cache["at"]):
            _lookup(port)
    return _cache["url"]


def proxy_source():
    """The address Tailscale Serve connects to this dashboard from (its own requests must never be redirected)."""
    return _cache["via"]


def forget():
    _cache["at"] = 0.0
