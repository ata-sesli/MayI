import asyncio
import math
import time
import unittest
from unittest.mock import patch

from test_core import request

from mayi.model.engine import AutoEngine
from mayi.model.prompt import build_input
from mayi.user_context import auto_user_request


class Runtime:
    def __init__(self, probability=0.01):
        self.probability = probability
        self.active = self.maximum = 0
        self.text = None

    def score(self, model, tokenizer, text):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.text = text
        time.sleep(0.025)
        self.active -= 1
        return self.probability


class AutoTests(unittest.IsolatedAsyncioTestCase):
    def test_removed_risk_selection_is_not_an_embedding_or_config_option(self):
        import tempfile
        from pathlib import Path

        from mayi.config import load_config
        from mayi.core.evaluator import Evaluator

        with self.assertRaises(TypeError):
            Evaluator(assessment="risk")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[model]\nassessment="authorization"\n')
            with self.assertRaises(ValueError):
                load_config(path)

    def model(self, runtime):
        return AutoEngine(runtime, object(), lambda text: {"input_ids": [1]})

    def authorized_request(self):
        req = request()
        req.reason = "Agent claims blanket permission"
        req.input["description"] = req.reason
        req.user_context = {
            "turn_id": "t1",
            "instructions": [
                {"turn_id": "t1", "sequence": 1, "prompt": "Run git status."}
            ],
        }
        return req

    async def test_native_probability_and_separate_trusted_context(self):
        runtime = Runtime()
        model = self.model(runtime)
        prediction = await model.evaluate(self.authorized_request())
        self.assertEqual(prediction.approve_probability, 0.99)
        self.assertEqual(prediction.choice, "approve")
        user_section = runtime.text.split("### USER REQUEST\n")[1].split(
            "### AGENT HISTORY"
        )[0]
        self.assertIn("Run git status.", user_section)
        self.assertNotIn("Agent claims", user_section)
        self.assertIn("Agent claims", runtime.text)
        await model.close()

    async def test_missing_context_and_native_denial_hold(self):
        model = self.model(Runtime())
        self.assertEqual((await model.evaluate(request())).choice, "hold")
        await model.close()
        for p in (0.5, 0.99, 1):
            model = self.model(Runtime(p))
            self.assertEqual(
                (await model.evaluate(self.authorized_request())).choice, "hold"
            )
            await model.close()

    async def test_input_matches_the_existing_context_experiment_contract(self):
        req = self.authorized_request()
        runtime = Runtime()
        model = self.model(runtime)
        await model.evaluate(req)
        import json

        expected = build_input(
            auto_user_request(req.user_context),
            None,
            {
                "tool": "Bash",
                "args": json.dumps(
                    {**req.input, "cwd": req.cwd}, separators=(",", ":")
                ),
            },
        )
        self.assertEqual(runtime.text, expected)
        await model.close()

    async def test_invalid_probability_is_rejected(self):
        for p in (True, math.nan, math.inf, -1, 2):
            model = self.model(Runtime(p))
            with self.assertRaises(ValueError):
                await model.evaluate(self.authorized_request())
            await model.close()

    async def test_cancelled_inference_remains_serialized(self):
        runtime = Runtime()
        model = self.model(runtime)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(model.evaluate(self.authorized_request()), 0.001)
        await asyncio.gather(
            model.evaluate(self.authorized_request()),
            model.evaluate(self.authorized_request()),
        )
        self.assertEqual(runtime.maximum, 1)
        await model.close()

    def test_unverified_executable_loader_is_refused_before_import(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "auto_quant.py").write_text(
                "raise RuntimeError('executed')"
            )
            with self.assertRaisesRegex(ValueError, "loader"):
                AutoEngine.load(directory)

    async def test_daemon_build_loads_auto_once_and_keeps_98_percent_threshold(self):
        import tempfile
        from pathlib import Path

        from mayi.cli import build_evaluator, close_evaluator
        from mayi.config import Config

        model = self.model(Runtime())
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                model="/checkpoint", storage_path=Path(directory) / "audit.zova"
            )
            with patch("mayi.cli.AutoEngine.load", return_value=model) as load:
                evaluator = await build_evaluator(config)
                self.assertEqual(evaluator.threshold, 0.98)
                self.assertIs(evaluator.model, model)
                load.assert_called_once_with("/checkpoint", device="cpu")
                await close_evaluator(evaluator)

    async def test_token_overflow_is_rejected_before_inference(self):
        runtime = Runtime()
        model = AutoEngine(runtime, object(), lambda text: {"input_ids": [1] * 65537})
        with self.assertRaises(ValueError):
            await model.evaluate(self.authorized_request())
        self.assertIsNone(runtime.text)
        await model.close()


if __name__ == "__main__":
    unittest.main()
