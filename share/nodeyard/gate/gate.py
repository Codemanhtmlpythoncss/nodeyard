#!/usr/bin/env python3
"""nodeyard model gate: your own network uses the model without its API key; everyone else needs the key.

Sits in front of the model server (llama.cpp's llama-server, which only knows "key or no key") and decides
by the address a request really comes from. It never trusts X-Forwarded-For or any other header for that.

Environment:
  PORT       port to listen on (default 31435)
  UPSTREAM   the model server, e.g. http://llama.ai-split.svc.cluster.local:8080
  API_KEY    the model's API key (optional; without one, only trusted addresses are served)
  TRUSTED    comma-separated networks that need no key (default: loopback, private ranges, Tailscale)
  PUBLIC_PORT  optional second port, on 127.0.0.1 only, where EVERY request needs the key. This is
             what Tailscale Funnel (`nodeyard public on`) points at: Funnel delivers internet
             traffic from 127.0.0.1, which the normal port would wrongly trust.

Standard library only (Python 3.8+).
"""
import hmac
import http.client
import ipaddress
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_TRUSTED = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,::1/128,fc00::/7,fd7a:115c:a1e0::/48"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host",
       "content-length", "authorization", "x-api-key", "expect"}
MAX_BODY = 32 * 1024 * 1024
WINDOW, LIMIT = 300.0, 12  # failed attempts per address per window before it is told to wait
PUBLIC_LIMIT = 60          # failed attempts on the public port, from anywhere, per window
UNAUTHORIZED = b'{"error":{"message":"Invalid API Key","type":"authentication_error","code":401}}'


def parse_networks(text):
    nets = []
    for part in (text or "").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def client_ip(text):
    ip = ipaddress.ip_address(text.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def is_trusted(addr, nets):
    try:
        ip = client_ip(addr)
    except ValueError:
        return False
    return any(ip in n for n in nets if n.version == ip.version)


def supplied_key(headers):
    auth = headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("X-Api-Key", "").strip()


class Gate:
    def __init__(self, upstream, key, trusted):
        u = urllib.parse.urlparse(upstream)
        self.host, self.port = u.hostname, u.port or 80
        self.key = key or ""
        self.nets = trusted
        self.lock = threading.Lock()
        self.fails = {}

    def locked_out(self, ip, now=None):
        now = now or time.time()
        with self.lock:
            self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < WINDOW]
            return len(self.fails[ip]) >= (PUBLIC_LIMIT if ip == "public:*" else LIMIT)

    def failed(self, ip, now=None):
        with self.lock:
            self.fails.setdefault(ip, []).append(now or time.time())

    def decide(self, ip, headers, path, public=False):
        """('ok' | 'unauthorized' | 'forbidden' | 'wait', why)"""
        if path.split("?")[0] in ("/health", "/gate-health"):
            return "ok", "health"
        if public:
            # From the internet (through Tailscale Funnel): the key is always needed. The
            # forwarded address only decides whose failures are counted, never trust.
            ip = "public:" + (headers.get("X-Forwarded-For", "").split(",")[0].strip() or ip)
            if self.locked_out("public:*"):
                return "wait", "too many wrong keys from the internet"
        elif is_trusted(ip, self.nets):
            return "ok", "your network"
        if not self.key:
            return "forbidden", "no API key is set, so only your own network can use this"
        if self.locked_out(ip):
            return "wait", "too many wrong keys"
        if hmac.compare_digest(supplied_key(headers).encode(), self.key.encode()):
            return "ok", "key"
        self.failed(ip)
        if public:
            self.failed("public:*")
        return "unauthorized", "wrong or missing key"


class Handler(BaseHTTPRequestHandler):
    server_version = "nodeyard-gate"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, body, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _body(self):
        te = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in te:
            data = b""
            while True:
                size = int((self.rfile.readline().split(b";")[0].strip() or b"0"), 16)
                if size == 0:
                    self.rfile.readline()
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
                if len(data) > MAX_BODY:
                    return None
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return None
        return self.rfile.read(n) if n else None

    def handle_any(self):
        gate = self.server.gate
        ip = self.client_address[0]
        if self.path.split("?")[0] == "/gate-health":
            return self._reply(200, b"ok", "text/plain")
        verdict, why = gate.decide(ip, self.headers, self.path, getattr(self.server, "public", False))
        if verdict != "ok":
            sys.stderr.write("gate: %s %s from %s: %s\n" % (verdict, self.command, ip, why))
            sys.stderr.flush()
            if verdict == "wait":
                return self._reply(429, b'{"error":{"message":"Too many wrong keys. Try again in a few minutes.","type":"rate_limit","code":429}}', extra={"Retry-After": "300"})
            if verdict == "forbidden":
                return self._reply(403, b'{"error":{"message":"This address needs an API key and none is set up.","type":"forbidden","code":403}}')
            return self._reply(401, UNAUTHORIZED, extra={"WWW-Authenticate": "Bearer"})
        try:
            body = self._body()
        except (ValueError, OSError):
            return self._reply(400, b'{"error":{"message":"Bad request body.","code":400}}')
        if body is None and (self.headers.get("Content-Length") or "0") != "0":
            return self._reply(413, b'{"error":{"message":"Request too large.","code":413}}')
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        if gate.key:
            headers["Authorization"] = "Bearer " + gate.key
        conn = http.client.HTTPConnection(gate.host, gate.port, timeout=10)  # connecting must be quick...
        try:
            conn.connect()
            conn.sock.settimeout(900)  # ...answering can take minutes (a long prompt on slow machines)
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException):
            conn.close()
            return self._reply(502, b'{"error":{"message":"The model server isn\'t answering. It may be loading.","code":502}}')
        try:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            length = resp.getheader("Content-Length")
            if length and not resp.getheader("Transfer-Encoding"):
                self.send_header("Content-Length", length)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while True:
                chunk = resp.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the caller left; closing the upstream connection stops the model's work
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = handle_any


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DualStackServer(Server):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def listen(port):
    """IPv4 and IPv6 on one socket when the machine has IPv6; plain IPv4 otherwise."""
    try:
        return DualStackServer(("::", port), Handler)
    except OSError:
        return Server(("0.0.0.0", port), Handler)


def main():
    port = int(os.environ.get("PORT", "31435"))
    upstream = os.environ.get("UPSTREAM", "http://llama.ai-split.svc.cluster.local:8080")
    try:
        nets = parse_networks(os.environ.get("TRUSTED", DEFAULT_TRUSTED))
    except ValueError as e:
        sys.exit("gate: bad TRUSTED list: %s" % e)
    srv = listen(port)
    srv.gate = Gate(upstream, os.environ.get("API_KEY", ""), nets)
    srv.public = False
    print("nodeyard model gate on :%d -> %s (no key needed from: %s)" % (port, upstream, ", ".join(str(n) for n in nets)), flush=True)
    public_port = int(os.environ.get("PUBLIC_PORT", "0") or 0)
    if public_port:
        pub = Server(("127.0.0.1", public_port), Handler)
        pub.gate, pub.public = srv.gate, True
        threading.Thread(target=pub.serve_forever, daemon=True).start()
        print("public port 127.0.0.1:%d: the key is always needed (for Tailscale Funnel)" % public_port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
