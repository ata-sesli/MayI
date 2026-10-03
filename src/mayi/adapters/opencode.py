"""OpenCode V2 plugin bridge events, distinct from OpenCode V1 hooks."""

import uuid
from dataclasses import asdict

from ..core.models import AuthorizationRequest
from . import codex


def normalize(event):
    value = asdict(codex.normalize(event))
    value["agent"] = "opencode"
    value["metadata"]["context_turn_ids"] = event.get("context_turn_ids")
    permission = event.get("permission")
    if permission is not None:
        if (
            not isinstance(permission, dict)
            or set(permission) - {"action", "resources", "metadata"}
            or not isinstance(permission.get("action"), str)
            or not isinstance(permission.get("resources"), list)
            or not all(isinstance(r, str) for r in permission["resources"])
        ):
            raise ValueError("Invalid permission scope")
        # A separate resource grant cannot be approved by a shell allow rule.
        # Preserve its scope for Auto instead of treating it as just the command.
        if (
            value["tool"] != "shell" or permission["action"] != "bash"
            or permission["resources"] != [value["operation"]]
            or permission.get("metadata")
        ):
            value["input"]["permission"] = permission
    return AuthorizationRequest.normalize(value)


def user_prompt(event):
    value = codex.user_prompt(event)
    # Retry-safe ID; a changed draft under the same message ID is a conflict.
    value["id"] = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "mayi:opencode:" + str((value["session_id"], value["turn_id"])),
        )
    )
    value["agent"] = "opencode"
    return value


def translate(result):
    effect = {"approve": "allow", "deny": "deny"}.get(result.get("decision"), "ask")
    return {"effect": effect, "message": "MayI: " + effect}


def capture_success(result):
    return {"stored": True}


def capture_block():
    return {"stored": False, "message": "MayI could not persist the submitted prompt."}


def tool_outcome(event):
    value = codex.tool_outcome(event)
    value["agent"] = "opencode"
    return value
