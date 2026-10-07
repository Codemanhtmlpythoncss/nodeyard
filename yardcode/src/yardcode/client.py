"""A streaming client for OpenAI-compatible chat APIs (llama.cpp, Ollama, vLLM, LM Studio, OpenAI...).

It also copes with servers that can't do tool calls themselves: the model is then taught a text format
(<tool_call>{...}</tool_call>) and this module reads the calls back out of its answer.
"""
import http.client
import json
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.parse

from . import net


class APIError(Exception):
    def __init__(self, message, status=0, kind="error"):
        super().__init__(message)
        self.status = status
        self.kind = kind     # error | auth | context | tools | loading | connect


class Cancelled(Exception):
    pass


class Completion:
    def __init__(self):
        self.content = ""
        self.thinking = ""
        self.tool_calls = []      # [{"id", "type": "function", "function": {"name", "arguments": str}}]
        self.finish = ""
        self.usage = {}
        self.timings = {}
        self.seconds = 0.0
        self.first_token = None

    @property
    def completion_tokens(self):
        return int(self.usage.get("completion_tokens") or self.timings.get("predicted_n") or 0)

    @property
    def prompt_tokens(self):
        return int(self.usage.get("prompt_tokens") or self.timings.get("prompt_n") or 0)

    @property
    def tokens_per_second(self):
        v = self.timings.get("predicted_per_second")
        if v:
            return float(v)
        n = self.completion_tokens
        gen = self.seconds - (self.first_token or 0)
        return n / gen if n and gen > 0.2 else 0.0

    def message(self):
        m = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            m["tool_calls"] = self.tool_calls
        return m


class ThinkSplitter:
    """Separates <think>...</think> from the answer, even when a tag is cut in half between two chunks."""

    def __init__(self):
        self.inside = False
        self.buf = ""

    def feed(self, text):
        out = []     # [(is_thinking, text)]
        self.buf += text
        while self.buf:
            tag = "</think>" if self.inside else "<think>"
            i = self.buf.find(tag)
            if i >= 0:
                if i:
                    out.append((self.inside, self.buf[:i]))
                self.buf = self.buf[i + len(tag):]
                self.inside = not self.inside
                continue
            keep = 0     # a partial tag at the end waits for the next chunk
            for n in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-n:]):
                    keep = n
                    break
            emit = self.buf[:len(self.buf) - keep]
            if emit:
                out.append((self.inside, emit))
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return out

    def flush(self):
        out = [(self.inside, self.buf)] if self.buf else []
        self.buf = ""
        return out


# ---- tool calls written as text (servers without native tool calling) -------------------------

TEXT_TOOL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
FUNC_RE = re.compile(r"<function=([\w.\-]+)>\s*(.*?)\s*</function>", re.S)
PARAM_RE = re.compile(r"<parameter=([\w.\-]+)>\s*(.*?)\s*</parameter>", re.S)
FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def new_call_id():
    return "call_" + secrets.token_hex(6)


def _coerce_param(v):
    v = v.strip("\n")
    if v[:1] in "[{" or v in ("true", "false", "null"):
        try:
            return json.loads(v)
        except ValueError:
            return v
    if re.fullmatch(r"-?\d+", v.strip()):
        return int(v.strip())
    return v


def parse_text_tool_calls(text, known=None):
    """(the text without its tool calls, [tool calls]) for the formats models use when asked in text."""
    calls = []

    def add(name, args):
        if known is not None and name not in known:
            return False
        if not isinstance(args, dict):
            args = {}
        calls.append({"id": new_call_id(), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}})
        return True

    def take(m):
        body = m.group(1)
        fm = FUNC_RE.search(body)
        if fm:  # Qwen's <function=NAME><parameter=KEY>VALUE</parameter></function>
            args = {k: _coerce_param(v) for k, v in PARAM_RE.findall(fm.group(2))}
            return "" if add(fm.group(1), args) else m.group(0)
        try:
            obj = json.loads(body)
        except ValueError:
            return m.group(0)
        if isinstance(obj, dict) and "name" in obj:
            args = obj.get("arguments", obj.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            return "" if add(obj["name"], args) else m.group(0)
        return m.group(0)

    out = TEXT_TOOL_RE.sub(take, text)
    if not calls:
        def take_fence(m):
            try:
                obj = json.loads(m.group(1))
            except ValueError:
                return m.group(0)
            if isinstance(obj, dict) and "name" in obj and known and obj["name"] in known:
                args = obj.get("arguments", obj.get("parameters", {}))
                return "" if add(obj["name"], args) else m.group(0)
            return m.group(0)
        out = FENCE_RE.sub(take_fence, out)
    return out.strip(), calls


# ---- the client ------------------------------------------------------------------------------

CONTEXT_WORDS = ("exceeds the available context", "context length", "context window", "maximum context", "too many tokens",
                 "prompt is too long", "n_ctx", "context_length_exceeded", "request (")


class Client:
    def __init__(self, base_url, api_key="", model="", timeout=900, connect_timeout=10, verify_tls=True):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.verify_tls = verify_tls
        self._conn = None
        self._lock = threading.Lock()

    # -- plumbing --
    def _split(self, path):
        u = urllib.parse.urlparse(self.base_url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise APIError("The API address %r isn't valid. Use something like http://host:31435/v1" % self.base_url, kind="connect")
        port = u.port or (443 if u.scheme == "https" else 80)
        prefix = u.path.rstrip("/")
        return u.scheme, u.hostname, port, prefix + path

    def _connect(self, scheme, host, port):
        if scheme == "https":
            ctx = net.ssl_context(self.verify_tls)
            conn = http.client.HTTPSConnection(host, port, timeout=self.connect_timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.connect_timeout)
        try:
            conn.connect()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise APIError("Can't reach the model API at %s:%d (%s)." % (host, port, e), kind="connect")
        conn.sock.settimeout(self.timeout)
        return conn

    def _headers(self, extra=None):
        h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "yardcode"}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        h.update(extra or {})
        return h

    def abort(self):
        """Stop the request in flight (the server stops generating when the connection closes)."""
        with self._lock:
            conn = self._conn
        if conn is not None:
            try:
                if conn.sock is not None:
                    conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def request_json(self, method, path, body=None, timeout=30):
        scheme, host, port, full = self._split(path)
        conn = self._connect(scheme, host, port)
        try:
            conn.sock.settimeout(timeout)
            payload = json.dumps(body).encode() if body is not None else None
            conn.request(method, full, body=payload, headers=self._headers())
            resp = conn.getresponse()
            raw = resp.read(8 * 1024 * 1024)
        except (OSError, http.client.HTTPException) as e:
            raise APIError("The model API stopped answering (%s)." % e, kind="connect")
        finally:
            conn.close()
        if resp.status >= 400:
            raise self._error(resp.status, raw)
        try:
            return json.loads(raw.decode("utf-8", "replace")) if raw.strip() else {}
        except ValueError:
            raise APIError("The model API sent something that isn't JSON.", resp.status)

    def _error(self, status, raw):
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        msg = text.strip()[:600]
        try:
            j = json.loads(text)
            e = j.get("error", j)
            msg = (e.get("message") if isinstance(e, dict) else str(e)) or msg
        except (ValueError, AttributeError):
            pass
        low = msg.lower()
        if status in (401, 403):
            return APIError("The API refused the key (%s). Set it with: yardcode login" % (msg or status), status, "auth")
        if status in (502, 503) or "loading model" in low:
            return APIError("The model isn't ready yet (%s)." % (msg or status), status, "loading")
        if "tool" in low and any(w in low for w in ("not support", "unsupported", "jinja", "does not support", "not available")):
            return APIError(msg, status, "tools")
        if any(w in low for w in CONTEXT_WORDS) and status in (400, 413, 500):
            return APIError(msg, status, "context")
        return APIError("API error %d: %s" % (status, msg), status)

    # -- discovery --
    def models(self):
        try:
            data = self.request_json("GET", "/models", timeout=15)
        except APIError:
            return []
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]

    def context_window(self):
        """The server's context length (llama.cpp's /props, Ollama's /api/show), or 0 if it won't say."""
        u = urllib.parse.urlparse(self.base_url)
        root = "%s://%s" % (u.scheme, u.netloc)
        for path in ("/props", "/v1/props"):
            try:
                scheme, host, port, _ = self._split("")
                conn = self._connect(scheme, host, port)
                try:
                    conn.sock.settimeout(10)
                    conn.request("GET", path, headers=self._headers())
                    resp = conn.getresponse()
                    raw = resp.read(1 << 20)
                finally:
                    conn.close()
                if resp.status == 200:
                    j = json.loads(raw)
                    n = (j.get("default_generation_settings") or {}).get("n_ctx") or j.get("n_ctx")
                    if n:
                        return int(n)
            except (APIError, OSError, ValueError, http.client.HTTPException):
                pass
        try:
            mid = self.model or (self.models() or [""])[0]
            scheme, host, port, _ = self._split("")
            conn = self._connect(scheme, host, port)
            try:
                conn.sock.settimeout(10)
                conn.request("POST", "/api/show", body=json.dumps({"model": mid}).encode(), headers=self._headers())
                resp = conn.getresponse()
                raw = resp.read(1 << 20)
            finally:
                conn.close()
            if resp.status == 200:
                info = json.loads(raw).get("model_info") or {}
                for k, v in info.items():
                    if k.endswith(".context_length"):
                        return int(v)
        except (APIError, OSError, ValueError, http.client.HTTPException):
            pass
        _ = root
        return 0

    def health(self):
        """'ok', 'loading' or 'down' for the model behind the API."""
        try:
            scheme, host, port, _ = self._split("")
            conn = self._connect(scheme, host, port)
            try:
                conn.sock.settimeout(10)
                conn.request("GET", "/health", headers=self._headers())
                resp = conn.getresponse()
                resp.read(4096)
            finally:
                conn.close()
            if resp.status == 200:
                return "ok"
            return "loading" if resp.status in (502, 503) else "down"
        except APIError:
            return "down"
        except (OSError, http.client.HTTPException):
            return "down"

    def wait_ready(self, timeout=900, tick=None, interval=3, give_up_down=90):
        """Wait until the model answers. Gives up early when the API itself can't be reached for GIVE_UP_DOWN seconds."""
        end = time.time() + timeout
        down_since = None
        while time.time() < end:
            state = self.health()
            if state == "ok":
                return True
            if state == "down":
                down_since = down_since or time.time()
                if time.time() - down_since > give_up_down:
                    return False
            else:
                down_since = None
            if tick:
                tick(state, end - time.time())
            time.sleep(interval)
        return False

    # -- chat --
    def chat(self, messages, tools=None, max_tokens=0, temperature=0.2, on_text=None, on_thinking=None,
             on_tool=None, stop=None, extra=None):
        """One streamed completion. Callbacks get text pieces as they arrive; returns a Completion."""
        body = {"model": self.model or "default", "messages": messages, "stream": True, "temperature": temperature,
                "stream_options": {"include_usage": True}}
        if max_tokens and max_tokens > 0:
            body["max_tokens"] = int(max_tokens)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        body.update(extra or {})
        scheme, host, port, full = self._split("/chat/completions")
        t0 = time.time()
        conn = self._connect(scheme, host, port)
        with self._lock:
            self._conn = conn
        comp = Completion()
        splitter = ThinkSplitter()
        calls = {}
        try:
            try:
                conn.request("POST", full, body=json.dumps(body).encode(), headers=self._headers({"Accept": "text/event-stream"}))
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                if stop is not None and stop.is_set():
                    raise Cancelled()
                raise APIError("The model API stopped answering (%s)." % e, kind="connect")
            if resp.status != 200:
                raise self._error(resp.status, resp.read(1 << 20))
            ctype = resp.getheader("Content-Type", "")
            if "event-stream" not in ctype:  # a server that ignored stream:true
                self._take_whole(json.loads(resp.read().decode("utf-8", "replace")), comp, splitter, on_text, on_thinking, on_tool)
            else:
                self._read_stream(resp, comp, splitter, calls, on_text, on_thinking, on_tool, stop)
        except (OSError, http.client.HTTPException) as e:
            if stop is not None and stop.is_set():
                raise Cancelled()
            raise APIError("The connection to the model broke (%s)." % e, kind="connect")
        finally:
            with self._lock:
                self._conn = None
            try:
                conn.close()
            except OSError:
                pass
        for piece in splitter.flush():
            self._emit(piece, comp, on_text, on_thinking)
        for idx in sorted(calls):
            c = calls[idx]
            if c["function"]["name"]:
                comp.tool_calls.append(c)
        comp.seconds = time.time() - t0
        return comp

    def _emit(self, piece, comp, on_text, on_thinking):
        thinking, text = piece
        if not text:
            return
        if thinking:
            comp.thinking += text
            if on_thinking:
                on_thinking(text)
        else:
            comp.content += text
            if on_text:
                on_text(text)

    def _read_stream(self, resp, comp, splitter, calls, on_text, on_thinking, on_tool, stop):
        t0 = time.time()
        while True:
            if stop is not None and stop.is_set():
                raise Cancelled()
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if "error" in chunk and not chunk.get("choices"):
                e = chunk["error"]
                raise self._error(int(e.get("code", 500)) if isinstance(e, dict) and str(e.get("code", "")).isdigit() else 500,
                                  json.dumps(chunk).encode())
            if chunk.get("usage"):
                comp.usage = chunk["usage"]
            if chunk.get("timings"):
                comp.timings = chunk["timings"]
            for ch in chunk.get("choices") or []:
                delta = ch.get("delta") or {}
                if comp.first_token is None and (delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls")):
                    comp.first_token = time.time() - t0
                r = delta.get("reasoning_content") or delta.get("reasoning")
                if r:
                    self._emit((True, r), comp, on_text, on_thinking)
                if delta.get("content"):
                    for piece in splitter.feed(delta["content"]):
                        self._emit(piece, comp, on_text, on_thinking)
                for tc in delta.get("tool_calls") or []:
                    i = tc.get("index", len(calls))
                    cur = calls.setdefault(i, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        cur["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        cur["function"]["name"] += fn["name"] if cur["function"]["name"] != fn["name"] else ""
                    if fn.get("arguments"):
                        cur["function"]["arguments"] += fn["arguments"]
                    if on_tool:
                        on_tool(i, cur)
                if ch.get("finish_reason"):
                    comp.finish = ch["finish_reason"]
        for c in calls.values():
            if not c["id"]:
                c["id"] = new_call_id()

    def _take_whole(self, j, comp, splitter, on_text, on_thinking, on_tool):
        ch = (j.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        for piece in splitter.feed(msg.get("content") or ""):
            self._emit(piece, comp, on_text, on_thinking)
        if msg.get("reasoning_content"):
            self._emit((True, msg["reasoning_content"]), comp, on_text, on_thinking)
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            comp.tool_calls.append({"id": tc.get("id") or new_call_id(), "type": "function",
                                    "function": {"name": fn.get("name", ""), "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
        comp.usage = j.get("usage") or {}
        comp.timings = j.get("timings") or {}
        comp.finish = ch.get("finish_reason") or ""
