import json
import socket
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import aiapi  # noqa: E402
import controlapi  # noqa: E402


def register_chat(backend):
    ctx = SimpleNamespace(ai=backend, get_routes={}, post_routes={}, post_limits={})
    fake_webtools = SimpleNamespace(register=lambda ctx, args: None)
    with patch.dict(sys.modules, {"webtools": fake_webtools}):
        controlapi.register(ctx, SimpleNamespace())
    return ctx.post_routes["/api/v1/chat/completions"]


class NonStreamingAPITests(unittest.TestCase):
    def test_disconnect_while_waiting_for_split_load_skips_json_response(self):
        class Backend:
            store = SimpleNamespace(snapshot=lambda: {"state": {"ai": {"split": {
                "model": "sample.gguf", "loaded": False, "ready": False, "ctx": 4096, "shares": [],
            }}}})

            def __init__(self):
                self.opened = False

            @staticmethod
            def disk_models():
                return {"in_use": "", "nodes": [{"node": "node-1", "items": [
                    {"kind": "model", "name": "sample.gguf", "bytes": 100},
                ]}], "downloads": []}

            @staticmethod
            def ollama_overview():
                return []

            @staticmethod
            def targets():
                return {"targets": [{"id": "split", "kind": "split", "name": "sample", "model": "sample.gguf", "ready": False}]}

            @staticmethod
            def run(_action, _params):
                return "load-job-1"

            def open_chat(self, _target, _payload):
                self.opened = True
                return SimpleNamespace(close=lambda: None), SimpleNamespace(readline=lambda size=-1: b"")

        class Handler:
            def __init__(self, connection):
                self.connection = connection
                self.responses = []

            def _json(self, payload, status=200):
                self.responses.append((payload, status))

        client, server = socket.socketpair()
        backend = Backend()
        handler = Handler(server)
        route = register_chat(backend)

        def wait_then_disconnect(_backend, _model_id, _job_id, _turn, client_gone=lambda: False):
            client.close()
            return not client_gone()

        try:
            with patch.object(controlapi, "wait_for_split_load", side_effect=wait_then_disconnect):
                route(handler, {"model": "split", "messages": [{"role": "user", "content": "hello"}]})
        finally:
            client.close()
            server.close()

        self.assertFalse(backend.opened)
        self.assertEqual(handler.responses, [])

    def test_disconnect_while_collecting_closes_upstream_and_skips_json_error(self):
        class Connection:
            nodeyard_stopped = False

            def __init__(self):
                self.closed = threading.Event()

            def close(self):
                self.closed.set()

        class Response:
            def __init__(self, connection):
                self.connection = connection
                self.reading = threading.Event()

            def readline(self, size=-1):
                self.reading.set()
                self.connection.closed.wait(10)
                return b""

        class Backend:
            def __init__(self):
                self.connection = Connection()
                self.response = Response(self.connection)

            @staticmethod
            def ollama_overview():
                return [{"pod": "ollama-0", "models": [{"name": "qwen", "loaded": True}]}]

            def open_chat(self, _target, _payload, on_conn=None):
                if on_conn:
                    on_conn(self.connection)
                return self.connection, self.response

        class Handler:
            def __init__(self, connection):
                self.connection = connection
                self.responses = []

            def _json(self, payload, status=200):
                self.responses.append((payload, status))

        client, server = socket.socketpair()
        backend = Backend()
        handler = Handler(server)
        route = register_chat(backend)
        request = {"model": "ollama:ollama-0:qwen", "messages": [{"role": "user", "content": "hello"}]}
        worker = threading.Thread(target=route, args=(handler, request), daemon=True)
        worker.start()
        try:
            self.assertTrue(backend.response.reading.wait(1), "upstream collection did not start")
            client.close()
            self.assertTrue(backend.connection.closed.wait(2), "upstream connection was not closed after disconnect")
            worker.join(2)
            self.assertFalse(worker.is_alive(), "non-stream request stayed blocked after disconnect")
            self.assertEqual(handler.responses, [])
        finally:
            client.close()
            backend.connection.closed.set()
            server.close()
            worker.join(1)

    def test_custom_tool_call_round_trip_and_follow_up_messages_are_valid(self):
        messages = [
            {"role": "user", "content": "What is the weather?"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_custom_1", "type": "function",
                "function": {"name": "weather_lookup", "arguments": '{"city":"Paris"}'},
            }]},
            {"role": "tool", "tool_call_id": "call_custom_1", "name": "weather_lookup", "content": "Sunny."},
            {"role": "assistant", "content": "It is sunny in Paris."},
        ]

        cleaned = aiapi.clean_chat_messages(messages, allow_custom_tools=True)

        self.assertEqual(cleaned, messages)

    def test_client_tool_result_can_omit_optional_name(self):
        message = {"role": "tool", "tool_call_id": "call_custom_3", "content": "done"}
        self.assertEqual(aiapi.clean_chat_messages([message], allow_custom_tools=True), [message])
        with self.assertRaises(ValueError):
            aiapi.clean_chat_messages([message])

    def test_open_chat_does_not_submit_when_cancelled_during_connect(self):
        class FakeSocket:
            def settimeout(self, _timeout):
                pass

        class FakeConnection:
            instance = None

            def __init__(self, _host, _port, timeout):
                self.sock = FakeSocket()
                self.timeout = timeout
                self.requested = False
                self.closed = False
                FakeConnection.instance = self

            def connect(self):
                self.connected = True

            def request(self, *_args, **_kwargs):
                self.requested = True

            def getresponse(self):
                return SimpleNamespace(status=200)

            def close(self):
                self.closed = True

        backend = object.__new__(aiapi.Live)
        backend._resolve = lambda _target: ("127.0.0.1", 1234, {}, "model")

        def cancel_before_connect_finishes(conn):
            conn.nodeyard_stopped = True

        with patch.object(aiapi.http.client, "HTTPConnection", FakeConnection):
            with self.assertRaises(aiapi.AIError):
                backend._open_chat("split", {"messages": []}, on_conn=cancel_before_connect_finishes)

        self.assertFalse(FakeConnection.instance.requested)
        self.assertTrue(FakeConnection.instance.closed)

    def test_client_tools_are_api_opt_in_and_not_enabled_for_dashboard_chat(self):
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_custom_2", "type": "function",
                "function": {"name": "my_external_tool", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call_custom_2", "name": "my_external_tool", "content": "done"},
        ]
        with self.assertRaises(ValueError):
            aiapi.clean_chat_messages(messages)
        self.assertEqual(aiapi.clean_chat_messages(messages, allow_custom_tools=True), messages)

    def test_invalid_tools_are_rejected_before_loading_unloaded_split_target(self):
        class Backend:
            store = SimpleNamespace(snapshot=lambda: {"state": {"ai": {"split": {
                "model": "sample.gguf", "loaded": False, "ready": False, "ctx": 4096, "shares": [],
            }}}})

            def __init__(self):
                self.started = []

            @staticmethod
            def disk_models():
                return {"in_use": "", "nodes": [{"node": "node-1", "items": [
                    {"kind": "model", "name": "sample.gguf", "bytes": 100},
                ]}], "downloads": []}

            @staticmethod
            def targets():
                return {"targets": [{"id": "split", "kind": "split", "name": "sample", "model": "sample.gguf", "ready": False}]}

            @staticmethod
            def ollama_overview():
                return []

            def run(self, action, params):
                self.started.append((action, params))
                return "load-job-1"

        class Handler:
            def _json(self, payload, status=200):
                self.payload, self.status = payload, status

        invalid_payloads = [
            [{"type": "invalid", "function": {"name": "my_tool"}}],
            [{"type": "function", "function": {"name": "my_tool", "parameters": []}}],
            [{"type": "function", "function": {"name": "my_tool", "parameters": False}}],
            [{"type": "function", "function": {"name": "my_tool", "parameters": ""}}],
        ]
        for tools in invalid_payloads:
            with self.subTest(tools=tools):
                backend = Backend()
                ctx = SimpleNamespace(ai=backend, get_routes={}, post_routes={}, post_limits={})
                fake_webtools = SimpleNamespace(register=lambda ctx, args: None)
                with patch.dict(sys.modules, {"webtools": fake_webtools}):
                    controlapi.register(ctx, SimpleNamespace())
                handler = Handler()
                ctx.post_routes["/api/v1/chat/completions"](handler, {
                    "model": "split", "messages": [{"role": "user", "content": "hello"}],
                    "tools": tools,
                })
                self.assertEqual(handler.status, 400)
                self.assertEqual(backend.started, [])

    def test_chat_defaults_to_json_and_forwards_common_parameters(self):
        class Backend:
            def __init__(self):
                self.sent = None

            @staticmethod
            def ollama_overview():
                return [{"pod": "ollama-0", "models": [{"name": "qwen", "loaded": True}]}]

            def open_chat(self, target, payload):
                self.sent = (target, payload)
                return Connection(), Response()

        class Connection:
            nodeyard_stopped = False
            sock = None

            @staticmethod
            def close():
                pass

        class Response:
            def __init__(self):
                self.lines = [
                    b'data: {"id":"cmpl-1","object":"chat.completion.chunk","created":123,"model":"qwen","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n',
                    b'data: {"id":"cmpl-1","object":"chat.completion.chunk","created":123,"model":"qwen","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":"stop"}]}\n',
                    b"data: [DONE]\n",
                ]

            def readline(self, size=-1):
                return self.lines.pop(0) if self.lines else b""

        class Handler:
            def __init__(self):
                self.wfile = SimpleNamespace(write=lambda _data: None, flush=lambda: None)

            def send_response(self, status):
                self.status = status

            def send_header(self, _name, _value):
                pass

            def end_headers(self):
                pass

            def _json(self, payload, status=200):
                self.payload, self.status = payload, status

        backend = Backend()
        ctx = SimpleNamespace(ai=backend, get_routes={}, post_routes={}, post_limits={})
        fake_webtools = SimpleNamespace(register=lambda ctx, args: None)
        with patch.dict(sys.modules, {"webtools": fake_webtools}):
            controlapi.register(ctx, SimpleNamespace())
        handler = Handler()
        ctx.post_routes["/api/v1/chat/completions"](handler, {
            "model": "ollama:ollama-0:qwen",
            "messages": [{"role": "user", "content": "Say hello"}],
            "top_p": 0.8, "presence_penalty": 0.2, "frequency_penalty": 0.1,
            "max_completion_tokens": 128, "response_format": {"type": "json_object"},
            "stop": ["END"], "seed": 42,
            "tools": [{"type": "function", "function": {"name": "lookup", "description": "Look up a value.",
                        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}}, "parallel_tool_calls": False,
        })

        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.payload["object"], "chat.completion")
        self.assertEqual(handler.payload["choices"][0]["message"]["content"], "Hello world")
        self.assertEqual(handler.payload["choices"][0]["finish_reason"], "stop")
        sent = backend.sent
        if sent is None:
            self.fail("backend.open_chat was not called")
        sent_target, sent_payload = sent
        self.assertEqual(sent_target, "ollama:ollama-0:qwen")
        self.assertTrue(sent_payload["stream"])
        self.assertEqual(sent_payload["temperature"], 1.0)
        self.assertEqual(sent_payload["max_tokens"], 128)
        self.assertEqual(sent_payload["response_format"], {"type": "json_object"})
        for key in ("top_p", "presence_penalty", "frequency_penalty", "stop", "seed"):
            self.assertIn(key, sent_payload)
        self.assertEqual(sent_payload["tool_choice"], {"type": "function", "function": {"name": "lookup"}})
        self.assertFalse(sent_payload["parallel_tool_calls"])
        self.assertEqual(sent_payload["tools"][0]["function"]["name"], "lookup")

    def test_collector_rejects_oversized_sse_event_with_bounded_read(self):
        oversized = b'data: ' + json.dumps({"choices": [{"delta": {"content": "x" * (1 << 20)}}]}).encode() + b"\n"

        class Response:
            def __init__(self):
                self.lines = [oversized]
                self.requested_sizes = []

            def readline(self, size=-1):
                self.requested_sizes.append(size)
                return self.lines.pop(0) if self.lines else b""

        response = Response()
        with self.assertRaises(aiapi.AIError) as caught:
            controlapi.collect_openai_stream(SimpleNamespace(nodeyard_stopped=False), response, "qwen")

        self.assertEqual(caught.exception.code, 413)
        self.assertTrue(response.requested_sizes)
        self.assertTrue(all(size > 0 and size <= controlapi.MAX_OPENAI_EVENT_BYTES + 1 for size in response.requested_sizes))

    def test_nonstream_collector_assembles_incremental_tool_calls(self):
        class Response:
            def __init__(self):
                self.lines = [
                    b'data: {"id":"cmpl-2","created":123,"model":"qwen","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_","arguments":"{\\"q\\":"}}]},"finish_reason":null}]}\n',
                    b'data: {"id":"cmpl-2","created":123,"model":"qwen","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"weather","arguments":"\\"Paris\\"}"}}]},"finish_reason":"tool_calls"}]}\n',
                    b"data: [DONE]\n",
                ]

            def readline(self, size=-1):
                return self.lines.pop(0) if self.lines else b""

        result = controlapi.collect_openai_stream(SimpleNamespace(nodeyard_stopped=False), Response(), "qwen")
        tool = result["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tool["id"], "call_1")
        self.assertEqual(tool["function"], {"name": "get_weather", "arguments": "{\"q\":\"Paris\"}"})
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")


if __name__ == "__main__":
    unittest.main()
