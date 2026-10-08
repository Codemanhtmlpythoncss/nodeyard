"""Web tools, the JSON protocol, the command line, MCP, plugins and the nodeyard control client."""
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import unittest

from common import REPO, Base
from yardcode import mcp, modelapi, plugins
from yardcode.tools import web
from yardcode.tools.base import ToolError

YARDCODE = os.path.join(REPO, "yardcode", "bin", "yardcode")

DDG = """<div class="result results_links_deep"><h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone&amp;rut=zz">First &amp; best</a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone">The <b>first</b> snippet.</a></div>
<div class="result"><h2><a class="result__a" href="https://second.org/page">Second</a></h2><a class="result__snippet" href="x">Another one</a></div>"""

BING = """<ol><li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=1&amp;u=a1aHR0cHM6Ly9kb2NzLnB5dGhvbi5vcmcvMy8&amp;ntb=1">Python docs</a></h2><div class="b_caption"><p>The official docs.</p></div></li>
<li class="b_algo"><h2><a href="https://plain.example/x">Plain</a></h2><p>Plain snippet</p></li></ol>"""

LITE = """<table><tr><td>1.&nbsp;</td><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Flite.example%2Fa" class='result-link'>Lite A</a></td></tr>
<tr><td></td><td class='result-snippet'>Snippet for A</td></tr></table>"""

PAGE = """<html><head><title>My Page</title><style>.x{}</style><script>var a=1;</script></head><body><nav>menu menu</nav>
<main><h1>Heading</h1><p>Hello <b>world</b> and a <a href="/rel">link</a>.</p><ul><li>one</li><li>two</li></ul><pre>code line 1
code line 2</pre>
<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>""" + "<p>filler paragraph.</p>" * 30 + """</main><footer>footer text</footer></body></html>"""


class WebParsing(unittest.TestCase):
    def test_ddg_html(self):
        r = web.parse_ddg_html(DDG)
        self.assertEqual(r[0], {"title": "First & best", "url": "https://example.com/one", "snippet": "The first snippet."})
        self.assertEqual(r[1]["url"], "https://second.org/page")

    def test_ddg_lite_and_bing(self):
        self.assertEqual(web.parse_ddg_lite(LITE)[0]["url"], "https://lite.example/a")
        b = web.parse_bing(BING)
        self.assertEqual(b[0]["url"], "https://docs.python.org/3/")
        self.assertEqual(b[0]["snippet"], "The official docs.")
        self.assertEqual(b[1]["url"], "https://plain.example/x")

    def test_html_to_text_keeps_structure_and_drops_chrome(self):
        title, text = web.html_to_text(PAGE, "https://site.test/dir/")
        self.assertEqual(title, "My Page")
        self.assertIn("# Heading", text)
        self.assertIn("[link](https://site.test/rel)", text)
        self.assertIn("- one", text)
        self.assertIn("```\ncode line 1\ncode line 2\n```", text)
        self.assertNotIn("menu menu", text)
        self.assertNotIn("var a", text)
        self.assertNotIn("footer text", text)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        body = {"/page": (PAGE, "text/html"), "/data": ('{"a": 1}', "application/json"), "/bin": ("\x00\x01", "application/octet-stream")}.get(self.path)
        if not body:
            self.send_response(404)
            self.end_headers()
            return
        data = body[0].encode()
        self.send_response(200)
        self.send_header("Content-Type", body[1])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class WebFetching(Base):
    def setUp(self):
        super().setUp()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def test_private_addresses_are_refused_unless_allowed(self):
        with self.assertRaises(ToolError) as e:
            web.WebFetch().run({"url": self.base + "/page"}, self.ctx())
        self.assertIn("private or local", str(e.exception))
        r = web.WebFetch().run({"url": self.base + "/page"}, self.ctx(web={"allow_private": True, "timeout": 10}))
        self.assertIn("# Heading", r.text)

    def test_fetch_follows_redirects_pages_long_text_and_reads_json(self):
        ctx = self.ctx(web={"allow_private": True, "timeout": 10})
        r = web.WebFetch().run({"url": self.base + "/redir", "max_chars": 600}, ctx)
        self.assertIn("Title: My Page", r.text)
        self.assertIn("call again with start=600", r.text)
        r2 = web.WebFetch().run({"url": self.base + "/redir", "max_chars": 600, "start": 600}, ctx)
        self.assertNotIn("# Heading", r2.text)
        self.assertIn('"a": 1', web.WebFetch().run({"url": self.base + "/data"}, ctx).text)
        with self.assertRaises(ToolError):
            web.WebFetch().run({"url": self.base + "/bin"}, ctx)
        with self.assertRaises(ToolError) as e:
            web.WebFetch().run({"url": self.base + "/missing"}, ctx)
        self.assertIn("404", str(e.exception))

    def test_only_http_and_https(self):
        with self.assertRaises(ToolError):
            web.http_fetch("file:///etc/passwd")
        with self.assertRaises(ToolError):
            web.http_fetch("ftp://example.com/x")


class Protocol(Base):
    def run_serve(self, srv, extra=()):
        env = dict(os.environ, YARDCODE_API_BASE=srv.url, YARDCODE_HOME=self.home, YARDCODE_DATA=self.data)
        return subprocess.Popen([sys.executable, YARDCODE, "--serve-json", "--cwd", self.cwd] + list(extra), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)

    def events_until(self, proc, stop_type, timeout=30):
        out, end = [], time.time() + timeout
        while time.time() < end:
            line = proc.stdout.readline()
            if not line:
                break
            e = json.loads(line)
            out.append(e)
            if e["type"] == stop_type:
                break
        return out

    def test_permission_round_trip_over_json(self):
        srv = self.server([{"content": "", "tool_calls": [("Write", {"path": "n.txt", "content": "hi\n"})]}, {"content": "written"}])
        p = self.run_serve(srv)
        try:
            ready = self.events_until(p, "ready")[-1]
            self.assertEqual(ready["type"], "ready")
            self.assertIn("Write", ready["tools"])
            p.stdin.write(json.dumps({"type": "user", "text": "make a file"}) + "\n")
            p.stdin.flush()
            ev = self.events_until(p, "permission")
            perm = ev[-1]
            self.assertEqual(perm["tool"], "Write")
            self.assertEqual(perm["diff"]["adds"], 1)
            p.stdin.write(json.dumps({"type": "permission_reply", "id": perm["id"], "decision": "allow", "scope": "once"}) + "\n")
            p.stdin.flush()
            rest = self.events_until(p, "done")
            types = [e["type"] for e in rest]
            self.assertIn("tool_result", types)
            self.assertEqual(rest[-1]["text"], "written")
            self.assertEqual(self.read("n.txt"), "hi\n")
        finally:
            p.stdin.close()
            p.wait(timeout=10)

    def test_commands_and_interrupt(self):
        srv = self.server([{"content": "x " * 500, "slow": 0.05}, {"content": "next"}])
        p = self.run_serve(srv)
        try:
            self.events_until(p, "ready")
            p.stdin.write(json.dumps({"type": "command", "name": "max_tokens", "arg": "77"}) + "\n")
            p.stdin.write(json.dumps({"type": "user", "text": "long"}) + "\n")
            p.stdin.flush()
            self.events_until(p, "text")
            p.stdin.write(json.dumps({"type": "interrupt"}) + "\n")
            p.stdin.flush()
            ev = self.events_until(p, "done")
            self.assertEqual(ev[-1]["type"], "done")
            self.assertEqual(srv.requests[0]["max_tokens"], 77)
        finally:
            p.stdin.close()
            p.wait(timeout=10)


class CommandLine(Base):
    def run_cli(self, args, srv=None, stdin=None, env_extra=None):
        env = dict(os.environ, YARDCODE_HOME=self.home, YARDCODE_DATA=self.data)
        if srv:
            env["YARDCODE_API_BASE"] = srv.url
        env.update(env_extra or {})
        return subprocess.run([sys.executable, YARDCODE] + args, capture_output=True, text=True, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL, env=env, cwd=self.cwd, timeout=60)

    def test_version_and_help(self):
        self.assertIn("yardcode 0.1.0", self.run_cli(["--version"]).stdout)
        h = self.run_cli(["--help"]).stdout
        for flag in ("--print", "--allowedTools", "--permission-mode", "--output-format", "--max-tokens"):
            self.assertIn(flag, h)

    def test_print_mode_text_json_and_stdin(self):
        srv = self.server([{"content": "pong"}])
        r = self.run_cli(["-p", "ping"], srv)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "pong"))
        srv.script[:] = [{"content": "from stdin"}]
        r = self.run_cli(["-p", "--output-format", "json", "summarize"], srv, stdin="some piped text")
        j = json.loads(r.stdout)
        self.assertEqual(j["result"], "from stdin")
        self.assertIn("some piped text", json.dumps(srv.requests[-1]["messages"]))

    def test_print_mode_allowed_tools_and_no_questions(self):
        srv = self.server([{"content": "", "tool_calls": [("Bash", {"command": "touch made"})]}, {"content": "done"}])
        self.run_cli(["-p", "go"], srv)
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "made")))
        srv.script[:] = [{"content": "", "tool_calls": [("Bash", {"command": "touch made"})]}, {"content": "done"}]
        self.run_cli(["-p", "--allowedTools", "Bash(touch:*)", "go"], srv)
        self.assertTrue(os.path.exists(os.path.join(self.cwd, "made")))

    def test_doctor_checks_the_connection_and_everything_it_prints_works(self):
        srv = self.server(n_ctx=12000)
        r = self.run_cli(["doctor"], srv, env_extra={"YARDCODE_CONTROL_URL": "http://127.0.0.1:1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Model API answers", r.stdout)
        self.assertIn("Context window: 12k", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_slash_commands_all_run_without_crashing(self):
        # every /command that needs no input must at least not blow up (a pty-free smoke test through the app object)
        from yardcode import slash
        from yardcode.app import App
        srv = self.server()
        os.chdir(self.cwd)
        s = self.settings(api_base=srv.url)
        app = App(s, persist=False)
        app.discover()
        app.new_agent()
        for name in ("help", "status", "context", "cost", "tools", "permissions", "mode", "hooks", "agents", "todos", "memory", "mcp", "plugins", "config", "max", "think",
                     "theme", "web", "sessions", "diff", "models"):
            try:
                slash.dispatch(app, name, "")
            except Exception as e:      # noqa: BLE001
                self.fail("/%s crashed: %r" % (name, e))

    def test_no_api_set_explains_what_to_do(self):
        r = self.run_cli(["-p", "hi"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("yardcode login", r.stderr)

    def test_config_login_flow_saves_address_and_key_privately(self):
        srv = self.server(key="k123")
        r = subprocess.run([sys.executable, YARDCODE, "login", srv.url], capture_output=True, text=True, input="k123\n", cwd=self.cwd, timeout=30,
                           env=dict(os.environ, YARDCODE_HOME=self.home, YARDCODE_DATA=self.data))
        self.assertIn("Connected", r.stdout + r.stderr)
        creds = json.load(open(os.path.join(self.home, "credentials.json")))
        self.assertEqual(creds["api_key"], "k123")
        self.assertEqual(oct(os.stat(os.path.join(self.home, "credentials.json")).st_mode & 0o777), "0o600")

    def test_models_list_against_the_control_api(self):
        api = FakeControl().start()
        self.addCleanup(api.stop)
        srv = self.server()
        r = self.run_cli(["models", "list"], srv, env_extra={"YARDCODE_CONTROL_URL": api.url, "YARDCODE_API_KEY": "k"})
        self.assertIn("qwen3-coder", r.stdout)
        self.assertIn("loaded", r.stdout)


class FakeControl:
    """A stand-in for the dashboard's /api/v1 (key protected)."""

    def __init__(self):
        self.calls = []
        self.httpd = None

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def start(self):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                outer.calls.append((self.command, self.path, body, self.headers.get("Authorization"), self.headers.get("X-Nodeyard")))
                if self.headers.get("Authorization") != "Bearer k":
                    return self.reply(401, {"ok": False, "error": "bad key"})
                if self.path == "/api/v1/models":
                    return self.reply(200, {"ok": True, "models": [{"id": "split:a", "kind": "split", "name": "qwen3-coder-30b", "file": "q.gguf", "size": 17700000000, "loaded": True, "ready": True, "nodes": ["debian-1"]},
                                                                  {"id": "split:b", "kind": "split", "name": "small", "file": "s.gguf", "size": 4700000000, "loaded": False, "nodes": ["debian-1"]}], "downloads": []})
                if self.path == "/api/v1/models/load":
                    return self.reply(200, {"ok": True, "job": "j1"})
                if self.path.startswith("/api/v1/jobs?id=j1"):
                    return self.reply(200, {"ok": True, "status": "ok", "lines": ["switching", "done"], "next": 2})
                if self.path == "/api/v1/status":
                    return self.reply(200, {"ok": True, "model": {"state": "serving", "alias": "q"}})
                self.reply(404, {"ok": False})

            do_GET = do_POST = go

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ControlClient(Base):
    def test_calls_send_the_key_and_follow_jobs(self):
        api = FakeControl().start()
        self.addCleanup(api.stop)
        m = modelapi.ModelAPI(api.url, "k")
        self.assertEqual(m.models()["models"][0]["name"], "qwen3-coder-30b")
        job = m.load("small")
        lines = []
        self.assertEqual(m.follow(job["job"], lines.append), "ok")
        self.assertEqual(lines, ["switching", "done"])
        posts = [c for c in api.calls if c[0] == "POST"]
        self.assertEqual(posts[0][2], {"model": "small"})
        self.assertEqual(posts[0][3], "Bearer k")
        self.assertEqual(posts[0][4], "1")

    def test_a_wrong_key_and_a_missing_api_are_explained(self):
        api = FakeControl().start()
        self.addCleanup(api.stop)
        with self.assertRaises(modelapi.ModelAPIError) as e:
            modelapi.ModelAPI(api.url, "bad").models()
        self.assertIn("isn't the server's API key", str(e.exception))
        self.assertEqual(e.exception.status, 401)
        with self.assertRaises(modelapi.ModelAPIError) as e:
            modelapi.ModelAPI(api.url, "k").request("GET", "/nothing")
        self.assertIn("no /api/v1 control API", str(e.exception))
        with self.assertRaises(modelapi.ModelAPIError):
            modelapi.ModelAPI("http://127.0.0.1:1", "k", timeout=2).models()


class WebViaServer(Base):
    """Web tools run through the nodeyard server (its internet connection), not this computer's."""

    def make(self, handler, via="auto", key="k"):
        from yardcode import app as appmod
        api = FakeWeb(handler).start()
        self.addCleanup(api.stop)
        s = self.settings(control_url=api.url, api_key=key, web={"via": via})
        a = appmod.App(s, persist=False)
        a.tui.warn = lambda m: a.warnings.append(m)
        a.warnings = []
        return a, api

    def test_the_server_result_is_used_and_nothing_is_fetched_locally(self):
        a, api = self.make(lambda body: (200, {"ok": True, "text": "Results from the server", "summary": "WebSearch(cats)", "preview": ["one"], "error": False}))
        ctx = self.ctx()
        ctx.web_proxy = a.make_web_proxy()
        r = web.WebSearch().run({"query": "cats", "_internal": 1}, ctx)
        self.assertEqual(r.text, "Results from the server")
        self.assertIn("via the server", r.summary)
        self.assertEqual(api.calls[0], {"tool": "WebSearch", "args": {"query": "cats"}})      # underscore keys are not sent

    def test_a_server_that_cant_do_it_falls_back_to_this_computer_once_with_a_warning(self):
        a, api = self.make(lambda body: (404, {"ok": False}))
        proxy = a.make_web_proxy()
        self.assertIsNone(proxy("WebSearch", {"query": "a"}))
        self.assertIsNone(proxy("WebSearch", {"query": "b"}))
        self.assertEqual(len(api.calls), 1)               # it doesn't keep asking a server that can't help
        self.assertEqual(len(a.warnings), 1)

    def test_a_refused_key_falls_back_too(self):
        a, api = self.make(lambda body: (401, {"ok": False, "error": "bad key"}))
        self.assertIsNone(a.make_web_proxy()("Weather", {"location": "Leeds"}))

    def test_via_server_never_falls_back(self):
        from yardcode.tools.base import ToolError
        a, api = self.make(lambda body: (404, {"ok": False}), via="server")
        with self.assertRaises(ToolError) as e:
            a.make_web_proxy()("WebSearch", {"query": "a"})
        self.assertIn("can't be reached", str(e.exception))

    def test_a_search_that_failed_on_the_server_is_the_answer_not_retried_here(self):
        from yardcode.tools.base import ToolError
        a, api = self.make(lambda body: (502, {"ok": False, "error": "No search engine answered."}))
        with self.assertRaises(ToolError) as e:
            a.make_web_proxy()("WebSearch", {"query": "a"})
        self.assertIn("No search engine answered", str(e.exception))
        self.assertIn("searched from the server", str(e.exception))
        self.assertEqual(a.warnings, [])

    def test_local_setting_and_a_missing_key_search_from_this_computer(self):
        a, api = self.make(lambda body: (200, {"ok": True, "text": "x"}), via="local")
        self.assertIsNone(a.make_web_proxy()("WebSearch", {"query": "a"}))
        a, api = self.make(lambda body: (200, {"ok": True, "text": "x"}), key="")
        self.assertIsNone(a.make_web_proxy()("WebSearch", {"query": "a"}))
        self.assertEqual(api.calls, [])


class FakeWeb(FakeControl):
    """Answers POST /api/v1/web/tool with whatever HANDLER(body) says."""

    def __init__(self, handler):
        FakeControl.__init__(self)
        self.handler = handler
        self.bodies = self.calls = []

    def start(self):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                outer.bodies.append(body)
                code, obj = outer.handler(body) if self.path == "/api/v1/web/tool" else (404, {"ok": False})
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self


class ShellMode(Base):
    def test_a_shell_keeps_its_folder_and_exports_and_captures_output(self):
        import pty
        prog = (
            "import sys\nsys.path.insert(0, %r)\nfrom yardcode import shellmode\n"
            "sh = shellmode.Shell(%r, rc=False)\n"
            "print('R1', sh.run('echo hi; cd /usr && pwd'))\n"
            "print('R2', sh.run('export FOO=bar'))\n"
            "print('R3', sh.run('echo $FOO; pwd; false'))\n"
            "print('R4', sh.run(\"printf '\\\\033[31mred\\\\033[0m\\\\n'\"))\n" % (os.path.join(REPO, "yardcode", "src"), self.cwd))
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, "-c", prog])
        out = b""
        while True:
            try:
                d = os.read(fd, 65536)
            except OSError:
                break
            if not d:
                break
            out += d
        os.waitpid(pid, 0)
        text = out.decode("utf-8", "replace").replace("\r\n", "\n")
        self.assertIn("R1 (0, 'hi\\n/usr')", text)
        self.assertIn("R3 (1, 'bar\\n/usr')", text)              # the folder and the export carried over, and the exit status came back
        self.assertIn("R4 (0, 'red')", text)                     # colour codes are stripped from what the model sees

    def test_slash_commands_for_shell_and_reset_exist(self):
        from yardcode import slash
        for name in ("shell", "reset", "clear"):
            self.assertIn(name, slash.REGISTRY)


MCP_SERVER = r'''
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    r = {"jsonrpc": "2.0", "id": m["id"]}
    if m["method"] == "initialize":
        r["result"] = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "t"}}
    elif m["method"] == "tools/list":
        r["result"] = {"tools": [{"name": "shout", "description": "Upper-case text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, "annotations": {"readOnlyHint": True}}]}
    elif m["method"] == "tools/call":
        r["result"] = {"content": [{"type": "text", "text": m["params"]["arguments"]["text"].upper()}]}
    else:
        r["error"] = {"code": -32601, "message": "no"}
    print(json.dumps(r), flush=True)
'''


class McpAndPlugins(Base):
    def test_an_mcp_server_becomes_tools(self):
        path = os.path.join(self.tmp, "mcp_server.py")
        open(path, "w").write(MCP_SERVER)
        s = self.settings(mcpServers={"demo": {"command": sys.executable, "args": [path]}})
        servers, tools = mcp.start_servers(s, self.cwd)
        try:
            self.assertEqual([t.name for t in tools], ["mcp__demo__shout"])
            self.assertTrue(tools[0].read_only)
            self.assertEqual(tools[0].run({"text": "hello"}, self.ctx()).text, "HELLO")
        finally:
            for sv in servers:
                sv.stop()

    def test_a_broken_mcp_server_is_reported_not_fatal(self):
        errs = []
        servers, tools = mcp.start_servers(self.settings(mcpServers={"bad": {"command": "/nonexistent/x"}}), self.cwd, on_error=lambda n, e: errs.append(n))
        self.assertEqual((tools, errs), ([], ["bad"]))

    def test_python_and_json_plugins(self):
        d = os.path.join(self.home, "plugins")
        os.makedirs(d)
        open(os.path.join(d, "dice.py"), "w").write('TOOLS = [{"name": "Dice", "description": "roll", "run": lambda a, c: "rolled %s" % a.get("sides", 6), "read_only": True, "kind": "read"}]\n')
        open(os.path.join(d, "broken.py"), "w").write("raise RuntimeError('boom')\n")
        json.dump({"name": "Hello", "description": "say hello", "command": ["/bin/sh", "-c", "echo hello $(cat | head -c 20)"], "read_only": True}, open(os.path.join(d, "hello.json"), "w"))
        errs = []
        found = plugins.load_plugins(self.settings(), on_error=lambda p, e: errs.append(os.path.basename(p)))
        by = {t.name: t for t in found}
        self.assertEqual(sorted(by), ["Dice", "Hello"])
        self.assertEqual(errs, ["broken.py"])
        self.assertEqual(by["Dice"].run({"sides": 20}, self.ctx()).text, "rolled 20")
        self.assertIn("hello", by["Hello"].run({}, self.ctx()).text)

    def test_project_plugins_need_trust(self):
        d = os.path.join(self.cwd, ".yardcode", "plugins")
        os.makedirs(d)
        open(os.path.join(d, "x.py"), "w").write('TOOLS = [{"name": "Evil", "description": "x", "run": lambda a, c: "x"}]\n')
        self.assertEqual(plugins.load_plugins(self.settings()), [])
        s = self.settings()
        s.trust_project()
        self.assertEqual([t.name for t in plugins.load_plugins(s)], ["Evil"])


if __name__ == "__main__":
    unittest.main()


class Update(Base):
    """yardcode update: replace this copy from a folder, the server or GitHub's format, safely."""

    def installed_copy(self):
        root = os.path.join(self.tmp, "lib", "yardcode")
        os.makedirs(os.path.join(root, "bin"))
        shutil_copy = __import__("shutil")
        shutil_copy.copy2(os.path.join(REPO, "yardcode", "bin", "yardcode"), os.path.join(root, "bin", "yardcode"))
        shutil_copy.copytree(os.path.join(REPO, "yardcode", "src", "yardcode"), os.path.join(root, "src", "yardcode"), ignore=shutil_copy.ignore_patterns("__pycache__"))
        return root

    def run_from(self, root, *args):
        # run the COPY's own code, the way a person's installed yardcode would
        return subprocess.run([sys.executable, os.path.join(root, "bin", "yardcode"), "update"] + list(args), capture_output=True, text=True, timeout=120,
                              env=dict(os.environ, YARDCODE_HOME=self.home, YARDCODE_DATA=self.data), cwd=self.cwd)

    def test_an_out_of_date_copy_is_brought_up_to_date_and_then_left_alone(self):
        root = self.installed_copy()
        with open(os.path.join(root, "src", "yardcode", "util.py"), "a") as f:
            f.write("\n# an old, different copy\n")
        r = self.run_from(root, "--from-dir", REPO, "--check")
        self.assertIn("different yardcode is available", r.stdout)
        r = self.run_from(root, "--from-dir", REPO)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Updated yardcode", r.stdout)
        self.assertNotIn("an old, different copy", open(os.path.join(root, "src", "yardcode", "util.py")).read())
        self.assertTrue(os.access(os.path.join(root, "bin", "yardcode"), os.X_OK))
        self.assertEqual(os.listdir(os.path.dirname(root)), ["yardcode"])           # no .old or .new folders left behind
        r = self.run_from(root, "--from-dir", REPO)
        self.assertIn("already up to date", r.stdout)
        r = self.run_from(root, "--version") if False else subprocess.run([sys.executable, os.path.join(root, "bin", "yardcode"), "--version"], capture_output=True, text=True)
        self.assertIn("yardcode 0.1.0", r.stdout)

    def test_the_python_an_install_chose_is_kept(self):
        root = self.installed_copy()
        launcher = os.path.join(root, "bin", "yardcode")
        lines = open(launcher).read().split("\n", 1)
        open(launcher, "w").write("#!/usr/bin/python3\n" + lines[1])
        with open(os.path.join(root, "src", "yardcode", "util.py"), "a") as f:
            f.write("\n# changed\n")
        r = self.run_from(root, "--from-dir", REPO)
        self.assertIn("Updated", r.stdout)
        self.assertEqual(open(launcher).readline().strip(), "#!/usr/bin/python3")

    def test_a_git_checkout_is_not_touched(self):
        r = subprocess.run([sys.executable, YARDCODE, "update", "--from-dir", REPO], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, YARDCODE_HOME=self.home, YARDCODE_DATA=self.data), cwd=self.cwd)
        self.assertIn("git checkout", r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_a_download_with_unsafe_paths_is_refused(self):
        import io
        import tarfile
        from yardcode import update
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ti = tarfile.TarInfo("../evil")
            ti.size = 1
            tar.addfile(ti, io.BytesIO(b"x"))
        with self.assertRaises(update.UpdateError):
            update._extract(buf.getvalue(), os.path.join(self.tmp, "x"))

    def test_the_dashboard_serves_the_same_program(self):
        sys.path.insert(0, os.path.join(REPO, "share", "nodeyard", "dashboard"))
        import updateapi
        from yardcode import update
        data, info = updateapi.bundle()
        self.assertGreater(info["files"], 20)
        d = os.path.join(self.tmp, "got")
        os.makedirs(d)
        new = update._extract(data, d)
        mine = os.path.join(REPO, "yardcode")
        self.assertEqual(update.tree_hash(new), update.tree_hash(mine))


class SharedChats(Base):
    """yardcode shares finished turns with the dashboard (against the real demo dashboard) and can continue a chat from it."""

    @classmethod
    def setUpClass(cls):
        cls.dash_tmp = __import__("tempfile").mkdtemp()
        pw = os.path.join(cls.dash_tmp, "pw")
        open(pw, "w").write("ABCD-EF01-2345-6789-ABCD-EF01")
        cls.proc = subprocess.Popen([sys.executable, os.path.join(REPO, "share", "nodeyard", "dashboard", "server.py"), "--demo", "--password-file", pw, "--port", "0", "--interval", "1"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        import re
        cls.port = None
        end = time.time() + 20
        while time.time() < end and cls.port is None:
            m = re.search(r"listening on http://[^:]*:(\d+)", cls.proc.stdout.readline() or "")
            if m:
                cls.port = int(m.group(1))
        threading.Thread(target=lambda: [None for _ in cls.proc.stdout], daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)

    def app(self, srv):
        from yardcode.app import App
        s = self.settings(api_base=srv.url, control_url="http://127.0.0.1:%d" % self.port, api_key="demo-api-key-0000-0000-0000-0000")
        os.chdir(self.cwd)
        app = App(s, persist=True)
        app.discover()
        app.new_agent()
        return app

    def test_a_finished_turn_reaches_the_dashboard_and_can_be_continued_from_there(self):
        from yardcode import sync
        srv = self.server([{"content": "first answer"}, {"content": "second answer"}])
        app = self.app(srv)
        app.send("hello from yardcode")
        app._sync_thread.join(10)
        rows = sync.list_remote(app)
        mine = [r for r in rows if r["id"] == "yc-" + app.session.id]
        self.assertEqual((mine[0]["source"], mine[0]["count"], mine[0]["title"]), ("yardcode", 2, "hello from yardcode"))
        app.send("and another")
        app._sync_thread.join(10)
        self.assertEqual([r for r in sync.list_remote(app) if r["id"] == "yc-" + app.session.id][0]["count"], 4)     # appended, not duplicated
        # a fresh yardcode (another computer) picks the conversation up and carries on
        srv2 = self.server([{"content": "third answer"}])
        app2 = self.app(srv2)
        ses, data = sync.pull(app2, "yc-" + app.session.id)
        app2.new_agent(ses, 8192)
        self.assertEqual([m["content"] for m in ses.messages if m["role"] == "assistant"], ["first answer", "second answer"])
        app2.send("continue please")
        sent = srv2.requests[0]["messages"]
        self.assertTrue(any(m["content"].startswith("hello from yardcode") for m in sent if m["role"] == "user"))
        app2._sync_thread.join(10)
        self.assertEqual([r for r in sync.list_remote(app2) if r["id"] == "yc-" + app.session.id][0]["count"], 6)

    def test_nothing_is_sent_when_sharing_is_off_or_there_is_no_key(self):
        from yardcode import sync
        srv = self.server([{"content": "x"}])
        app = self.app(srv)
        app.settings.data["sync_chats"] = False
        self.assertFalse(sync.enabled(app))
        app.settings.data["sync_chats"] = True
        app.settings.data["api_key"] = ""
        self.assertFalse(sync.enabled(app))

    def test_a_compaction_resends_the_whole_chat(self):
        from yardcode import sync
        srv = self.server([{"content": "a"}, {"content": "b"}, {"content": "c"}, {"content": "## Request\nsummary"}])
        app = self.app(srv)
        for t in ("one", "two", "three"):
            app.send(t)
            app._sync_thread.join(10)
        n_before = [r for r in sync.list_remote(app) if r["id"] == "yc-" + app.session.id][0]["count"]
        app.agent.maybe_compact(force=True)
        sync.push(app)
        n_after = [r for r in sync.list_remote(app) if r["id"] == "yc-" + app.session.id][0]["count"]
        self.assertLess(n_after, n_before)
