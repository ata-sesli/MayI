import asyncio
import logging
import math
import time
import uuid

from ..julia.engine import Prediction
from ..telemetry import context, milliseconds, normalize_event
from . import policy
from .decision import Decision
from .models import AuthorizationRequest, AuthorizationResult, hold

logger = logging.getLogger("mayi")


class Evaluator:
    def __init__(
        self,
        model=None,
        *,
        threshold=0.98,
        timeout=10.0,
        audit=None,
        policy_name="strict",
        model_id=None,
        device=None,
    ):
        if policy_name not in ("strict", "approve-or-hold"):
            raise ValueError("Unknown authorization policy")
        self.policy_name = policy_name
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
        self.context = context(policy_name, threshold, timeout, model_id, device)
        self.started = time.monotonic()
        self.counters = dict.fromkeys(
            (
                "requests",
                "active_requests",
                "model_timeouts",
                "model_failures",
                "audit_failures",
                "event_failures",
                "events",
                "invalid_requests",
            ),
            0,
        )
        self.decisions = dict.fromkeys(("approve", "hold", "deny"), 0)

    def health(self):
        return {
            **self.counters,
            "decisions": dict(self.decisions),
            "uptime_seconds": time.monotonic() - self.started,
        }

    def record_event(self, value):
        try:
            event = normalize_event(value)
            if self.audit is None:
                raise OSError("No event store")
            if event["kind"] == "human_feedback" and not self.audit.has_request(
                event["request_id"]
            ):
                raise ValueError("Unknown request")
            self.audit.record_event(event)
            self.counters["events"] += 1
            return {"stored": True, "event_id": event["id"]}
        except Exception:  # noqa: BLE001 - telemetry never grants permission.
            self.counters["event_failures"] += 1
            return {"stored": False}

    async def authorize(self, value):
        self.counters["requests"] += 1
        self.counters["active_requests"] += 1
        try:
            return await self._authorize(value)
        finally:
            self.counters["active_requests"] -= 1

    async def _authorize(self, value):
        started = time.monotonic()
        request_id = str(uuid.uuid4())
        prediction = None
        request = None
        timings = {
            "policy_ms": 0.0,
            "julia_ms": 0.0,
            "audit_ms": None,
            "total_ms": None,
        }
        stage = time.monotonic()
        try:
            request = AuthorizationRequest.normalize(value)
            candidate = request.metadata.get("request_id")
            if candidate is not None:
                request_id = str(uuid.UUID(candidate))
            stage = time.monotonic()
            result = policy.hard_deny(request) or policy.known_safe(request)
            timings["policy_ms"] = milliseconds(stage)
            if result is not None and result.decision == Decision.DENY:
                result.matched_rule = result.reason
                if self.policy_name == "approve-or-hold":
                    result.decision = Decision.HOLD
                    result.source = "static_hold"
            if result is None:
                result = hold("Julia unavailable")
                if self.model is not None:
                    stage = time.monotonic()
                    try:
                        async with asyncio.timeout(self.timeout):
                            prediction = await self.model.evaluate(request)
                    except TimeoutError:
                        self.counters["model_timeouts"] += 1
                        raise
                    except Exception:
                        self.counters["model_failures"] += 1
                        raise
                    finally:
                        timings["julia_ms"] = milliseconds(stage)
                    if not isinstance(prediction, Prediction) or not prediction.valid():
                        self.counters["model_failures"] += 1
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
            if request is None:
                self.counters["invalid_requests"] += 1
            # Never expose request data or exception strings through normal logs.
            result = hold("Invalid request or evaluation failure")
            prediction = None
        result.policy = self.policy_name
        result.request_id = request_id
        result.context = dict(self.context)
        result.timings = dict(timings)
        audit_started = time.monotonic()
        if self.audit is not None:
            try:
                self.audit.record(request_id, request, result, prediction)
            except Exception:  # noqa: BLE001 - no unaudited automatic approval.
                self.counters["audit_failures"] += 1
                if result.decision != Decision.DENY:
                    matched_rule = result.matched_rule
                    result = hold("Audit persistence unavailable")
                    result.policy = self.policy_name
                    result.matched_rule = matched_rule
                logger.error("audit_write_failed request_id=%s", request_id)
        timings["audit_ms"] = milliseconds(audit_started) if self.audit else 0.0
        timings["total_ms"] = milliseconds(started)
        result.request_id = request_id
        result.context = dict(self.context)
        result.timings = timings
        self.decisions[result.decision] += 1
        if self.audit is not None:
            try:
                self.audit.record_event(
                    {
                        "id": str(uuid.uuid4()),
                        "kind": "decision_completed",
                        "request_id": request_id,
                        "decision": result.decision,
                        "timings": timings,
                    }
                )
            except Exception:  # noqa: BLE001 - completion metrics are best effort.
                self.counters["event_failures"] += 1
        logger.info(
            "decision request_id=%s decision=%s source=%s latency_ms=%.2f confidence=%s policy=%s",
            request_id,
            result.decision,
            result.source,
            (time.monotonic() - started) * 1000,
            result.confidence,
            self.policy_name,
        )
        return result


async def authorize(request):
    """Evaluate deterministic policy; use Evaluator for a resident Julia model."""
    return await Evaluator().authorize(request)
