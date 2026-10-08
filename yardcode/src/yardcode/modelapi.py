"""Loading models on a nodeyard cluster from here, through the dashboard's key-protected control API.

The server's API key (one key for every model; the dashboard shows it under Settings > Server API key) unlocks this. Endpoints (all under /api/v1 on the dashboard):
  GET  /status                    what is loaded and whether it is ready
  GET  /models                    downloaded models (and Ollama's), which one is loaded
  POST /models/load               {"model": "file.gguf" | "ollama-name"}  -> {"job": id}
  POST /models/unload             {}
  POST /models/download           {"repo": "owner/name", "file": "x.gguf"}
  GET  /search?q=...              search Hugging Face for GGUF models
  GET  /files?repo=owner/name     the GGUF files of a repo, with a "fits" estimate
  GET  /jobs?id=ID&since=N        progress lines of a task
"""
import http.client
import json
import time
import urllib.parse

from . import net


class ModelAPIError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class ModelAPI:
    def __init__(self, base_url, api_key="", timeout=30, verify_tls=True):
        self.base = (base_url or "").rstrip("/")
        self.key = api_key or ""
        self.timeout = timeout
        self.verify_tls = verify_tls

    @property
    def available(self):
        return bool(self.base)

    def request(self, method, path, body=None, timeout=None):
        if not self.base:
            raise ModelAPIError("No dashboard address is known. Set it with: yardcode config control_url http://HOST:9092")
        u = urllib.parse.urlsplit(self.base)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ModelAPIError("The dashboard address %r isn't valid." % self.base)
        port = u.port or (443 if u.scheme == "https" else 80)
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout or self.timeout, context=net.ssl_context(self.verify_tls))
        else:
            conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout or self.timeout)
        headers = {"Accept": "application/json", "X-Nodeyard": "1", "User-Agent": "yardcode"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, (u.path.rstrip("/") + "/api/v1" + path), body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(4 * 1024 * 1024)
        except (OSError, http.client.HTTPException) as e:
            raise ModelAPIError("Can't reach the nodeyard dashboard at %s (%s)." % (self.base, e))
        finally:
            conn.close()
        try:
            data = json.loads(raw.decode("utf-8", "replace")) if raw.strip() else {}
        except ValueError:
            raise ModelAPIError("The dashboard answered with something that isn't JSON (is %s really the nodeyard dashboard?)." % self.base, resp.status)
        said = data.get("error") or ""
        if resp.status == 401:
            if not self.key:
                raise ModelAPIError("This needs the server's API key. Run: yardcode login   (the dashboard shows it under Settings > Server API key)", 401)
            raise ModelAPIError("The dashboard rejected this key. If it still works with your running model, choose Settings > Server API key > Use the running model's key, or on the server run: sudo nodeyard ai key --adopt-model. %s" % (said or "Then run yardcode login again."), 401)
        if resp.status == 403:
            raise ModelAPIError("The dashboard won't use the control API: %s" % (said or "it answered 403."), 403)
        if resp.status == 429:
            raise ModelAPIError(said or "Too many wrong keys. Wait a few minutes.", 429)
        if resp.status == 404 and not data.get("error"):
            raise ModelAPIError("That nodeyard dashboard has no /api/v1 control API yet (update nodeyard on the server).", 404)
        if resp.status >= 400 or data.get("ok") is False:
            raise ModelAPIError(data.get("error") or "The dashboard answered %d." % resp.status, resp.status)
        return data

    # ---- calls ----------------------------------------------------------------------------
    def status(self):
        return self.request("GET", "/status")

    def models(self):
        return self.request("GET", "/models")

    def load(self, model, ctx=None):
        body = {"model": model}
        if ctx:
            body["ctx"] = int(ctx)
        return self.request("POST", "/models/load", body, timeout=60)

    def unload(self, model=None):
        return self.request("POST", "/models/unload", {"model": model} if model else {}, timeout=60)

    def download(self, repo, file):
        return self.request("POST", "/models/download", {"repo": repo, "file": file}, timeout=60)

    def search(self, query, sort="downloads", limit=8):
        return self.request("GET", "/search?" + urllib.parse.urlencode({"q": query, "sort": sort, "limit": limit}), timeout=60)

    def files(self, repo):
        return self.request("GET", "/files?" + urllib.parse.urlencode({"repo": repo}), timeout=60)

    def job(self, jid, since=0):
        return self.request("GET", "/jobs?" + urllib.parse.urlencode({"id": jid, "since": since}))

    def follow(self, jid, on_line=None, timeout=3600, stop=None):
        """Print a task's output as it arrives; returns its final state ('ok', 'failed', ...)."""
        since, end = 0, time.time() + timeout
        while time.time() < end:
            if stop is not None and stop.is_set():
                return "stopped"
            j = self.job(jid, since)
            for line in j.get("lines", []):
                if on_line:
                    on_line(line)
            since = j.get("next", since)
            if j.get("status") != "running":
                return j.get("status", "ok")
            time.sleep(1.5)
        return "timeout"
