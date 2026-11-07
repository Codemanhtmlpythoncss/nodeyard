import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import aiapi  # noqa: E402
import turns  # noqa: E402


class TurnAPICompatibilityTests(unittest.TestCase):
    def test_api_turn_finishes_with_openai_done_sentinel(self):
        turn = turns.Turn("chatcmpl-test", "owner", "api")
        turn.finish("done")
        self.assertEqual(turn.frames[-1], b"[DONE]")

    def test_dashboard_turn_keeps_nodeyard_turn_end_event(self):
        turn = turns.Turn("chat-test", "owner", "chat")
        turn.finish("done")
        self.assertEqual(turn.frames[-1], b'{"type": "turn_end", "result": "done"}')

    def test_api_stream_emits_only_one_done_sentinel(self):
        class Response:
            def __init__(self):
                self.lines = [
                    b'data: {"id":"upstream","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n',
                    b"data: [DONE]\n",
                ]

            def readline(self):
                return self.lines.pop(0) if self.lines else b""

        class Connection:
            nodeyard_stopped = False

        turn = turns.Turn("chatcmpl-test", "owner", "api")
        self.assertEqual(aiapi.pump_stream(turn, Connection(), Response(), lambda: "cut"), "done")
        turn.finish("done")
        self.assertEqual(turn.frames.count(b"[DONE]"), 1)
        self.assertTrue(any(b'"content":"hi"' in frame for frame in turn.frames))


if __name__ == "__main__":
    unittest.main()
