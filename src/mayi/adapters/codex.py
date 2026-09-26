"""PermissionRequest translation only; this module never imports an ML runtime."""

from ..core.models import AuthorizationRequest
from ..server.unix import query


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


async def run_hook(event, path, *, timeout=12.0):
    from dataclasses import asdict

    try:
        request = normalize(event)
        return translate(await query(path, asdict(request), timeout=timeout))
    except Exception:  # noqa: BLE001 - every hook failure must fall through.
        return {}
