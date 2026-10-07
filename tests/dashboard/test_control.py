"""The /api/v1 control API (load, unload, download models with the model's API key), against the demo dashboard."""
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "..", "share", "nodeyard", "dashboard", "server.py")
KEY = "demo-api-key-0000-0000-0000-0000"      # what the demo back end uses as the model's key
PASSWORD = "ABCD-EF01-2345-6789-ABCD-EF01"


class Control(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        pw = os.path.join(cls.tmp, "pw")
        with open(pw, "w") as f:
            f.write(PASSWORD)
        cls.proc = subprocess.Popen([sys.executable, SERVER, "--demo", "--password-file", pw, "--port", "0", "--interval", "1"], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        cls.port = None
        cls.log = []
        end = time.time() + 20
        while time.time() < end and cls.port is None:
            line = cls.proc.stdout.readline()
            if not line:
                break
            cls.log.append(line)
            m = re.search(r"listening on http://[^:]*:(\d+)", line)
            if m:
                cls.port = int(m.group(1))
        threading.Thread(target=lambda: [None for _ in cls.proc.stdout], daemon=True).start()
        if cls.port is None:
            raise RuntimeError("server didn't start: " + "".join(cls.log))
        time.sleep(1.5)   # let the demo state load

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)

    def call(self, method, path, body=None, key=KEY, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = dict(headers or {})
        if key:
            h["Authorization"] = "Bearer " + key
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def test_no_key_or_a_wrong_key_is_refused(self):
        s, j = self.call("GET", "/api/v1/status", key=None)
        self.assertEqual(s, 401)
        self.assertIn("API key", j["error"])
        s, j = self.call("GET", "/api/v1/models", key="wrong-key")
        self.assertEqual(s, 401)
        s, j = self.call("POST", "/api/v1/models/load", {"model": "x"}, key=None)
        self.assertEqual(s, 401)

    def test_status_and_models_with_the_key(self):
        s, j = self.call("GET", "/api/v1/status")
        self.assertEqual(s, 200)
        self.assertIn(j["model"]["state"], ("serving", "loading", "unloaded", "downloading", "none"))
        s, j = self.call("GET", "/api/v1/models")
        self.assertEqual(s, 200)
        self.assertTrue(j["ok"])
        kinds = {m["kind"] for m in j["models"]}
        self.assertIn("ollama", kinds)
        self.assertTrue(all("id" in m and "loaded" in m for m in j["models"]))

    def test_loading_a_model_starts_a_task_that_can_be_followed(self):
        s, j = self.call("GET", "/api/v1/models")
        split = [m for m in j["models"] if m["kind"] == "split"]
        self.assertTrue(split, j)
        target = next((m for m in split if not m["loaded"]), split[0])
        s, j = self.call("POST", "/api/v1/models/load", {"model": target["file"]})
        self.assertEqual(s, 200, j)
        if j.get("job"):
            end = time.time() + 15
            state = "running"
            while time.time() < end and state == "running":
                s, jj = self.call("GET", "/api/v1/jobs?id=%s&since=0" % j["job"])
                state = jj["status"]
                time.sleep(0.4)
            self.assertEqual(state, "ok")

    def test_unknown_and_ambiguous_models_are_explained(self):
        s, j = self.call("POST", "/api/v1/models/load", {"model": "no-such-model.gguf"})
        self.assertEqual(s, 404)
        self.assertIn("No downloaded model", j["error"])
        s, j = self.call("POST", "/api/v1/models/load", {})
        self.assertEqual(s, 400)

    def test_downloads_only_take_gguf_files_from_a_repo(self):
        s, j = self.call("POST", "/api/v1/models/download", {"repo": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF", "file": "x.bin"})
        self.assertEqual(s, 400)
        s, j = self.call("POST", "/api/v1/models/download", {"repo": "--oops", "file": "a.gguf"})
        self.assertEqual(s, 400)
        s, j = self.call("POST", "/api/v1/models/download", {"repo": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF", "file": "qwen.gguf"})
        self.assertEqual(s, 200, j)
        self.assertTrue(j["job"])

    def test_search_and_files(self):
        s, j = self.call("GET", "/api/v1/search?q=qwen")
        self.assertEqual(s, 200)
        self.assertTrue(j["results"])
        s, j = self.call("GET", "/api/v1/files?repo=Qwen/Qwen2.5-Coder-7B-Instruct-GGUF")
        self.assertEqual(s, 200)
        self.assertTrue(j["files"])

    def test_the_key_never_works_on_the_rest_of_the_dashboard(self):
        s, j = self.call("GET", "/api/state")
        self.assertEqual(s, 401)
        s, j = self.call("POST", "/api/run", {"action": "cli"}, headers={"X-Nodeyard": "1"})
        self.assertEqual(s, 401)

    def test_a_forwarded_internet_request_still_needs_the_key(self):
        s, j = self.call("GET", "/api/v1/status", key=None, headers={"X-Forwarded-For": "8.8.8.8", "X-Forwarded-Proto": "https"})
        self.assertEqual(s, 401)
        s, j = self.call("GET", "/api/v1/status", headers={"X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(s, 200)

    def test_guessing_keys_gets_locked_out(self):
        for i in range(14):
            s, _ = self.call("GET", "/api/v1/status", key="guess-%d" % i, headers={"X-Forwarded-For": "6.6.6.6"})
        self.assertEqual(s, 429)
        s, _ = self.call("GET", "/api/v1/status", headers={"X-Forwarded-For": "6.6.6.6"})
        self.assertEqual(s, 429)     # even the right key waits (the lockout is per address)
        s, _ = self.call("GET", "/api/v1/status", headers={"X-Forwarded-For": "7.7.7.7"})
        self.assertEqual(s, 200)


if __name__ == "__main__":
    unittest.main()


class ChatAndWeb(Control):
    """The chat page's server side: no-limit replies and web research (demo back end)."""

    def post_session(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("POST", "/api/login", body=json.dumps({"password": PASSWORD}), headers={"Content-Type": "application/json", "X-Nodeyard": "1"})
        r = conn.getresponse()
        r.read()
        cookie = r.getheader("Set-Cookie").split(";")[0]
        conn.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json", "X-Nodeyard": "1", "Cookie": cookie})
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, raw

    def test_chat_accepts_no_limit_and_big_limits(self):
        for mt in (0, None, 1024, 40000):
            s, raw = self.post_session("/api/ai/chat", {"target": "split", "messages": [{"role": "user", "content": "hi"}], "max_tokens": mt})
            self.assertEqual(s, 200, (mt, raw[:200]))

    def test_web_research_in_the_demo_needs_no_internet(self):
        s, raw = self.post_session("/api/ai/web", {"query": "what is tailscale"})
        j = json.loads(raw)
        self.assertEqual(s, 200)
        self.assertTrue(j["sources"])
        s, raw = self.post_session("/api/ai/web", {"query": "   "})
        self.assertEqual(s, 502)


class Plugins(ChatAndWeb):
    """The chat's plugins (demo agent): the list, a tool run streamed to the page, and a permission round trip."""

    def stream(self, cookie_path, body, reply_after=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("POST", "/api/login", body=json.dumps({"password": PASSWORD}), headers={"Content-Type": "application/json", "X-Nodeyard": "1"})
        r = conn.getresponse()
        r.read()
        cookie = r.getheader("Set-Cookie").split(";")[0]
        h = {"Content-Type": "application/json", "X-Nodeyard": "1", "Cookie": cookie}
        conn.request("POST", "/api/ai/agent", body=json.dumps(body), headers=h)
        r = conn.getresponse()
        events = []
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            events.append(ev)
            if ev["type"] == "permission" and reply_after:
                c2 = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
                c2.request("POST", "/api/ai/agent/reply", body=json.dumps({"chat": body["chat"], "id": ev["id"], "decision": reply_after}), headers=h)
                c2.getresponse().read()
            if ev["type"] in ("done", "exit"):
                break
        conn.close()
        return events

    def test_the_list_marks_what_is_available(self):
        s, raw = self.post_session("/api/ai/agent", {})
        self.assertEqual(s, 400)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/login", body=json.dumps({"password": PASSWORD}), headers={"Content-Type": "application/json", "X-Nodeyard": "1"})
        r = conn.getresponse()
        r.read()
        cookie = r.getheader("Set-Cookie").split(";")[0]
        conn.request("GET", "/api/ai/plugins", headers={"Cookie": cookie})
        j = json.loads(conn.getresponse().read())
        ids = [p["id"] for p in j["plugins"]]
        for want in ("web", "wikipedia", "arxiv", "weather", "calculator", "python", "files"):
            self.assertIn(want, ids)
        self.assertTrue(all(p["available"] for p in j["plugins"]))     # (the demo allows everything)

    def test_tools_stream_to_the_page(self):
        ev = self.stream("x", {"chat": "c1", "text": "what is tailscale", "plugins": ["web"]})
        types = [e["type"] for e in ev]
        self.assertIn("tool_use", types)
        self.assertIn("tool_result", types)
        self.assertEqual(ev[-1]["type"], "done")

    def test_code_asks_first_and_runs_only_when_allowed(self):
        ev = self.stream("x", {"chat": "c2", "text": "please run some python", "plugins": ["web", "python"]}, reply_after="allow")
        self.assertTrue(any(e["type"] == "permission" and e["tool"] == "Python" for e in ev))
        self.assertTrue(any(e["type"] == "tool_result" and e["name"] == "Python" and e["ok"] for e in ev))
        ev = self.stream("x", {"chat": "c3", "text": "please run some python", "plugins": ["web", "python"]}, reply_after="deny")
        self.assertTrue(any(e["type"] == "tool_result" and e["name"] == "Python" and not e["ok"] for e in ev))


class RunCode(ChatAndWeb):
    """The Run button: the server runs the code (the demo only pretends), with limits."""

    def test_the_demo_pretends_to_run_code(self):
        s, raw = self.post_session("/api/ai/run-code", {"lang": "python", "code": "print(6 * 7)"})
        j = json.loads(raw)
        self.assertEqual(s, 200)
        self.assertEqual(j["rc"], 0)
        self.assertIn("demo", j["out"])

    def test_only_python_bash_and_javascript_and_sane_sizes(self):
        for body in ({"lang": "ruby", "code": "puts 1"}, {"lang": "python", "code": "   "}, {"lang": "python", "code": "x" * 70000}, {"lang": "../sh", "code": "ls"}):
            s, _ = self.post_session("/api/ai/run-code", body)
            self.assertEqual(s, 400, body.get("lang"))

    def test_it_needs_a_sign_in(self):
        s, j = self.call("POST", "/api/ai/run-code", {"lang": "python", "code": "1"}, key=None, headers={"X-Nodeyard": "1"})
        self.assertEqual(s, 401)
