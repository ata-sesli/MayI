"""Daemon-owned instruction ledger sourced directly from UserPromptSubmit."""

import hashlib
import json
import math
import time
import uuid

MAX_PROMPT_BYTES = 8192
MAX_CONTEXT_BYTES = 32768
MAX_INSTRUCTIONS = 64


class ContextUnavailable(ValueError):
    """A bounded, payload-free reason why context cannot authorize a request."""


def identifier(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and not any(ord(c) < 32 for c in value)
    )


def auto_user_request(context):
    """Preserve the ordered original text, without agent summaries or reasons."""
    return json.dumps(
        {
            "current_turn_id": context["turn_id"],
            "instructions": [
                {key: row[key] for key in ("turn_id", "sequence", "prompt")}
                for row in context["instructions"]
            ],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


class PromptLedger:
    def __init__(self, store, *, max_age=3600):
        if (
            type(max_age) not in (int, float)
            or not math.isfinite(max_age)
            or not 0 < max_age <= 86400
        ):
            raise ValueError("Invalid context age")
        self.store = store
        self.max_age = max_age

    def capture(self, value):
        fields = {"id", "session_id", "turn_id", "cwd", "prompt"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("Invalid prompt envelope")
        value = dict(value)
        value["id"] = str(uuid.UUID(value["id"]))
        if not all(identifier(value[k]) for k in ("session_id", "turn_id")):
            raise ValueError("Missing prompt association")
        if (
            not isinstance(value["cwd"], str)
            or not value["cwd"]
            or len(value["cwd"]) > 4096
        ):
            raise ValueError("Invalid prompt directory")
        if (
            not isinstance(value["prompt"], str)
            or not value["prompt"].strip()
            or len(value["prompt"].encode()) > MAX_PROMPT_BYTES
        ):
            raise ValueError("Invalid prompt text")
        digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        record = {
            **value,
            "digest": digest,
            "source": "codex_user_prompt_submit",
            "received_at": time.time(),
        }
        if self.store is None:
            raise OSError("Prompt persistence unavailable")
        saved = self.store.record_prompt(record)
        return {
            "stored": True,
            "prompt_id": saved["id"],
            "ordinal": saved["ordinal"],
        }

    def resolve(self, request):
        session = request.metadata.get("session_id")
        turn = request.metadata.get("turn_id")
        if not identifier(session) or not identifier(turn) or not request.cwd:
            raise ContextUnavailable("missing_association")
        if self.store is None:
            raise ContextUnavailable("missing_store")
        rows = self.store.prompts(session, limit=MAX_INSTRUCTIONS + 1)
        if not rows:
            raise ContextUnavailable("missing_context")
        if len(rows) > MAX_INSTRUCTIONS:
            raise ContextUnavailable("context_overflow")
        if rows[-1]["turn_id"] != turn:
            raise ContextUnavailable("turn_mismatch")
        age = time.time() - rows[-1]["received_at"]
        if age < 0 or age > self.max_age:
            raise ContextUnavailable("stale_context")
        previous = None
        previous_ordinal = 0
        seen_turns = set()
        last_turn = None
        instructions = []
        for sequence, row in enumerate(rows, 1):
            if row["cwd"] != request.cwd:
                raise ContextUnavailable("directory_mismatch")
            if row.get("source") != "codex_user_prompt_submit":
                raise ContextUnavailable("unknown_source")
            # Older attested records can be retained as original captures.
            # Sequence is assigned from persisted arrival order, never the caller.
            if row["ordinal"] <= previous_ordinal:
                raise ContextUnavailable("ambiguous_order")
            if row["turn_id"] != last_turn:
                if row["turn_id"] in seen_turns:
                    raise ContextUnavailable("ambiguous_order")
                seen_turns.add(row["turn_id"])
                last_turn = row["turn_id"]
            instructions.append(
                {
                    **{
                        key: row[key]
                        for key in ("id", "turn_id", "ordinal", "prompt", "source")
                    },
                    "sequence": sequence,
                }
            )
            previous = row["id"]
            previous_ordinal = row["ordinal"]
        result = {
            "session_id": session,
            "turn_id": turn,
            "instructions": instructions,
            "latest_id": previous,
            "fingerprint": hashlib.sha256(
                json.dumps([(r["id"], r["digest"]) for r in rows]).encode()
            ).hexdigest(),
        }
        if len(auto_user_request(result).encode()) > MAX_CONTEXT_BYTES:
            raise ContextUnavailable("context_overflow")
        return result
