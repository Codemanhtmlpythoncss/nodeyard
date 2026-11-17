"""Train AI settings as a JSON file: the specification, the upload check, and Start enforcing the same ranges."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "dashboard"))
sys.path.insert(0, str(ROOT / "share" / "nodeyard" / "jobs"))

import trainapi  # noqa: E402
import trainsettings as ts  # noqa: E402

MODELS = [{"name": "best-yet", "vocabulary": 16384, "context": 256, "width": 640, "layers": 13, "heads": 10}]
DATA = ["dolly-15k.jsonl", "tinystories.txt", "starter-chat.txt"]
MACHINES = ["debian-1", "debian-1 · CPU", "k8s-control"]


def check(doc, current=None):
    return ts.check(doc, current=current, datasets=DATA, models=MODELS, machines=MACHINES)


class SpecTests(unittest.TestCase):
    def test_every_setting_the_page_saves_is_specified(self):
        self.assertEqual(set(trainapi.Trainer.PRESET_KEYS), set(ts.BY_KEY))
        for s in ts.SETTINGS:
            self.assertTrue(s["meaning"] and s["label"], s["key"])
            if s["type"] in ("integer", "number"):
                self.assertLess(s["min"], s["max"], s["key"])
                if s["default"] is not None:
                    self.assertTrue(s["min"] <= s["default"] <= s["max"], s["key"])

    def test_the_text_for_an_ai_names_every_key_and_its_example_is_valid(self):
        text = ts.ai_text()
        for keys in ts.SECTIONS.values():
            for s in keys:
                self.assertIn("- %s (" % s["key"], text)
        self.assertIn(json.dumps(ts.EXAMPLE, indent=2), text)
        out = ts.check(ts.EXAMPLE, datasets=["dolly-15k.jsonl", "tinystories.txt"], models=[{"name": "my-model"}], machines=["debian-1"])
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["warnings"], [])
        self.assertEqual(out["settings"], ts.EXAMPLE["settings"])
        json.dumps(ts.spec())   # the page reads it as JSON


class StartUsesTheSpecTests(unittest.TestCase):
    BASE = {"dim": 32, "heads": 2, "steps": 10, "batch_size": 1, "context": 8, "layers": 1, "vocab_size": 300, "learning_rate": 0.001}

    def test_start_refuses_just_past_each_limit(self):
        with tempfile.TemporaryDirectory() as d:
            t = trainapi.Trainer(d)
            t.paste({"name": "d.txt", "text": "x" * 2000})

            def past_the_checks(*_a, **_k):   # never let a value that slips through start a real job
                raise trainapi.TrainError("got past the checks")
            t.machines = past_the_checks
            for s in ts.SETTINGS:
                if "min" not in s:
                    continue
                for bad in (s["max"] * 2 + 1, s["min"] - 1 if s["type"] == "integer" else -0.5):
                    with self.assertRaises(trainapi.TrainError, msg=(s["key"], bad)) as e:
                        t.start({"data": ["d.txt"], "settings": dict(self.BASE, **{s["key"]: bad})})
                    self.assertIn(s["key"], str(e.exception))
            self.assertFalse(t.running())

    def test_the_check_endpoint_knows_what_is_here(self):
        with tempfile.TemporaryDirectory() as d:
            t = trainapi.Trainer(d)
            t.paste({"name": "d.txt", "text": "x" * 2000})
            out = t.check_settings({"file": {"run": {"data": ["d.txt", "gone.txt"]}}, "current": self.BASE})
        self.assertEqual(out["run"]["data"], ["d.txt"])
        self.assertIn("gone.txt", out["warnings"][0])


class CheckTests(unittest.TestCase):
    def test_a_good_file_comes_back_cast_and_complete(self):
        out = check({"format": ts.FORMAT, "version": 1, "note": "x",
                     "settings": {"learning_rate": 3e-4, "batch_size": 16.0, "dropout": 0, "assistant_only": True, "device": "cuda"},
                     "run": {"data": ["dolly-15k.jsonl"], "model": "best-yet", "continue": True, "machines": ["debian-1"], "together": "fastest"},
                     "stop_when": {"on": True, "give_up": 8, "target_loss": None}})
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["settings"], {"learning_rate": 3e-4, "batch_size": 16, "dropout": 0.0, "assistant_only": True, "device": "cuda"})
        self.assertIsInstance(out["settings"]["batch_size"], int)
        self.assertEqual(out["stop_when"], {"on": True, "give_up": 8, "target_loss": None})

    def test_wrong_types_ranges_and_names_are_all_listed(self):
        out = check({"settings": {"learning_rate": "3e-4", "batch_size": 1.5, "steps": True, "dropout": 0.9, "device": "gpu",
                                  "learning_rat": 1, "lr": 1},
                     "run": {"together": "both", "data": "a.txt"}, "extra": 1})
        errs = "\n".join(out["errors"])
        for want in ("settings.learning_rate must be a number without quotes", "settings.batch_size must be a whole number",
                     "settings.steps must be a number", "settings.dropout must be 0.0 to 0.5", "settings.device must be one of",
                     "Did you mean learning_rate?", "Unknown key settings.lr", "run.together must be one of",
                     "run.data must be a list", "Unknown key extra"):
            self.assertIn(want, errs)
        self.assertEqual(out["settings"], {})

    def test_a_bare_settings_object_works(self):
        out = check({"learning_rate": 0.0003, "warmup": 200})
        self.assertEqual((out["errors"], out["settings"]), ([], {"learning_rate": 0.0003, "warmup": 200}))

    def test_dim_and_heads_are_checked_with_the_pages_values(self):
        self.assertIn("divide", check({"settings": {"dim": 100}}, current={"dim": 640, "heads": 8})["errors"][0])
        self.assertEqual(check({"settings": {"dim": 512}}, current={"dim": 640, "heads": 8})["errors"], [])
        self.assertEqual(check({"settings": {"steps": 5}}, current={"dim": 30, "heads": 7})["errors"], [])   # not the file's doing

    def test_continuing_needs_the_model_and_keeps_its_shape(self):
        self.assertIn("no saved model called nope", check({"run": {"model": "nope", "continue": True}})["errors"][0])
        self.assertIn("must name", check({"run": {"continue": True}})["errors"][0])
        out = check({"settings": {"dim": 512, "heads": 8, "vocab_size": 16384}, "run": {"model": "best-yet", "continue": True}})
        self.assertEqual(out["errors"], [])
        self.assertIn("dim 512 (it has 640)", out["warnings"][0])
        self.assertNotIn("vocab_size", out["warnings"][0])
        self.assertIn("already called best-yet", check({"run": {"model": "best-yet"}})["warnings"][0])

    def test_missing_files_and_machines_are_left_out(self):
        out = check({"run": {"data": ["tinystories.txt", "nope.txt"], "machines": ["debian-1", "ghost"]}})
        self.assertEqual((out["run"]["data"], out["run"]["machines"]), (["tinystories.txt"], ["debian-1"]))
        self.assertEqual(len(out["warnings"]), 2)
        none = check({"run": {"data": ["nope.txt"]}})
        self.assertNotIn("data", none["run"])
        self.assertEqual(len(none["warnings"]), 1)
        self.assertEqual(check({"run": {"data": [], "machines": []}}), check({"run": {}}))   # an empty list keeps the ticks

    def test_own_stop_prompts(self):
        out = check({"stop_when": {"on": True, "prompts": [{"prompt": "What is 12 plus 7?", "expect": "19, nineteen"}]}})
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["stop_when"]["preset"], "own")
        self.assertEqual(out["stop_when"]["prompts"][0]["expect"], ["19", "nineteen"])
        data = check({"stop_when": {"preset": "data", "prompts": [{"prompt": "Hi", "answer": "Hello!"}]}})["stop_when"]
        self.assertEqual(data["preset"], "data")
        self.assertIn("expect word", check({"stop_when": {"on": True, "prompts": [{"prompt": "Hi"}]}})["errors"][0])
        self.assertIn("unknown key", check({"stop_when": {"prompts": [{"prompt": "Hi", "expected": ["x"]}]}})["errors"][0])

    def test_not_an_object_or_another_format(self):
        self.assertTrue(check([1, 2])["errors"])
        self.assertIn("format must be", check({"format": "other", "settings": {}})["errors"][0])
        self.assertIn("version 2", check({"version": 2})["warnings"][0])


if __name__ == "__main__":
    unittest.main()
