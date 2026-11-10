import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "share" / "nodeyard" / "jobs"))
try:
    import torch  # noqa: F401
    import train_bpe_gpt_torch as T
except ImportError:   # the trainer needs PyTorch; the dashboard's own tests don't
    T = None


@unittest.skipIf(T is None, "PyTorch isn't installed here")
class AnswerTests(unittest.TestCase):
    def test_answer_score_compares_words(self):
        self.assertEqual(T.answer_score("Marry, that with no man here he is offended.", "marry that with no man here he is offended"), 1.0)
        self.assertGreater(T.answer_score("that with no man here is offended", "Marry, that with no man here he is offended"), 0.8)
        self.assertLess(T.answer_score("I will, and the fold", "Marry, that with no man here he is offended"), 0.2)
        self.assertEqual(T.answer_score("", "x"), 0.0)

    def test_quality_spec_keeps_answers(self):
        q = T.parse_quality('{"prompts": [{"prompt": "Hi", "answer": "Hello."}], "give_up": null}')
        self.assertEqual((q["prompts"][0]["answer"], q["match"], q["give_up"]), ("Hello.", "exact", 10))


if __name__ == "__main__":
    unittest.main()
