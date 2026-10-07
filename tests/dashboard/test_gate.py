"""Tests for the model gate: who needs the API key, and that it passes streams through."""
import http.client
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "share", "nodeyard", "gate"))

import gate  # noqa: E402

KEY = "model-key-123"


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def _do(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        Upstream.seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"), "body": body, "xff": self.headers.get("X-Forwarded-For")})
        if self.headers.get("Authorization") != "Bearer " + KEY and self.path != "/health":
            data = b'{"error":"upstream says no"}'
            self.send_response(401)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(3):
                piece = ("data: %d\n\n" % i).encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"0\r\n\r\n")
            return
        data = json.dumps({"ok": True, "echo": body.decode()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _do


def serve(handler_gate):
    srv = gate.Server(("127.0.0.1", 0), gate.Handler)
    srv.gate = handler_gate
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Decisions(unittest.TestCase):
    def test_default_trusted_networks(self):
        nets = gate.parse_networks(gate.DEFAULT_TRUSTED)
        for ip in ("127.0.0.1", "10.42.1.5", "192.168.1.20", "172.20.0.1", "100.64.0.10", "::1", "fd7a:115c:a1e0::d236:bd7d", "::ffff:192.168.1.5"):
            self.assertTrue(gate.is_trusted(ip, nets), ip)
        for ip in ("8.8.8.8", "203.0.113.9", "100.128.0.1", "172.32.0.1", "2001:db8::1", "not-an-ip"):
            self.assertFalse(gate.is_trusted(ip, nets), ip)

    def test_decisions(self):
        g = gate.Gate("http://x:1", KEY, gate.parse_networks(gate.DEFAULT_TRUSTED))
        self.assertEqual(g.decide("100.82.1.1", {}, "/v1/chat/completions")[0], "ok")
        self.assertEqual(g.decide("8.8.8.8", {}, "/v1/chat/completions")[0], "unauthorized")
        self.assertEqual(g.decide("8.8.8.8", {"Authorization": "Bearer " + KEY}, "/v1/x")[0], "ok")
        self.assertEqual(g.decide("8.8.8.8", {"X-Api-Key": KEY}, "/v1/x")[0], "ok")
        self.assertEqual(g.decide("8.8.8.8", {"Authorization": "Bearer nope"}, "/v1/x")[0], "unauthorized")
        self.assertEqual(g.decide("8.8.8.8", {}, "/health")[0], "ok")

    def test_forwarded_headers_are_never_trusted(self):
        g = gate.Gate("http://x:1", KEY, gate.parse_networks(gate.DEFAULT_TRUSTED))
        self.assertEqual(g.decide("8.8.8.8", {"X-Forwarded-For": "192.168.1.1", "X-Real-IP": "100.64.0.1"}, "/v1/x")[0], "unauthorized")

    def test_without_a_key_outsiders_are_refused_outright(self):
        g = gate.Gate("http://x:1", "", gate.parse_networks(gate.DEFAULT_TRUSTED))
        self.assertEqual(g.decide("8.8.8.8", {"Authorization": "Bearer "}, "/v1/x")[0], "forbidden")
        self.assertEqual(g.decide("192.168.1.2", {}, "/v1/x")[0], "ok")

    def test_guessing_gets_locked_out(self):
        g = gate.Gate("http://x:1", KEY, gate.parse_networks(gate.DEFAULT_TRUSTED))
        for _ in range(gate.LIMIT):
            self.assertEqual(g.decide("8.8.4.4", {"Authorization": "Bearer guess"}, "/v1/x")[0], "unauthorized")
        self.assertEqual(g.decide("8.8.4.4", {"Authorization": "Bearer " + KEY}, "/v1/x")[0], "wait")
        self.assertEqual(g.decide("9.9.9.9", {"Authorization": "Bearer " + KEY}, "/v1/x")[0], "ok")


class Proxying(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.up.daemon_threads = True
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.upstream = "http://127.0.0.1:%d" % cls.up.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()

    def setUp(self):
        Upstream.seen.clear()

    def call(self, srv, method="POST", path="/v1/chat/completions", headers=None, body=b'{"hi":1}'):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request(method, path, body=body if method == "POST" else None, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_your_own_network_needs_no_key_and_the_gate_adds_it(self):
        srv = serve(gate.Gate(self.upstream, KEY, gate.parse_networks("127.0.0.0/8")))
        status, data = self.call(srv)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["echo"], '{"hi":1}')
        self.assertEqual(Upstream.seen[0]["auth"], "Bearer " + KEY)

    def test_outside_addresses_need_the_key(self):
        srv = serve(gate.Gate(self.upstream, KEY, gate.parse_networks("203.0.113.0/24")))  # loopback is "outside" here
        status, data = self.call(srv)
        self.assertEqual(status, 401)
        self.assertIn(b"Invalid API Key", data)
        self.assertEqual(Upstream.seen, [])  # never reached the model
        status, _ = self.call(srv, headers={"Authorization": "Bearer " + KEY})
        self.assertEqual(status, 200)
        status, _ = self.call(srv, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)

    def test_the_callers_own_authorization_never_reaches_the_model(self):
        srv = serve(gate.Gate(self.upstream, KEY, gate.parse_networks("127.0.0.0/8")))
        self.call(srv, headers={"Authorization": "Bearer something-else", "X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(Upstream.seen[0]["auth"], "Bearer " + KEY)

    def test_streams_pass_through_as_they_come(self):
        srv = serve(gate.Gate(self.upstream, KEY, gate.parse_networks("127.0.0.0/8")))
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("GET", "/stream")
        r = c.getresponse()
        first = r.read1(100)
        self.assertIn(b"data: 0", first)
        rest = b""
        while True:
            chunk = r.read1(100)
            if not chunk:
                break
            rest += chunk
        self.assertIn(b"data: 2", first + rest)
        c.close()

    def test_a_model_that_is_down_gives_a_clear_error(self):
        srv = serve(gate.Gate("http://127.0.0.1:1", KEY, gate.parse_networks("127.0.0.0/8")))
        status, data = self.call(srv)
        self.assertEqual(status, 502)
        self.assertIn(b"may be loading", data)

    def test_health_works_for_everyone(self):
        srv = serve(gate.Gate(self.upstream, KEY, gate.parse_networks("203.0.113.0/24")))
        status, _ = self.call(srv, method="GET", path="/health")
        self.assertEqual(status, 200)
        status, data = self.call(srv, method="GET", path="/gate-health")
        self.assertEqual((status, data), (200, b"ok"))


if __name__ == "__main__":
    unittest.main()
