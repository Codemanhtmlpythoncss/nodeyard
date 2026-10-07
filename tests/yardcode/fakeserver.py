"""A tiny OpenAI-compatible server for tests: it replays scripted answers and records every request.

  srv = FakeModelServer([{"content": "hi"}, {"tool_calls": [("Bash", {"command": "ls"})]}, ...])
  srv.start(); ... use srv.url ...; srv.requests -> the JSON bodies it received; srv.stop()

An item in the script is a dict with any of: content, thinking, tool_calls [(name, args)], finish, status (HTTP error),
error (message for the error body), delay (seconds before the first byte).
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeModelServer:
    def __init__(self, script=None, key="", n_ctx=8192, tools_supported=True):
        self.script = list(script or [])
        self.requests = []
        self.key = key
        self.n_ctx = n_ctx
        self.tools_supported = tools_supported
        self.default = {"content": "ok"}
        self.models = ["fake-model"]
        self.httpd = None
        self.lock = threading.Lock()

    @property
    def url(self):
        return "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]

    def next_item(self):
        with self.lock:
            return self.script.pop(0) if self.script else dict(self.default)

    def start(self):
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError):
                    pass   # the client hung up (an interrupt test)

            def _auth(self):
                if srv.key and self.headers.get("Authorization") != "Bearer " + srv.key:
                    self._json(401, {"error": {"message": "Invalid API Key"}})
                    return False
                return True

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    return self._json(200, {"status": "ok"})
                if self.path in ("/props", "/v1/props"):
                    return self._json(200, {"default_generation_settings": {"n_ctx": srv.n_ctx}})
                if not self._auth():
                    return
                if self.path.endswith("/models"):
                    return self._json(200, {"data": [{"id": m} for m in srv.models]})
                self._json(404, {"error": {"message": "nope"}})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not self._auth():
                    return
                if not self.path.endswith("/chat/completions"):
                    return self._json(404, {"error": {"message": "nope"}})
                srv.requests.append(body)
                if body.get("tools") and not srv.tools_supported:
                    return self._json(400, {"error": {"message": "tools param requires --jinja flag"}})
                item = srv.next_item()
                if item.get("delay"):
                    time.sleep(item["delay"])
                if item.get("status"):
                    return self._json(item["status"], {"error": {"message": item.get("error", "error")}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()

                def send(obj):
                    self.wfile.write(("data: %s\n\n" % json.dumps(obj)).encode())
                    self.wfile.flush()

                if item.get("thinking"):
                    send({"choices": [{"index": 0, "delta": {"reasoning_content": item["thinking"]}}]})
                text = item.get("content", "")
                for i in range(0, len(text), 7):
                    send({"choices": [{"index": 0, "delta": {"content": text[i:i + 7]}}]})
                    if item.get("slow"):
                        time.sleep(item["slow"])
                for idx, (name, args) in enumerate(item.get("tool_calls", [])):
                    send({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": idx, "id": "call_%d_%d" % (len(srv.requests), idx),
                                                                              "function": {"name": name, "arguments": ""}}]}}]})
                    a = json.dumps(args)
                    for i in range(0, len(a), 11):
                        send({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": idx, "function": {"arguments": a[i:i + 11]}}]}}]})
                prompt_tokens = sum(len(json.dumps(m)) for m in body.get("messages", [])) // 4
                send({"choices": [{"index": 0, "delta": {}, "finish_reason": item.get("finish", "tool_calls" if item.get("tool_calls") else "stop")}],
                      "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": max(1, len(text) // 4)},
                      "timings": {"predicted_per_second": 12.5, "predicted_n": max(1, len(text) // 4)}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
