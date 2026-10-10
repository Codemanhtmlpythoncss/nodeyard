"""The dashboard chat's AI skills helper (agentapi.py): it must talk to the model the chat chose."""
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "share", "nodeyard", "dashboard"))

import agentapi  # noqa: E402
import aiapi  # noqa: E402


class FakeAI:
    def _resolve(self, target):
        if target == "split":
            return "10.43.0.9", 8080, {"Authorization": "Bearer server-key-123456"}, "qwen-coder"
        if target == "ollama:ollama-a:llama3.2:3b":
            return "10.42.1.7", 11434, {}, "llama3.2:3b"
        if target == "ollama:gone:x":
            raise aiapi.AIError("Unknown model.", 404)
        raise aiapi.AIError("No split model is deployed.", 404)


class FakeStore:
    def __init__(self, model):
        self.model = model

    def snapshot(self):
        return {"state": {"ai": {"split": {"model": self.model}}}}


def agents(model="a.gguf"):
    ctx = types.SimpleNamespace(ai=FakeAI(), store=FakeStore(model))
    args = types.SimpleNamespace(demo=False, terminal_user="")
    a = agentapi.Agents(ctx, args)
    a.bin = "/opt/yardcode/bin/yardcode"
    return a


class ModelChoice(unittest.TestCase):
    def test_an_ollama_chat_uses_that_ollama_model_not_the_split_model(self):
        argv, env = agents()._argv(["web"], 0, "ollama:ollama-a:llama3.2:3b")
        self.assertEqual(argv[argv.index("--api-base") + 1], "http://10.42.1.7:11434/v1")
        self.assertEqual(argv[argv.index("--model") + 1], "llama3.2:3b")
        self.assertEqual(env["YARDCODE_API_KEY"], "")

    def test_the_split_model_gets_the_server_key_through_the_environment(self):
        argv, env = agents()._argv(["web"], 0, "split")
        self.assertEqual(argv[argv.index("--api-base") + 1], "http://10.43.0.9:8080/v1")
        self.assertEqual(env["YARDCODE_API_KEY"], "server-key-123456")
        self.assertNotIn("server-key-123456", " ".join(argv))

    def test_a_missing_model_is_a_clear_error_not_a_crash(self):
        with self.assertRaises(agentapi.AgentError) as e:
            agents()._model("ollama:gone:x")
        self.assertEqual((str(e.exception), e.exception.code), ("Unknown model.", 404))

    def test_switching_the_running_split_model_starts_a_new_helper(self):
        a = agents("a.gguf")
        first = a._model_id("split")
        a.ctx.store.model = "b.gguf"
        self.assertNotEqual(first, a._model_id("split"))
        self.assertEqual(a._model_id("ollama:ollama-a:llama3.2:3b"), "")


if __name__ == "__main__":
    unittest.main()
