"""Validated Auto output and serialized inference."""

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from importlib import util
from pathlib import Path

from ..user_context import auto_user_request
from .prompt import build_input

LOADER_SHA256 = "f2a7eada1ef02b7e33f7ac01a4af72f64d679711d2fe48877eb6d513b0736410"


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


class SerializedEngine:
    def __init__(self, runtime):
        self.runtime = runtime
        self._lock = asyncio.Lock()
        self._pending = None

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


class AutoEngine(SerializedEngine):
    def __init__(self, runtime, model, tokenizer):
        super().__init__(runtime)
        self.model = model
        self.tokenizer = tokenizer

    @classmethod
    def load(cls, model_path, *, device="cpu"):
        source = Path(model_path) / "auto_quant.py"
        if hashlib.sha256(source.read_bytes()).hexdigest() != LOADER_SHA256:
            raise ValueError("Unverified Auto executable loader")
        import torch

        torch.set_num_threads(4)
        if torch.get_num_interop_threads() != 1:
            torch.set_num_interop_threads(1)
        spec = util.spec_from_file_location("mayi_auto_quant", source)
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)
        model, tokenizer = module.load(
            str(model_path), device=device, dtype=torch.float32, attention="linear"
        )
        return cls(module, model, tokenizer)

    def _predict(self, request):
        context = (
            auto_user_request(request.user_context) if request.user_context else None
        )
        arguments = {**request.input, "cwd": request.cwd}
        if request.operation is not None:
            arguments.setdefault(
                "command" if request.tool == "shell" else "operation", request.operation
            )
        if request.reason is not None:
            arguments.setdefault("description", request.reason)
        text = build_input(
            context,
            None,
            {
                "tool": "Bash" if request.tool == "shell" else request.tool,
                "args": json.dumps(arguments, separators=(",", ":")),
            },
        )
        if len(self.tokenizer(text)["input_ids"]) > 65536:
            raise ValueError("Auto input exceeds context; truncation refused")
        p_deny = self.runtime.score(self.model, self.tokenizer, text)
        if (
            type(p_deny) not in (int, float)
            or not math.isfinite(p_deny)
            or not 0 <= p_deny <= 1
        ):
            raise ValueError("Invalid Auto probability")
        return Prediction(
            "approve" if context and p_deny < 0.5 else "hold", 1 - p_deny, p_deny
        )
