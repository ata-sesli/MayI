"""Codex permission and outcome translation; never imports an ML runtime."""

import math
import time
import uuid

from ..core.models import AuthorizationRequest
from ..server.unix import query
from ..telemetry import append_routing, milliseconds


def normalize(event):
    if (
        not isinstance(event, dict)
        or event.get("hook_event_name") != "PermissionRequest"
    ):
        raise ValueError("Unsupported hook event")
    tool = event.get("tool_name")
    data = event.get("tool_input")
    if not isinstance(tool, str) or not tool or not isinstance(data, dict):
        raise ValueError("Unsupported hook input")
    return AuthorizationRequest.normalize(
        {
            "agent": "codex",
            "tool": "shell" if tool == "Bash" else tool,
            "operation": data.get("command"),
            "cwd": event.get("cwd"),
            "reason": data.get("description"),
            "input": data,
            "metadata": {
                key: event[key]
                for key in ("session_id", "turn_id", "tool_use_id")
                if key in event
            },
        }
    )


def translate(result):
    decision = result.get("decision")
    if decision not in {"approve", "deny"}:
        return {}
    output = {"behavior": "allow" if decision == "approve" else "deny"}
    if decision == "deny":
        output["message"] = "Blocked by MayI policy"
    return {
        "hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": output}
    }


def tool_outcome(event):
    result = {
        "kind": "tool_outcome",
        "id": str(uuid.uuid4()),
        "tool": event.get("tool_name"),
    }
    for key in ("session_id", "turn_id", "tool_use_id"):
        if key in event:
            result[key] = event[key]
    # Only structured values are usable; never parse or retain textual output.
    response = event.get("tool_response")
    result["exit_code"] = None
    result["duration_ms"] = None
    if isinstance(response, dict):
        if type(response.get("exit_code")) is int:
            result["exit_code"] = response["exit_code"]
        seconds = response.get("wall_time_seconds")
        if (
            type(seconds) in (int, float)
            and math.isfinite(seconds)
            and 0 <= seconds <= 1e12
        ):
            result["duration_ms"] = seconds * 1000
    return result


def user_prompt(event):
    # Use documented hook fields directly; ordering is owned by the daemon.
    return {
        "id": str(uuid.uuid4()),
        "session_id": event.get("session_id"),
        "turn_id": event.get("turn_id"),
        "cwd": event.get("cwd"),
        "prompt": event.get("prompt"),
    }


def capture_block():
    return {
        "decision": "block",
        "reason": "MayI could not persist the submitted prompt.",
    }


async def run_hook(
    event,
    path,
    *,
    timeout=12.0,
    endpoints=(),
    connect_timeout=1.0,
    state_file=None,
    telemetry_file=None,
):
    from dataclasses import asdict

    started = time.monotonic()
    routing = {
        "kind": "hook_routing",
        "request_id": str(uuid.uuid4()),
        "attempts": [],
        "selected_endpoint": None,
    }
    capture = (
        isinstance(event, dict) and event.get("hook_event_name") == "UserPromptSubmit"
    )
    try:
        outcome = (
            isinstance(event, dict) and event.get("hook_event_name") == "PostToolUse"
        )
        routing["hook_event"] = (
            event.get("hook_event_name") if isinstance(event, dict) else None
        )
        if capture:
            request = {"action": "user_prompt", "prompt": user_prompt(event)}
        elif outcome:
            request = {"action": "event", "event": tool_outcome(event)}
            routing["event_id"] = request["event"]["id"]
        else:
            normalized = normalize(event)
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
            return {} if result.get("stored") is True else capture_block()
        return {} if outcome else translate(result)
    except Exception:  # noqa: BLE001 - capture blocks; permission failures fall through.
        routing["result"] = "error"
        return capture_block() if capture else {}
    finally:
        routing["total_ms"] = milliseconds(started)
        append_routing(telemetry_file, routing)
