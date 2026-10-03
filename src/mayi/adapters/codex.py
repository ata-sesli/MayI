"""Codex permission and outcome translation; never imports an ML runtime."""

import math
import uuid

from ..core.models import AuthorizationRequest
from .common import run_hook


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


def capture_success(result):
    return {}
