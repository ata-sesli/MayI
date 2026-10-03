"""Claude Code command hooks; session revisions are assigned by the daemon."""

from dataclasses import asdict

from ..core.models import AuthorizationRequest
from . import codex

capture_block = codex.capture_block
capture_success = codex.capture_success
translate = codex.translate


def normalize(event):
    value = asdict(codex.normalize(event))
    value["agent"] = "claude"
    # Claude's documented permission payload has no shared prompt/turn ID.
    value["metadata"].pop("turn_id", None)
    return AuthorizationRequest.normalize(value)


def user_prompt(event):
    value = codex.user_prompt(event)
    value.update(agent="claude", turn_id=None)
    return value


def tool_outcome(event):
    value = codex.tool_outcome(event)
    value["agent"] = "claude"
    value.pop("turn_id", None)
    response = event.get("tool_response")
    # Claude documents duration on the event; do not parse textual failures.
    value["duration_ms"] = event.get("duration_ms")
    value["exit_code"] = response.get("exit_code") if isinstance(response, dict) else None
    return value
