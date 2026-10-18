"""apiregistry.py — a registry of the user's own self-hosted (or third-party) HTTP APIs, so the dashboard and the
model can use them. The user registers each API with its exact base URL, so the host is always one they chose; the
model and the UI only supply the query and parameters (data), never the host. That is what keeps this from being an
open server-side proxy: there is no arbitrary-URL fetch here, only calls to the handful of endpoints the admin added.

Two kinds:
  search  a GET endpoint that answers JSON; the query goes in one parameter and the results are mapped to a list of
          {title, url, snippet} and returned as readable text (e.g. SearXNG, a Whoogle instance, a docs search).
  rest    a GET or POST endpoint; the caller supplies a path (joined onto the base, same host only) and parameters,
          and the JSON (or text) reply comes back, truncated.

Stored at <state_dir>/apis.json. Secrets (an API key/token) are kept there and never sent back to the page; the list
reports only whether an entry has auth. Registered as AI tools by the clients that ask for /api/apis.
"""
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

MAX_ENTRIES = 40
FETCH_TIMEOUT = 20
MAX_BYTES = 2_000_000
_slots = threading.BoundedSemaphore(4)
_lock = threading.Lock()


class RegistryError(Exception):
    pass


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    return s[:40] or "api"


def _host_is_private(host):
    """True if the host resolves only to private/loopback/link-local/reserved addresses. Used as a warning at save
    time, not a hard block: the user may deliberately register a LAN service (that is the whole point)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addrs = {ai[4][0] for ai in infos}
    if not addrs:
        return False
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            return False
        if not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast):
            return False
    return True


class Registry:
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir or "/var/lib/nodeyard/dashboard", "apis.json")

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _store(self, entries):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # -- validation ----------------------------------------------------------

    def _clean(self, body, existing=None):
        name = str(body.get("name") or "").strip()
        if not (1 <= len(name) <= 60):
            raise RegistryError("Give the API a name (1–60 characters).")
        kind = body.get("kind")
        if kind not in ("search", "rest"):
            raise RegistryError("Kind must be 'search' or 'rest'.")
        base = str(body.get("base") or "").strip()
        parts = urllib.parse.urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise RegistryError("The base URL must be a full http:// or https:// address.")
        method = str(body.get("method") or "GET").upper()
        if method not in ("GET", "POST"):
            raise RegistryError("Method must be GET or POST.")
        desc = str(body.get("description") or "").strip()[:300]
        auth = body.get("auth") if isinstance(body.get("auth"), dict) else {}
        atype = auth.get("type") if auth.get("type") in ("none", "header", "query", "bearer") else "none"
        aname = str(auth.get("name") or "")[:80]
        avalue = auth.get("value")
        # keep the old secret when the page saves without re-sending it
        if avalue in (None, "", "********") and existing:
            avalue = (existing.get("auth") or {}).get("value", "")
        avalue = str(avalue or "")[:4000]
        entry = {
            "id": (existing or {}).get("id") or (_slug(name) + "_" + format(int(time.time() * 1000) % 100000, "05d")),
            "name": name, "kind": kind, "base": base, "method": method, "description": desc,
            "auth": {"type": atype, "name": aname, "value": avalue},
            "extra_params": {str(k)[:60]: str(v)[:300] for k, v in (body.get("extra_params") or {}).items()} if isinstance(body.get("extra_params"), dict) else {},
        }
        if kind == "search":
            entry["query_param"] = str(body.get("query_param") or "q")[:60]
            result = body.get("result") if isinstance(body.get("result"), dict) else {}
            entry["result"] = {
                "items": str(result.get("items") or "")[:120],          # dotted path to the list, "" = top-level list
                "title": str(result.get("title") or "title")[:60],
                "url": str(result.get("url") or "url")[:60],
                "snippet": str(result.get("snippet") or "")[:60],
            }
        return entry

    # -- CRUD ----------------------------------------------------------------

    def list(self, reveal=False):
        out = []
        for e in self._load():
            safe = {k: v for k, v in e.items() if k != "auth"}
            a = e.get("auth") or {}
            safe["auth"] = {"type": a.get("type", "none"), "name": a.get("name", ""),
                            "value": a.get("value", "") if reveal else ("********" if a.get("value") else "")}
            safe["has_auth"] = bool(a.get("value"))
            safe["private_host"] = _host_is_private(urllib.parse.urlsplit(e.get("base", "")).hostname or "")
            out.append(safe)
        return out

    def get(self, api_id):
        for e in self._load():
            if e.get("id") == api_id:
                return e
        return None

    def save(self, body):
        with _lock:
            entries = self._load()
            existing = next((e for e in entries if e.get("id") == body.get("id")), None)
            if existing is None and len(entries) >= MAX_ENTRIES:
                raise RegistryError("That's the most APIs you can register (%d)." % MAX_ENTRIES)
            entry = self._clean(body, existing)
            entries = [entry if e.get("id") == entry["id"] else e for e in entries]
            if existing is None:
                entries.append(entry)
            self._store(entries)
            return entry["id"]

    def delete(self, api_id):
        with _lock:
            entries = [e for e in self._load() if e.get("id") != api_id]
            self._store(entries)

    # -- calling -------------------------------------------------------------

    def _request(self, entry, path, params, body_text):
        """Build and run the HTTP request. The host and scheme are always the registered base's; only the path
        (joined onto the base path, same host) and the parameters come from the caller."""
        base = urllib.parse.urlsplit(entry["base"])
        # join the caller's path onto the base path without letting it escape the host
        extra_path = ""
        if path:
            extra_path = "/" + str(path).lstrip("/")
            if ".." in extra_path:
                raise RegistryError("The path can't contain '..'.")
        full_path = (base.path.rstrip("/") + extra_path) if extra_path else base.path

        query = dict(entry.get("extra_params") or {})
        if isinstance(params, dict):
            for k, v in params.items():
                query[str(k)] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v)
        auth = entry.get("auth") or {}
        headers = {"Accept": "application/json, text/plain;q=0.9, */*;q=0.1", "User-Agent": "Nodeyard/1.0"}
        if auth.get("type") == "header" and auth.get("name"):
            headers[auth["name"]] = auth.get("value", "")
        elif auth.get("type") == "bearer":
            headers["Authorization"] = "Bearer " + auth.get("value", "")
        elif auth.get("type") == "query" and auth.get("name"):
            query[auth["name"]] = auth.get("value", "")

        method = entry.get("method", "GET")
        data = None
        if method == "POST":
            if body_text:
                data = str(body_text).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            else:
                data = urllib.parse.urlencode(query).encode("utf-8")
                headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
                query = {}
        url = urllib.parse.urlunsplit((base.scheme, base.netloc, full_path, urllib.parse.urlencode(query, doseq=True), ""))
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        if not _slots.acquire(blocking=False):
            raise RegistryError("Too many API calls at once. Try again in a moment.")
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310 -- scheme checked above
                raw = resp.read(MAX_BYTES + 1)
                ctype = resp.headers.get_content_type()
        except urllib.error.HTTPError as e:
            raise RegistryError("The API answered %s." % e.code)
        except (urllib.error.URLError, OSError) as e:
            raise RegistryError("Couldn't reach the API: %s" % getattr(e, "reason", e))
        finally:
            _slots.release()
        if len(raw) > MAX_BYTES:
            raw = raw[:MAX_BYTES]
        text = raw.decode("utf-8", "replace")
        parsed = None
        if "json" in ctype or text[:1] in ("{", "["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        return parsed, text

    @staticmethod
    def _dig(obj, dotted):
        cur = obj
        for part in [p for p in str(dotted).split(".") if p]:
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        return cur

    def call(self, entry, args):
        args = args if isinstance(args, dict) else {}
        if entry["kind"] == "search":
            q = str(args.get("query") or "").strip()
            if not q:
                raise RegistryError("Give a search query.")
            params = {entry.get("query_param", "q"): q}
            parsed, text = self._request(entry, "", params, None)
            return self._format_search(entry, parsed, text)
        # rest
        parsed, text = self._request(entry, args.get("path") or "", args.get("params") or {}, args.get("body"))
        if parsed is not None:
            out = json.dumps(parsed, indent=2, ensure_ascii=False)
        else:
            out = text
        return out[:12000]

    def _format_search(self, entry, parsed, text):
        if parsed is None:
            return text[:6000] or "(the search returned nothing readable)"
        mapping = entry.get("result") or {}
        items = self._dig(parsed, mapping.get("items")) if mapping.get("items") else parsed
        if not isinstance(items, list):
            return json.dumps(parsed, indent=2, ensure_ascii=False)[:8000]
        lines = []
        for it in items[:10]:
            if not isinstance(it, dict):
                lines.append("- " + str(it)[:200])
                continue
            title = it.get(mapping.get("title") or "title") or it.get("name") or ""
            url = it.get(mapping.get("url") or "url") or it.get("link") or ""
            snippet = it.get(mapping.get("snippet") or "") if mapping.get("snippet") else ""
            ent = (str(title)[:160]).strip()
            if url:
                ent += "\n  " + str(url)[:300]
            if snippet:
                ent += "\n  " + str(snippet)[:300]
            lines.append(str(len(lines) + 1) + ". " + ent)
        return ("\n".join(lines) or "No results.") if lines else "No results."

    # -- the tool schema the clients advertise to the model ------------------

    def tool_specs(self):
        specs = []
        for e in self._load():
            name = "api_" + _slug(e["name"])
            desc = e.get("description") or ("Search %s and return results." % e["name"] if e["kind"] == "search" else "Call the %s API." % e["name"])
            if e["kind"] == "search":
                props = {"query": {"type": "string", "description": "What to search for"}}
                required = ["query"]
            else:
                props = {"path": {"type": "string", "description": "Path to add to the API's base URL (optional)"},
                         "params": {"type": "object", "description": "Query parameters as key/value pairs (optional)"}}
                required = []
            specs.append({"id": e["id"], "name": name, "description": desc[:300],
                          "parameters": {"type": "object", "properties": props, "required": required, "additionalProperties": e["kind"] != "search"}})
        return specs


SEARXNG_NODEPORT = 30880


def _searxng_manifest(secret):
    """A self-contained SearXNG (Namespace + ConfigMap enabling the JSON API + Deployment + NodePort Service).
    NodePort so the dashboard on the control node can reach it at 127.0.0.1:<port>."""
    return """apiVersion: v1
kind: Namespace
metadata:
  name: nodeyard-apps
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: searxng-settings
  namespace: nodeyard-apps
data:
  settings.yml: |
    use_default_settings: true
    server:
      secret_key: "%s"
      limiter: false
      image_proxy: false
    search:
      formats:
        - html
        - json
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: searxng
  namespace: nodeyard-apps
spec:
  replicas: 1
  selector:
    matchLabels:
      app: searxng
  template:
    metadata:
      labels:
        app: searxng
    spec:
      containers:
        - name: searxng
          image: searxng/searxng:latest
          ports:
            - containerPort: 8080
          env:
            - name: SEARXNG_BASE_URL
              value: "http://localhost:%d/"
          volumeMounts:
            - name: settings
              mountPath: /etc/searxng
              readOnly: true
          resources:
            requests:
              cpu: "50m"
              memory: "128Mi"
            limits:
              memory: "512Mi"
      volumes:
        - name: settings
          configMap:
            name: searxng-settings
---
apiVersion: v1
kind: Service
metadata:
  name: searxng
  namespace: nodeyard-apps
spec:
  type: NodePort
  selector:
    app: searxng
  ports:
    - port: 8080
      targetPort: 8080
      nodePort: %d
""" % (secret, SEARXNG_NODEPORT, SEARXNG_NODEPORT)


def _searxng_api_body():
    return {"name": "SearXNG", "kind": "search", "method": "GET",
            "base": "http://127.0.0.1:%d/search" % SEARXNG_NODEPORT,
            "description": "Search the web through my self-hosted SearXNG",
            "query_param": "q", "extra_params": {"format": "json"},
            "result": {"items": "results", "title": "title", "url": "url", "snippet": "content"}}


def register(ctx, args):
    demo = bool(getattr(args, "demo", False))
    reg = Registry(getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard")
    ctx.registry = reg

    def _apis(h, q):
        import aisearch
        tools = reg.tool_specs()
        if not aisearch.allowed(ctx):    # (web search off: the AI isn't offered your search APIs)
            search = {e["id"] for e in reg.list() if e.get("kind") == "search"}
            tools = [t for t in tools if t.get("id") not in search]
        return h._json({"ok": True, "apis": reg.list(), "tools": tools, "web_search": aisearch.mode(ctx)})

    def _save(h, body):
        try:
            api_id = reg.save(body)
        except RegistryError as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        return h._json({"ok": True, "id": api_id})

    def _delete(h, body):
        reg.delete(str(body.get("id") or ""))
        return h._json({"ok": True})

    def _call(h, body):
        entry = reg.get(str(body.get("id") or ""))
        if entry is None:
            return h._json({"ok": False, "error": "No such API."}, 404)
        import aisearch
        if entry.get("kind") == "search" and not aisearch.allowed(ctx, body):
            return h._json({"ok": False, "error": "Web search is turned off (AI › Permissions).", "search_off": True}, 403)
        if demo:
            return h._json({"ok": True, "text": "(demo) would call %s; nothing was sent." % entry["name"]})
        try:
            text = reg.call(entry, body.get("args") or {})
        except RegistryError as e:
            return h._json({"ok": False, "error": str(e)}, 502)
        return h._json({"ok": True, "text": text})

    def _deploy_searxng(h, body):
        """One-click: apply a SearXNG Deployment+NodePort to the k3s cluster and register it as a search API.
        kubectl runs outside the dashboard's sandbox via a transient systemd unit, like the other system actions."""
        if demo:
            try:
                reg.save(_searxng_api_body())
            except RegistryError:
                pass
            return h._json({"ok": True, "message": "(demo) would deploy SearXNG to the cluster and register it as an API."})
        manifest = _searxng_manifest(secrets.token_hex(24))
        script = ("set -e\nexport KUBECONFIG=\"${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}\"\n"
                  "KC=\"$(command -v kubectl || echo 'k3s kubectl')\"\n"
                  "$KC apply -f - <<'NYMANIFEST'\n" + manifest + "NYMANIFEST\n")
        try:
            p = subprocess.run(["systemd-run", "--quiet", "--collect", "--wait", "--pipe", "--service-type=exec",
                                "--setenv=HOME=/root", "/bin/sh", "-c", script],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            return h._json({"ok": False, "error": "Couldn't apply the SearXNG manifest: %s" % e}, 500)
        out = (p.stdout or "").strip()
        if p.returncode != 0:
            return h._json({"ok": False, "error": ("kubectl failed:\n" + out)[:900]}, 500)
        api_id = None
        try:
            existing = next((e for e in reg.list() if (e.get("base") or "").startswith("http://127.0.0.1:%d" % SEARXNG_NODEPORT)), None)
            body2 = _searxng_api_body()
            if existing:
                body2["id"] = existing["id"]
            api_id = reg.save(body2)
        except RegistryError:
            pass
        return h._json({"ok": True, "api_id": api_id,
                        "message": "SearXNG is deploying on the cluster — pulling the image can take a few minutes. It's registered as a search API (127.0.0.1:%d/search) and will answer once the pod is Ready." % SEARXNG_NODEPORT,
                        "output": out[-500:]})

    ctx.get_routes["/api/apis"] = _apis
    ctx.post_routes.update({"/api/apis/save": _save, "/api/apis/delete": _delete, "/api/apis/call": _call,
                            "/api/apis/test": _call, "/api/apps/searxng": _deploy_searxng})
