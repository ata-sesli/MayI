"""Declarative rules with conservative shell matching; no built-in decisions."""

import hashlib
import posixpath
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .decision import Decision
from .models import AuthorizationResult

SHELL_TOOLS = {"shell", "Bash", "exec_command", "shell_command"}
SHELL_SYNTAX = re.compile(r"[\n\r;&|<>`$(){}\\\x00]")
RULE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class PolicyRules:
    allow: tuple[tuple[str, str, str], ...] = ()
    deny: tuple[tuple[str, re.Pattern], ...] = ()
    fingerprint: str | None = None

    @classmethod
    def load(cls, path):
        if path is None:
            return cls()
        data = Path(path).read_bytes()
        if len(data) > 65536:
            raise ValueError("Policy file too large")
        raw = tomllib.loads(data.decode())
        if not isinstance(raw, dict) or set(raw) != {"allow", "deny"}:
            raise ValueError("Policy file requires allow and deny arrays")
        if not isinstance(raw["allow"], list) or not isinstance(raw["deny"], list):
            raise TypeError("Invalid policy rules")
        if len(raw["allow"]) + len(raw["deny"]) > 100:
            raise ValueError("Too many policy rules")
        seen = set()
        allow, deny = [], []
        for kind in ("allow", "deny"):
            for value in raw[kind]:
                fields = (
                    {"id", "tool", "command"} if kind == "allow" else {"id", "pattern"}
                )
                if not isinstance(value, dict) or set(value) != fields:
                    raise ValueError("Invalid policy rule fields")
                name = value["id"]
                if (
                    not isinstance(name, str)
                    or not RULE_ID.fullmatch(name)
                    or name in seen
                ):
                    raise ValueError("Invalid or duplicate policy rule ID")
                seen.add(name)
                if kind == "allow":
                    tool, command = value["tool"], value["command"]
                    if not isinstance(tool, str) or tool not in SHELL_TOOLS:
                        raise ValueError("Invalid allow tool")
                    if (
                        not isinstance(command, str)
                        or not command
                        or len(command) > 4096
                        or SHELL_SYNTAX.search(command)
                    ):
                        raise ValueError("Unsafe allow command")
                    allow.append((name, tool, command))
                else:
                    pattern = value["pattern"]
                    if (
                        not isinstance(pattern, str)
                        or not pattern
                        or len(pattern) > 512
                    ):
                        raise ValueError("Invalid deny pattern")
                    try:
                        deny.append((name, re.compile(pattern)))
                    except re.error as error:
                        raise ValueError("Invalid deny pattern") from error
        return cls(tuple(allow), tuple(deny), hashlib.sha256(data).hexdigest()[:16])


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def hard_deny(request, rules):
    if not rules.deny:
        return None
    texts = [request.operation or "", request.cwd or "", *_strings(request.input)]
    for raw in list(texts):
        try:
            lexer = shlex.shlex(raw, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            words = list(lexer)
        except ValueError:
            continue
        texts.append(" ".join(words))
        for word in words:
            path = word.rsplit("=", 1)[-1]
            if "/" in path:
                texts.append(posixpath.normpath(path))
    text = "\n".join(texts)
    for name, pattern in rules.deny:
        if pattern.search(text):
            return AuthorizationResult(Decision.DENY, "static_deny", reason=name)
    return None


def known_safe(request, rules):
    command = request.operation
    if request.tool not in SHELL_TOOLS or not command or SHELL_SYNTAX.search(command):
        return None
    if set(request.input) - {"command", "cmd", "description"}:
        return None
    if any(
        request.input[key] != command
        for key in ("command", "cmd")
        if key in request.input
    ):
        return None
    try:
        shlex.split(command)
    except ValueError:
        return None
    for name, tool, candidate in rules.allow:
        if request.tool == tool and command == candidate:
            return AuthorizationResult(
                Decision.APPROVE,
                "static_allow",
                reason="Configured policy rule",
                matched_rule=name,
            )
    return None
