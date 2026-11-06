import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import controlapi  # noqa: E402


class FakeBackend:
    class Store:
        @staticmethod
        def snapshot():
            return {"state": {"ai": {"split": {"model": "sample.gguf", "loaded": False, "ready": False,
                                                "alias": "sample", "shares": []}}}}

    store = Store()

    @staticmethod
    def disk_models():
        return {"in_use": "sample.gguf", "nodes": [{"node": "node-1", "items": [
            {"kind": "model", "name": "sample.gguf", "bytes": 100},
        ]}], "downloads": []}

    @staticmethod
    def ollama_overview():
        return []

    @staticmethod
    def targets():
        return {"targets": [
            {"id": "split", "kind": "split", "name": "sample", "model": "sample.gguf", "ready": False},
            {"id": "ollama:pod-1:loading", "kind": "ollama", "name": "loading", "ready": False},
            {"id": "ollama:pod-1:qwen", "kind": "ollama", "name": "qwen", "ready": True},
        ]}


class ControlAPIOpenAIModelsTests(unittest.TestCase):
    def test_models_route_exposes_openai_list_and_preserves_nodeyard_inventory(self):
        ctx = SimpleNamespace(ai=FakeBackend(), get_routes={}, post_routes={}, post_limits={})
        handler = SimpleNamespace(_json=lambda payload, *args, **kwargs: setattr(handler, "payload", payload))
        fake_webtools = SimpleNamespace(register=lambda ctx, args: None)

        with patch.dict(sys.modules, {"webtools": fake_webtools}):
            controlapi.register(ctx, SimpleNamespace())
        ctx.get_routes["/api/v1/models"](handler, {})

        self.assertEqual(handler.payload["object"], "list")
        self.assertEqual([model["id"] for model in handler.payload["data"]], ["split", "ollama:pod-1:loading", "ollama:pod-1:qwen"])
        self.assertTrue(all(model["object"] == "model" for model in handler.payload["data"]))
        split = next(model for model in handler.payload["data"] if model["id"] == "split")
        self.assertEqual(split["nodeyard"]["state"], "unloaded")
        self.assertFalse(split["nodeyard"]["loaded"])
        self.assertEqual(handler.payload["models"][0]["file"], "sample.gguf")
        self.assertEqual(handler.payload["downloads"], [])

    def test_models_route_lists_downloaded_split_models_without_a_deployment(self):
        class UndeployedBackend(FakeBackend):
            store = SimpleNamespace(snapshot=lambda: {"state": {"ai": {}}})

            @staticmethod
            def targets():
                return {"targets": [{"id": "ollama:pod-1:qwen", "kind": "ollama", "name": "qwen", "ready": True}]}

        ctx = SimpleNamespace(ai=UndeployedBackend(), get_routes={}, post_routes={}, post_limits={})
        handler = SimpleNamespace(_json=lambda payload, *args, **kwargs: setattr(handler, "payload", payload))
        fake_webtools = SimpleNamespace(register=lambda ctx, args: None)
        with patch.dict(sys.modules, {"webtools": fake_webtools}):
            controlapi.register(ctx, SimpleNamespace())
        ctx.get_routes["/api/v1/models"](handler, {})

        split = next(model for model in handler.payload["data"] if model["id"] == "split:sample.gguf")
        self.assertEqual(split["nodeyard"]["backend"], "clustered")
        self.assertFalse(split["nodeyard"]["ready"])

    def test_chat_target_starts_loading_for_an_unloaded_split_model(self):
        class Backend(FakeBackend):
            store = SimpleNamespace(snapshot=lambda: {"state": {"ai": {"split": {
                "model": "sample.gguf", "loaded": False, "ready": False, "ctx": 4096, "shares": [],
            }}}})

            def __init__(self):
                self.started = []

            def run(self, action, params):
                self.started.append((action, params))
                return "load-job-1"

        backend = Backend()
        rows, _ = controlapi.describe(backend)
        target, job_id = controlapi.prepare_chat_target(backend, "split:sample.gguf", {"context_length": 8192}, rows)

        self.assertEqual(target, "split")
        self.assertEqual(job_id, "load-job-1")
        self.assertEqual(backend.started, [("split-load", {})])

    def test_loading_event_is_an_openai_chunk_with_30_second_eta_updates(self):
        event = controlapi.loading_chunk("turn-1", "split:sample.gguf", "clustered", "loading", 30, 70)
        self.assertEqual(event["object"], "chat.completion.chunk")
        self.assertEqual(event["choices"][0], {"index": 0, "delta": {}, "finish_reason": None})
        self.assertEqual(event["nodeyard"]["progress_pct"], 30)
        self.assertEqual(event["nodeyard"]["eta_seconds"], 70)
        self.assertEqual(event["nodeyard"]["update_interval_seconds"], 30)

    def test_eta_is_only_returned_when_observed_progress_is_positive(self):
        self.assertEqual(controlapi.estimate_eta([(0.0, 0.0), (30.0, 30.0)], now=30.0, pct=30.0), 70)
        self.assertIsNone(controlapi.estimate_eta([(0.0, 10.0)], now=30.0, pct=30.0))
        self.assertIsNone(controlapi.estimate_eta([(0.0, 30.0), (30.0, 30.0)], now=30.0, pct=30.0))

    def test_ollama_load_status_matches_the_requested_pod_and_model(self):
        class Backend:
            @staticmethod
            def ollama_overview():
                return [{"pod": "ollama-0", "models": [{"name": "qwen:7b", "loaded": False}]}]

        self.assertFalse(controlapi.ollama_loaded(Backend(), "ollama:ollama-0:qwen:7b"))
        self.assertIsNone(controlapi.ollama_loaded(Backend(), "ollama:ollama-1:qwen:7b"))

    def test_split_load_emits_progress_again_after_30_seconds(self):
        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        class Backend:
            def __init__(self, clock):
                self.clock = clock
                self.store = SimpleNamespace(snapshot=self.snapshot)

            def snapshot(self):
                pct = min(30, self.clock.now)
                ready = self.clock.now >= 31
                return {"state": {"ai": {"split": {"ready": ready, "load": {"phase": "ready" if ready else "loading", "pct": 100 if ready else pct}}}}}

            @staticmethod
            def job(job_id, since):
                return {"status": "running", "lines": []}

        class Turn:
            id = "turn-1"

            def __init__(self):
                self.events = []

            def push(self, event):
                self.events.append(event)

        clock = Clock()
        backend = Backend(clock)
        turn = Turn()
        ready = controlapi.wait_for_split_load(backend, "split:sample.gguf", "job-1", turn,
                                               timeout=60, clock=clock, pause=clock.sleep)
        self.assertTrue(ready)
        updates = [event for event in turn.events if event["nodeyard"]["stage"] == "loading"]
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[-1]["nodeyard"]["progress_pct"], 30)
        self.assertEqual(updates[-1]["nodeyard"]["eta_seconds"], 70)

    def test_chat_request_loads_split_then_streams_openai_chunks(self):
        class Store:
            def __init__(self):
                self.loading_started = False
                self.calls = 0

            def snapshot(self):
                self.calls += 1
                ready = self.loading_started and self.calls >= 3
                return {"state": {"ai": {"split": {"model": "sample.gguf", "loaded": self.loading_started,
                                                         "ready": ready, "ctx": 4096, "shares": [],
                                                         "load": {"phase": "loading", "pct": 30}}}}}

        class Connection:
            nodeyard_stopped = False

            @property
            def sock(self):
                return None

            @staticmethod
            def close():
                pass

        class Response:
            def __init__(self):
                self.lines = [
                    b'data: {"id":"upstream","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n',
                    b"data: [DONE]\n",
                ]

            def readline(self):
                return self.lines.pop(0) if self.lines else b""

        class Backend(FakeBackend):
            def __init__(self):
                self.store = Store()
                self.started = []
                self.opened = None

            @staticmethod
            def targets():
                return {"targets": [{"id": "split", "kind": "split", "name": "sample", "model": "sample.gguf", "ready": False}]}

            def run(self, action, params):
                self.started.append((action, params))
                self.store.loading_started = True
                self.store.calls = 0
                return "load-job-1"

            @staticmethod
            def job(job_id, since):
                return {"id": job_id, "status": "running", "lines": []}

            def open_chat(self, target, payload):
                self.opened = (target, payload)
                return Connection(), Response()

        class Handler:
            def __init__(self):
                self.wfile = SimpleNamespace(data=bytearray(), write=lambda b: self.wfile.data.extend(b), flush=lambda: None)

            def send_response(self, code):
                self.status = code

            def send_header(self, key, value):
                pass

            def end_headers(self):
                pass

        backend = Backend()
        ctx = SimpleNamespace(ai=backend, get_routes={}, post_routes={}, post_limits={})
        fake_webtools = SimpleNamespace(register=lambda ctx, args: None)
        handler = Handler()
        with patch.dict(sys.modules, {"webtools": fake_webtools}):
            controlapi.register(ctx, SimpleNamespace())
        ctx.post_routes["/api/v1/chat/completions"](handler, {
            "model": "split", "messages": [{"role": "user", "content": "hello"}],
        })

        self.assertEqual(backend.started, [("split-load", {})])
        self.assertIsNotNone(backend.opened)
        self.assertEqual(backend.opened[0], "split")
        stream = bytes(handler.wfile.data)
        self.assertIn(b'"nodeyard": {"backend": "clustered"', stream)
        self.assertIn(b'"content":"hello"', stream)
        self.assertEqual(stream.count(b"data: [DONE]"), 1)


if __name__ == "__main__":
    unittest.main()
