import hashlib
import ipaddress
import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PATH = Path("~/.config/mayi/config.toml")


@dataclass(frozen=True, slots=True)
class HookEndpoint:
    unix_socket: Path | None = None
    url: str | None = None
    token_file: Path | None = None
    timeout: float = 5.0


def hook_endpoint(value):
    if not isinstance(value, dict) or set(value) - {
        "unix_socket",
        "url",
        "token_file",
        "timeout",
    }:
        raise ValueError("Invalid hook endpoint")
    if ("unix_socket" in value) == ("url" in value):
        raise ValueError("Hook endpoint requires exactly one transport")
    result = dict(value)
    for key in ("unix_socket", "url", "token_file"):
        if key in result:
            if not isinstance(result[key], str) or not result[key].strip():
                raise ValueError("Invalid hook endpoint value")
            if key != "url":
                result[key] = Path(result[key]).expanduser()
    timeout = result.get("timeout", 5.0)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Invalid hook endpoint timeout")
    if "unix_socket" in result and "token_file" in result:
        raise ValueError("Unix endpoints do not use tokens")
    if "url" in result:
        url = result["url"]
        parsed = urlsplit(url)
        if (
            any(ord(c) <= 32 or ord(c) == 127 for c in url)
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid hook endpoint URL")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid hook endpoint port")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("Remote hook endpoints require HTTPS")
    return HookEndpoint(**result)


@dataclass(slots=True)
class Config:
    policy_mode: str = "strict"
    policy_file: Path | None = None
    policy_rules: object = None
    model: str | None = None
    device: str = "cpu"
    approval_threshold: float = 0.98
    unix_socket: Path = Path("~/.mayi/mayi.sock")
    request_timeout: float = 10.0
    http_enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 7411
    bearer_token: str | None = None
    ssl_cert: Path | None = None
    ssl_key: Path | None = None
    storage_path: Path = Path("~/.local/share/mayi/mayi.zova")
    retain_input: bool = False
    hook_endpoints: tuple[HookEndpoint, ...] = ()
    hook_timeout: float = 12.0
    hook_connect_timeout: float = 1.0
    hook_telemetry_file: Path | None = None
    hook_state_file: Path = Path("~/.local/state/mayi/hook.json")


def load_config(path=None):
    explicit = path is not None
    path = Path(path or DEFAULT_PATH).expanduser()
    try:
        with path.open("rb") as source:
            raw = tomllib.load(source)
    except FileNotFoundError:
        if explicit:
            raise
        raw = {}
    config = Config()
    config.hook_state_file = Path("~/.local/state/mayi") / (
        "hook-"
        + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
        + ".json"
    )
    fields = {
        "policy": {"mode": "policy_mode", "file": "policy_file"},
        "julia": {
            "model": "model",
            "device": "device",
            "approval_threshold": "approval_threshold",
        },
        "server": {"unix_socket": "unix_socket", "request_timeout": "request_timeout"},
        "http": {
            "enabled": "http_enabled",
            "host": "host",
            "port": "port",
            "bearer_token": "bearer_token",
            "ssl_cert": "ssl_cert",
            "ssl_key": "ssl_key",
        },
        "storage": {"path": "storage_path", "retain_input": "retain_input"},
        "hook": {
            "endpoints": "hook_endpoints",
            "timeout": "hook_timeout",
            "connect_timeout": "hook_connect_timeout",
            "state_file": "hook_state_file",
            "telemetry_file": "hook_telemetry_file",
        },
    }
    for section, values in raw.items():
        if section not in fields or not isinstance(values, dict):
            raise ValueError("Unknown configuration section")
        for key, value in values.items():
            if key not in fields[section]:
                raise ValueError(f"Unknown configuration key: {section}.{key}")
            setattr(config, fields[section][key], value)
    if "MAYI_BEARER_TOKEN" in os.environ:
        config.bearer_token = os.environ["MAYI_BEARER_TOKEN"]
    if config.policy_mode not in ("strict", "approve-or-hold"):
        raise ValueError("Unknown authorization policy")
    for key in ("http_enabled", "retain_input"):
        if type(getattr(config, key)) is not bool:
            raise ValueError(f"{key} must be boolean")
    for key in (
        "approval_threshold",
        "request_timeout",
        "hook_timeout",
        "hook_connect_timeout",
    ):
        value = getattr(config, key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"Invalid {key}")
    if not 0 <= config.approval_threshold <= 1 or config.request_timeout <= 0:
        raise ValueError("Invalid threshold or request timeout")
    if config.hook_timeout <= 0 or config.hook_connect_timeout <= 0:
        raise ValueError("Invalid hook timeout")
    if "hook" in raw and "endpoints" in raw["hook"]:
        if not isinstance(config.hook_endpoints, list) or not config.hook_endpoints:
            raise ValueError("Hook endpoints must be a nonempty array")
        config.hook_endpoints = tuple(
            hook_endpoint(value) for value in config.hook_endpoints
        )
    if type(config.port) is not int or not 1 <= config.port <= 65535:
        raise ValueError("Invalid HTTP port")
    for key in ("model", "device", "host", "bearer_token"):
        value = getattr(config, key)
        if value is None and key in {"model", "bearer_token"}:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Invalid {key}")
    for key in (
        "unix_socket",
        "storage_path",
        "ssl_cert",
        "ssl_key",
        "hook_state_file",
        "hook_telemetry_file",
        "policy_file",
    ):
        value = getattr(config, key)
        if value is None and key in {
            "ssl_cert",
            "ssl_key",
            "hook_telemetry_file",
            "policy_file",
        }:
            continue
        if not isinstance(value, (str, Path)) or not str(value):
            raise ValueError(f"Invalid {key}")
        setattr(config, key, Path(value).expanduser())
    if config.policy_file is not None and not config.policy_file.is_absolute():
        config.policy_file = path.parent / config.policy_file
    from .core.policy import PolicyRules

    config.policy_rules = PolicyRules.load(config.policy_file)
    if bool(config.ssl_cert) != bool(config.ssl_key):
        raise ValueError("Both ssl_cert and ssl_key are required")
    try:
        loopback = ipaddress.ip_address(config.host).is_loopback
    except ValueError:
        # Hostname resolution may change; only a literal loopback is trusted.
        loopback = False
    if config.http_enabled and not loopback and not config.bearer_token:
        raise ValueError("Non-loopback HTTP requires a bearer token")
    return config
