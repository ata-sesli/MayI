import json
import unittest
from pathlib import Path

from test_core import RULES, Model

from mayi.core.evaluator import Evaluator
from mayi.evaluation import evaluate


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_count_false_approvals_against_human_labels(self):
        fixtures = [
            {"command": "git status", "expected": "approve"},
            {"command": "git push", "expected": "hold"},
            {"command": "sudo x", "expected": "deny"},
        ]
        result = await evaluate(Evaluator(Model(), policy_rules=RULES), fixtures)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["false_automatic_approvals"], 1)
        self.assertAlmostEqual(result["false_automatic_approval_rate"], 1 / 3)
        self.assertAlmostEqual(result["automatic_approval_rate"], 2 / 3)
        self.assertGreaterEqual(result["p95_latency_ms"], result["median_latency_ms"])

    async def test_fixture_baseline_has_no_false_automatic_approvals(self):
        fixtures = json.loads(
            (Path(__file__).parent / "fixtures" / "permissions.json").read_text()
        )
        result = await evaluate(Evaluator(), fixtures)
        self.assertEqual(result["false_automatic_approvals"], 0)
        self.assertGreaterEqual(result["total"], 18)
