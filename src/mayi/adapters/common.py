"""Shared hook client: adapters translate; the daemon alone evaluates."""

import time
import uuid

from ..server.unix import query
from ..telemetry import append_routing, milliseconds


def get_adapter(agent):
    from . import claude, codex, opencode

    return {"codex": codex, "claude": claude, "opencode": opencode}[agent]


async def run_hook(
    event,
    path,
    *,
    agent="codex",
    timeout=12.0,
    endpoints=(),
    connect_timeout=1.0,
    state_file=None,
    telemetry_file=None,
):
    from dataclasses import asdict

    adapter = get_adapter(agent)

    started = time.monotonic()
    routing = {
        "kind": "hook_routing",
        "request_id": str(uuid.uuid4()),
        "attempts": [],
        "selected_endpoint": None,
        "agent": agent,
    }
    capture = (
        isinstance(event, dict) and event.get("hook_event_name") == "UserPromptSubmit"
    )
    try:
        outcome = (
            isinstance(event, dict)
            and event.get("hook_event_name") in {"PostToolUse", "PostToolUseFailure"}
        )
        routing["hook_event"] = (
            event.get("hook_event_name") if isinstance(event, dict) else None
        )
        if capture:
            request = {"action": "user_prompt", "prompt": adapter.user_prompt(event)}
        elif outcome:
            request = {"action": "event", "event": adapter.tool_outcome(event)}
            routing["event_id"] = request["event"]["id"]
        else:
            normalized = adapter.normalize(event)
            normalized.metadata["request_id"] = routing["request_id"]
            request = asdict(normalized)
        if endpoints:
            from ..hook_client import query_endpoints

            result = await query_endpoints(
                endpoints,
                request,
                timeout=timeout,
                connect_timeout=connect_timeout,
                state_file=state_file,
                routing=routing,
            )
        else:
            result = await query(path, request, timeout=timeout)
            routing["selected_endpoint"] = 0
        routing["result"] = (
            result.get("stored") if outcome or capture else result.get("decision")
        )
        if result.get("request_id"):
            routing["server_request_id"] = result["request_id"]
        if capture:
            if result.get("stored") is True:
                return adapter.capture_success(result)
            return adapter.capture_block()
        return {} if outcome else adapter.translate(result)
    except Exception:  # noqa: BLE001 - capture blocks; permission failures fall through.
        routing["result"] = "error"
        if capture:
            return adapter.capture_block()
        return {} if outcome else adapter.translate({})
    finally:
        routing["total_ms"] = milliseconds(started)
        append_routing(telemetry_file, routing)
