import json

from ..core.models import hold

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
        "julia_available": evaluator.model is not None,
        "approval_threshold": evaluator.threshold,
    }


async def dispatch(evaluator, data):
    try:
        value = decode(data)
    except ValueError, UnicodeError, RecursionError:
        return hold("Malformed JSON").to_dict()
    if value == {"action": "status"}:
        return status(evaluator)
    return (await evaluator.authorize(value)).to_dict()
