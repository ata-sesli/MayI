import asyncio
import logging
import math
import time
import uuid

from ..julia.engine import Prediction
from . import policy
from .decision import Decision
from .models import AuthorizationRequest, AuthorizationResult, hold

logger = logging.getLogger("mayi")


class Evaluator:
    def __init__(self, model=None, *, threshold=0.98, timeout=10.0, audit=None):
        if (
            type(threshold) not in (int, float)
            or not math.isfinite(threshold)
            or not 0 <= threshold <= 1
        ):
            raise ValueError("Approval threshold must be between 0 and 1")
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Timeout must be positive")
        self.model = model
        self.threshold = threshold
        self.timeout = timeout
        self.audit = audit

    async def authorize(self, value):
        started = time.monotonic()
        request_id = str(uuid.uuid4())
        prediction = None
        request = None
        try:
            request = AuthorizationRequest.normalize(value)
            result = policy.hard_deny(request) or policy.known_safe(request)
            if result is None:
                result = hold("Julia unavailable")
                if self.model is not None:
                    async with asyncio.timeout(self.timeout):
                        prediction = await self.model.evaluate(request)
                    if not isinstance(prediction, Prediction) or not prediction.valid():
                        prediction = None
                        result = hold("Invalid Julia output")
                    else:
                        approved = (
                            prediction.choice == "approve"
                            and prediction.approve_probability >= self.threshold
                        )
                        result = AuthorizationResult(
                            Decision.APPROVE if approved else Decision.HOLD,
                            "julia",
                            prediction.approve_probability,
                            "Approval threshold met"
                            if approved
                            else "Human review required",
                        )
        except Exception:  # noqa: BLE001 - uncertain authorization must become HOLD.
            # Never expose request data or exception strings through normal logs.
            result = hold("Invalid request or evaluation failure")
            prediction = None
        if self.audit is not None:
            try:
                self.audit.record(request_id, request, result, prediction)
            except Exception:  # noqa: BLE001 - no unaudited automatic approval.
                if result.decision != Decision.DENY:
                    result = hold("Audit persistence unavailable")
                logger.error("audit_write_failed request_id=%s", request_id)
        logger.info(
            "decision request_id=%s decision=%s source=%s latency_ms=%.2f confidence=%s",
            request_id,
            result.decision,
            result.source,
            (time.monotonic() - started) * 1000,
            result.confidence,
        )
        return result


async def authorize(request):
    """Evaluate deterministic policy; use Evaluator for a resident Julia model."""
    return await Evaluator().authorize(request)
