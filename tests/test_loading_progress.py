import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import controlapi as c  # noqa: E402


class LoadingProgressTests(unittest.TestCase):
    def test_updates_every_10_seconds(self):
        self.assertEqual(c.LOAD_UPDATE_SECONDS, 10)

    def test_line_is_human_readable(self):
        line = c.loading_line("loading", 45, 30)
        self.assertIn("45%", line)
        self.assertIn("Loading", line)
        self.assertTrue(line.endswith("\n"))

    def test_ready_line(self):
        self.assertIn("ready", c.loading_line("ready", 100, 0).lower())

    def test_line_without_pct_or_eta(self):
        line = c.loading_line("starting", None, None)
        self.assertIn("Loading", line)  # no crash, still says something

    def test_chunk_with_text_has_visible_content(self):
        ch = c.loading_chunk("t", "m", "clustered", "loading", 45, 30, text=c.loading_line("loading", 45, 30))
        self.assertIn("45%", ch["choices"][0]["delta"]["content"])
        self.assertEqual(ch["nodeyard"]["progress_pct"], 45)        # telemetry still present
        self.assertEqual(ch["object"], "chat.completion.chunk")

    def test_chunk_without_text_stays_empty_delta(self):
        ch = c.loading_chunk("t", "m", "clustered", "loading", 45, 30)
        self.assertEqual(ch["choices"][0]["delta"], {})            # back-compat: no stray content


if __name__ == "__main__":
    unittest.main()
