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
            return {"state": {"ai": {"split": {"model": "sample.gguf", "loaded": True, "ready": True,
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
            {"id": "split", "kind": "split", "name": "sample", "ready": True},
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
        self.assertEqual([model["id"] for model in handler.payload["data"]], ["split", "ollama:pod-1:qwen"])
        self.assertTrue(all(model["object"] == "model" for model in handler.payload["data"]))
        self.assertEqual(handler.payload["models"][0]["file"], "sample.gguf")
        self.assertEqual(handler.payload["downloads"], [])


if __name__ == "__main__":
    unittest.main()
