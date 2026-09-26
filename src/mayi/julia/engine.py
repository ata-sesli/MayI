"""Validated output at the Julia runtime boundary."""

import asyncio
import math
from dataclasses import dataclass

from .prompt import QUESTIONS, state_for


@dataclass(slots=True)
class Prediction:
    choice: str
    approve_probability: float
    hold_probability: float

    def valid(self):
        values = (self.approve_probability, self.hold_probability)
        return (
            self.choice in {"approve", "hold"}
            and all(
                type(p) in (int, float) and math.isfinite(p) and 0 <= p <= 1
                for p in values
            )
            and math.isclose(sum(values), 1, abs_tol=1e-5)
        )


class JuliaEngine:
    def __init__(self, runtime):
        self.runtime = runtime
        self._lock = asyncio.Lock()
        self._pending = None

    @classmethod
    def load(cls, model_path, *, device="cpu"):
        # Julia-1 owns torch, tokenization, and all other ML dependencies.
        from julia import load_model

        return cls(
            load_model(
                str(model_path),
                device=device,
                strict_encoding=True,
                max_length=8192,
                head_length=512,
            )
        )

    def _predict(self, request):
        result = self.runtime.predict(state=state_for(request), questions=QUESTIONS)
        answer = result["answers"]["authorization"]
        if answer["type"] != "choice" or set(answer["probabilities"]) != {
            "approve",
            "hold",
        }:
            raise ValueError("Invalid Julia answer")
        prediction = Prediction(
            answer["choice"],
            answer["probabilities"]["approve"],
            answer["probabilities"]["hold"],
        )
        if not prediction.valid():
            raise ValueError("Invalid Julia probabilities")
        return prediction

    def _finished(self, task):
        if self._pending is task:
            self._pending = None
        if not task.cancelled():
            task.exception()  # Consume errors even if the requesting client timed out.

    async def evaluate(self, request):
        async with self._lock:
            # Cancellation cannot stop a running torch call. Keep that task
            # resident and wait for it before submitting another inference.
            if self._pending is not None:
                await asyncio.gather(
                    asyncio.shield(self._pending), return_exceptions=True
                )
            task = asyncio.create_task(asyncio.to_thread(self._predict, request))
            self._pending = task
            task.add_done_callback(self._finished)
            return await asyncio.shield(task)

    async def close(self):
        if self._pending is not None:
            await asyncio.gather(asyncio.shield(self._pending), return_exceptions=True)
