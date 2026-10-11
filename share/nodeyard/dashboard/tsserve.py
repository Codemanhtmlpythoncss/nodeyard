"""The dashboard's https:// address on the tailnet (Tailscale Serve, `nodeyard dashboard https on`)."""
import json
import os
import shutil
import subprocess
import time

_cache = {"at": 0.0, "url": "", "port": None}


def https_url(port, max_age=60):
    """https://NAME.TAILNET.ts.net/ when Tailscale Serve sends an https port to this dashboard, else ""."""
    if _cache["port"] == port and time.time() - _cache["at"] < max_age:
        return _cache["url"]
    url = ""
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
    except (OSError, ValueError, subprocess.SubprocessError):
        url = ""
    _cache.update(at=time.time(), url=url, port=port)
    return url


def forget():
    _cache["at"] = 0.0
