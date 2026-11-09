import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import settings  # noqa: E402

clean = settings.Settings._clean_chat_defaults


class ContextMaxTests(unittest.TestCase):
    def test_default_has_context_max_false(self):
        self.assertIn("context_max", settings.CHAT_DEFAULTS)
        self.assertIs(settings.CHAT_DEFAULTS["context_max"], False)

    def test_true_is_kept(self):
        self.assertTrue(clean({"context_max": True})["context_max"])

    def test_non_bool_falls_back_to_default(self):
        for bad in ("yes", 1, None, "true"):
            self.assertFalse(clean({"context_max": bad})["context_max"])

    def test_context_length_and_max_coexist(self):
        out = clean({"context_max": True, "context_length": 12000})
        self.assertTrue(out["context_max"])
        self.assertEqual(out["context_length"], 12000)

    def test_missing_value_defaults_false(self):
        self.assertFalse(clean({})["context_max"])


if __name__ == "__main__":
    unittest.main()
