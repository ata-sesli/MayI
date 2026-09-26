import ipaddress
import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path("~/.config/mayi/config.toml")


@dataclass(slots=True)
class Config:
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
    fields = {
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
    for key in ("http_enabled", "retain_input"):
        if type(getattr(config, key)) is not bool:
            raise ValueError(f"{key} must be boolean")
    for key in ("approval_threshold", "request_timeout"):
        value = getattr(config, key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"Invalid {key}")
    if not 0 <= config.approval_threshold <= 1 or config.request_timeout <= 0:
        raise ValueError("Invalid threshold or request timeout")
    if type(config.port) is not int or not 1 <= config.port <= 65535:
        raise ValueError("Invalid HTTP port")
    for key in ("model", "device", "host", "bearer_token"):
        value = getattr(config, key)
        if value is None and key in {"model", "bearer_token"}:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Invalid {key}")
    for key in ("unix_socket", "storage_path", "ssl_cert", "ssl_key"):
        value = getattr(config, key)
        if value is None and key in {"ssl_cert", "ssl_key"}:
            continue
        if not isinstance(value, (str, Path)) or not str(value):
            raise ValueError(f"Invalid {key}")
        setattr(config, key, Path(value).expanduser())
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
