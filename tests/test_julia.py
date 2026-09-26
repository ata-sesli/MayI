import asyncio
import os
import time
import unittest
from unittest.mock import patch

from test_core import request

from mayi.julia.engine import JuliaEngine
from mayi.julia.prompt import QUESTIONS, state_for


class Runtime:
    def __init__(self):
        self.active = 0
        self.maximum = 0
        self.calls = 0

    def predict(self, *, state, questions):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.calls += 1
        time.sleep(0.04)
        self.active -= 1
        return {
            "answers": {
                "authorization": {
                    "type": "choice",
                    "choice": "approve",
                    "probabilities": {"approve": 0.99, "hold": 0.01},
                    "max_probability": 0.99,
                }
            }
        }


class JuliaTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_question_api_and_output(self):
        runtime = Runtime()
        result = await JuliaEngine(runtime).evaluate(request())
        self.assertEqual(result.choice, "approve")
        self.assertEqual(result.approve_probability, 0.99)
        self.assertTrue(result.valid())

    async def test_cancelled_inference_remains_serialized(self):
        runtime = Runtime()
        model = JuliaEngine(runtime)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(model.evaluate(request()), 0.01)
        results = await asyncio.gather(
            model.evaluate(request()), model.evaluate(request())
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(runtime.maximum, 1)
        await model.close()

    async def test_load_once_uses_public_options(self):
        calls = []
        runtime = Runtime()

        def load_model(*args, **kwargs):
            calls.append((args, kwargs))
            return runtime

        import types

        with patch.dict(
            "sys.modules", {"julia": types.SimpleNamespace(load_model=load_model)}
        ):
            model = JuliaEngine.load("/models/Julia-1", device="cpu")
            await model.evaluate(request())
            await model.evaluate(request())
        self.assertEqual(
            calls,
            [
                (
                    ("/models/Julia-1",),
                    {
                        "device": "cpu",
                        "strict_encoding": True,
                        "max_length": 8192,
                        "head_length": 512,
                    },
                )
            ],
        )

    def test_state_contains_only_operational_context(self):
        req = request()
        req.metadata["transcript"] = "do not include this"
        state = state_for(req)
        self.assertIn("git commit", state)
        self.assertIn("/work/project", state)
        self.assertNotIn("do not include this", state)
        self.assertEqual(
            set(QUESTIONS["authorization"]["criteria"]), {"approve", "hold"}
        )


@unittest.skipUnless(
    os.environ.get("MAYI_TEST_MODEL"), "Set MAYI_TEST_MODEL for real Julia integration"
)
class RealJuliaTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_runtime_prediction(self):
        model = await asyncio.to_thread(JuliaEngine.load, os.environ["MAYI_TEST_MODEL"])
        try:
            self.assertTrue((await model.evaluate(request())).valid())
        finally:
            await model.close()
