"""Small, payload-free telemetry shared by the daemon and hook client."""

import fcntl
import hashlib
import json
import math
import os
import stat
import time
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def context(
    policy, threshold, timeout, model_id=None, device=None, rules_fingerprint=None
):
    from .core import policy as rules

    values = {
        "policy": policy,
        "policy_version": hashlib.sha256(Path(rules.__file__).read_bytes()).hexdigest()[
            :16
        ],
        "threshold": threshold,
        "request_timeout": timeout,
        "model_id": fingerprint(model_id) if model_id else None,
        "device": device,
        "rules_fingerprint": rules_fingerprint,
        "mayi_version": version("mayi"),
    }
    return {**values, "config_version": fingerprint(values)}


def milliseconds(start):
    return (time.monotonic() - start) * 1000


def normalize_event(value):
    if not isinstance(value, dict):
        raise TypeError("Invalid event")
    common = {"kind", "id", "request_id", "session_id", "turn_id", "tool_use_id"}
    fields = {
        "tool_outcome": {"tool", "exit_code", "duration_ms"},
        "human_feedback": {"human_decision"},
    }
    kind = value.get("kind")
    if (
        not isinstance(kind, str)
        or kind not in fields
        or set(value) - common - fields[kind]
    ):
        raise ValueError("Unsupported event fields")
    result = dict(value)
    for key in common - {"kind"} | {"tool"}:
        if key in result:
            item = result[key]
            if (
                not isinstance(item, str)
                or not item
                or len(item) > 256
                or any(ord(c) < 32 for c in item)
            ):
                raise ValueError("Invalid event identifier")
    for key in ("id", "request_id"):
        if key in result:
            result[key] = str(uuid.UUID(result[key]))
    if kind == "human_feedback":
        if not result.get("request_id") or result.get("human_decision") not in (
            "approve",
            "deny",
        ):
            raise ValueError("Feedback requires request ID and explicit decision")
        result["source"] = "explicit_feedback"
    else:
        if (
            not result.get("tool")
            or not result.get("session_id")
            or not result.get("tool_use_id")
        ):
            raise ValueError("Outcome requires tool and invocation identifiers")
        if (
            "exit_code" in result
            and result["exit_code"] is not None
            and type(result["exit_code"]) is not int
        ):
            raise ValueError("Invalid exit code")
        duration = result.get("duration_ms")
        if duration is not None and (
            type(duration) not in (int, float)
            or not math.isfinite(duration)
            or duration < 0
        ):
            raise ValueError("Invalid duration")
        result["source"] = "codex_post_tool_use"
    result.setdefault("id", str(uuid.uuid4()))
    result["timestamp"] = datetime.now(UTC).isoformat()
    return result


def append_routing(path, record):
    """Best-effort private log, bounded to 1 MiB; never changes authorization."""
    if path is None:
        return
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            return
        fd = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        with os.fdopen(fd, "r+b") as output:
            info = os.fstat(output.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                return
            # Do not wait for another hook's logger inside an approval deadline.
            fcntl.flock(output, fcntl.LOCK_EX | fcntl.LOCK_NB)
            data = (
                json.dumps(
                    {**record, "timestamp": datetime.now(UTC).isoformat()},
                    allow_nan=False,
                )
                + "\n"
            ).encode()
            if len(data) > 1048576:
                return
            output.seek(0, os.SEEK_END)
            if output.tell() + len(data) > 1048576:
                output.seek(0)
                output.truncate()
            output.write(data)
    except OSError, ValueError, TypeError:
        pass
