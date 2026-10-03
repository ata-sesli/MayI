import json

from ..core.models import hold
from ..user_context import CONTEXT_AGENTS, PROMPT_SOURCES

MAX_BYTES = 65536


def decode(data):
    def reject(value):
        raise ValueError("Non-finite JSON number")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    return json.loads(data, parse_constant=reject, object_pairs_hook=unique)


def encode(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":")).encode() + b"\n"


def status(evaluator):
    return {
        "status": "ready",
        "model_available": evaluator.model is not None,
        "approval_threshold": evaluator.context["threshold"],
        "policy": evaluator.policy_name,
        "context": evaluator.context,
        "telemetry": evaluator.health(),
        "user_context": {
            "required_for_codex": evaluator.require_user_context,
            "required_for_agents": (
                sorted(CONTEXT_AGENTS) if evaluator.require_user_context else []
            ),
            "max_age": evaluator.prompt_ledger.max_age,
            "source": "codex_user_prompt_submit",
            "sources": PROMPT_SOURCES,
        },
    }


async def dispatch(evaluator, data):
    try:
        value = decode(data)
    except ValueError, UnicodeError, RecursionError:
        return hold("Malformed JSON").to_dict()
    if value == {"action": "status"}:
        return status(evaluator)
    if isinstance(value, dict) and value.get("action") == "event":
        return evaluator.record_event(value.get("event"))
    if isinstance(value, dict) and value.get("action") == "user_prompt":
        return evaluator.record_prompt(value.get("prompt"))
    return (await evaluator.authorize(value)).to_dict()
