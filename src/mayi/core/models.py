import json
from dataclasses import asdict, dataclass, field

from .decision import Decision


@dataclass(slots=True)
class AuthorizationRequest:
    agent: str
    tool: str
    operation: str | None = None
    cwd: str | None = None
    reason: str | None = None
    input: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)

    # Set only by the daemon after normalization and ledger verification.
    user_context: dict | None = None

    @classmethod
    def normalize(cls, value):
        if isinstance(value, cls):
            value = asdict(value)
        if not isinstance(value, dict):
            raise TypeError("Expected an authorization object")
        if set(value) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown request fields")
        if value.get("user_context") is not None:
            raise ValueError("User context must be resolved by the daemon")
        for name in ("agent", "tool"):
            if not isinstance(value.get(name), str) or not value[name].strip():
                raise ValueError(f"Missing {name}")
        for name in ("operation", "cwd", "reason"):
            if value.get(name) is not None and not isinstance(value[name], str):
                raise ValueError(f"Invalid {name}")
        for name in ("input", "metadata"):
            if name in value and not isinstance(value[name], dict):
                raise ValueError(f"Invalid {name}")
        # Reject non-JSON values and non-finite numbers, including nested data.
        encoded = json.dumps(value, allow_nan=False)
        if len(encoded.encode()) > 65536:
            raise ValueError("Request too large")
        return cls(**json.loads(encoded))


@dataclass(slots=True)
class AuthorizationResult:
    decision: Decision
    source: str
    confidence: float | None = None
    reason: str | None = None
    policy: str = "approve-or-hold"
    matched_rule: str | None = None

    request_id: str | None = None
    context: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def hold(reason="Human review required"):
    return AuthorizationResult(Decision.HOLD, "fallback", reason=reason)
