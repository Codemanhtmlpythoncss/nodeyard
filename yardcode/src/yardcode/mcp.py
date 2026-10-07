"""A small MCP (Model Context Protocol) client for stdio servers, so any MCP server's tools become tools here.

  "mcpServers": {"files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/me"], "env": {}}}

Tools appear as mcp__SERVER__TOOL and always ask before running (unless the server marks them read-only).
"""
import json
import os
import queue
import subprocess
import threading

from .tools.base import Result, Tool, ToolError

PROTOCOL = "2024-11-05"


class MCPError(Exception):
    pass


class MCPServer:
    def __init__(self, name, spec, cwd):
        self.name = name
        self.spec = spec
        self.cwd = cwd
        self.proc = None
        self.lock = threading.Lock()
        self.pending = {}
        self.next_id = 1
        self.tools = []
        self.error = ""
        self.instructions = ""

    def start(self, timeout=20):
        cmd = self.spec.get("command")
        if not cmd:
            raise MCPError("no command")
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.spec.get("env") or {}).items()})
        try:
            self.proc = subprocess.Popen([cmd] + [str(a) for a in self.spec.get("args") or []], cwd=self.cwd, env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            raise MCPError("can't start %s: %s" % (cmd, e))
        threading.Thread(target=self._read, daemon=True).start()
        init = self.call("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "yardcode", "version": "0.1"}}, timeout)
        self.instructions = (init or {}).get("instructions", "")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listing = self.call("tools/list", {}, timeout) or {}
        self.tools = listing.get("tools", [])
        return self

    def _send(self, obj):
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            self.proc.stdin.flush()
        except (OSError, AttributeError):
            raise MCPError("the server closed the connection")

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self.lock:
                    q = self.pending.pop(msg["id"], None)
                if q is not None:
                    q.put(msg)
            elif "id" in msg and "method" in msg:   # a request from the server: we support nothing, say so
                try:
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})
                except MCPError:
                    pass
        with self.lock:
            for q in self.pending.values():
                q.put({"error": {"message": "the server stopped"}})
            self.pending.clear()

    def call(self, method, params, timeout=60):
        with self.lock:
            rid = self.next_id
            self.next_id += 1
            q = queue.Queue()
            self.pending[rid] = q
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            raise MCPError("%s timed out" % method)
        if "error" in msg:
            raise MCPError(msg["error"].get("message", "error"))
        return msg.get("result")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass


class MCPTool(Tool):
    kind = "other"

    def __init__(self, server, spec):
        self.server = server
        self.remote = spec.get("name", "")
        self.name = "mcp__%s__%s" % (_clean(server.name), _clean(self.remote))
        self.description = (spec.get("description") or self.remote)[:600] + " (MCP: %s)" % server.name
        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        self.parameters = schema
        self.read_only = bool((spec.get("annotations") or {}).get("readOnlyHint"))

    def specifier(self, args, ctx):
        return json.dumps(args, sort_keys=True)[:200]

    def summary(self, args, ctx):
        return "%s(%s)" % (self.name, json.dumps(args)[:90])

    def run(self, args, ctx):
        try:
            res = self.server.call("tools/call", {"name": self.remote, "arguments": args}, timeout=120)
        except MCPError as e:
            raise ToolError("MCP %s: %s" % (self.server.name, e))
        parts = []
        for c in (res or {}).get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "image":
                parts.append("[image]")
            elif c.get("type") == "resource":
                parts.append(json.dumps(c.get("resource", {}))[:2000])
        text = "\n".join(parts) or "(no output)"
        return Result(text, error=bool((res or {}).get("isError")), summary="%s done" % self.remote)


def _clean(s):
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(s))[:40]


def start_servers(settings, cwd, on_error=None):
    """(servers, tools) for every configured MCP server that starts."""
    servers, tools = [], []
    for name, spec in (settings.get("mcpServers") or {}).items():
        if not isinstance(spec, dict) or spec.get("disabled"):
            continue
        s = MCPServer(name, spec, cwd)
        try:
            s.start()
        except MCPError as e:
            s.error = str(e)
            s.stop()
            if on_error:
                on_error(name, str(e))
            servers.append(s)
            continue
        servers.append(s)
        tools += [MCPTool(s, t) for t in s.tools if t.get("name")]
    return servers, tools
