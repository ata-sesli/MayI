"""Small, conservative patterns; deliberately not a shell interpreter."""

import posixpath
import re
import shlex

from .decision import Decision
from .models import AuthorizationResult

SHELL_TOOLS = {"shell", "Bash", "exec_command", "shell_command"}
SHELL_SYNTAX = re.compile(r"[\n\r;&|<>`$(){}\\\x00]")
DENY_PATTERNS = (
    (r"(?:^|[\s;/])sudo(?:\s|$)", "Privilege escalation"),
    (r"(?:^|\s)rm\s+(?:-[\w-]+\s+)*[/]+(?:\s|$|\*)", "Root deletion"),
    (
        r"\bgit\s+push\b[^\n;|&]*(?:\s-[A-Za-z]*f[A-Za-z]*(?:\s|$)|--force\b)",
        "Force push",
    ),
    (
        r"\b(?:curl|wget)\b[^\n]*\|\s*(?:/\w+/)*(?:ba|da|z|k)?sh\b",
        "Remote script execution",
    ),
    (r"(?:^|[/\s\"'])\.ssh(?:[/\s\"']|$)", "SSH credential access"),
    (r"(?:^|[\s\"'=<>])/etc(?:[/\s\"']|$)", "System configuration access"),
)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def hard_deny(request):
    # Inspect raw input as well as the normalized operation so hidden fields
    # cannot bypass a deny. /etc reads are conservatively denied in v0 too.
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
    for pattern, reason in DENY_PATTERNS:
        if re.search(pattern, text):
            return AuthorizationResult(Decision.DENY, "static_deny", reason=reason)
    return None


def known_safe(request):
    command = request.operation
    if request.tool not in SHELL_TOOLS or not command or SHELL_SYNTAX.search(command):
        return None
    # Additional tool options (environment, executable, etc.) need judgment.
    if set(request.input) - {"command", "cmd", "description"}:
        return None
    if any(
        request.input[key] != command
        for key in ("command", "cmd")
        if key in request.input
    ):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    safe = False
    if words[:2] in (["cargo", "test"], ["cargo", "check"]):
        safe = all(
            word
            in {
                "--workspace",
                "--all-targets",
                "--locked",
                "--offline",
                "--quiet",
                "-q",
            }
            for word in words[2:]
        )
    elif words[:2] in (["git", "status"], ["git", "diff"], ["git", "log"]):
        safe = all(
            word
            in {
                "--short",
                "--porcelain",
                "--stat",
                "--name-only",
                "--oneline",
                "--no-pager",
            }
            for word in words[2:]
        )
    elif words == ["pytest"] or words in (["ruff", "check"], ["ruff", "check", "."]):
        safe = True
    if safe:
        return AuthorizationResult(
            Decision.APPROVE, "static_allow", reason="Known development command"
        )
    return None
