"""Install agent configuration without starting a daemon or granting trust."""

import asyncio
import json
import math
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from .config import DEFAULT_PATH
from .server.protocol import decode


def executable_command():
    # Preserve virtual-environment paths; resolving a Python symlink loses it.
    script = Path(sys.argv[0]).absolute()
    if script.name == "mayi" and script.is_file():
        return [str(script)]
    return [str(Path(sys.executable).absolute()), "-m", "mayi"]


def agent_target(agent):
    if agent == "codex":
        return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "hooks.json"
    if agent == "claude":
        return Path("~/.claude/settings.json").expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "opencode"
    candidates = [base / "opencode.jsonc", base / "opencode.json"]
    present = [path for path in candidates if path.exists()]
    if len(present) > 1:
        raise ValueError("Both OpenCode configuration files exist; select --target")
    return present[0] if present else candidates[0]


def jsonc_text(text):
    # Keep offsets stable so edits leave unrelated comments/formatting intact.
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/')
    def strip(match):
        token = match.group()
        return token if token.startswith('"') else re.sub(r"[^\r\n]", " ", token)
    comments_removed = tokens.sub(strip, text)
    cleaned = re.sub(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])',
                     lambda match: " " if match.group() == "," else match.group(),
                     comments_removed)
    return comments_removed, cleaned


def value_spans(cleaned, start, end, *, object_values=False):
    decoder = json.JSONDecoder()
    position = start + 1
    values = []
    while position < end:
        while position < end and (cleaned[position].isspace() or cleaned[position] == ","):
            position += 1
        if position >= end:
            break
        key = None
        if object_values:
            key, position = decoder.raw_decode(cleaned, position)
            while cleaned[position].isspace():
                position += 1
            if cleaned[position] != ":":
                raise ValueError("Invalid configuration property")
            position += 1
            while cleaned[position].isspace():
                position += 1
        value_start = position
        value, position = decoder.raw_decode(cleaned, position)
        values.append((key, value_start, position, value))
    return values


def owns_hook(handler, agent):
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    try:
        args = shlex.split(handler.get("command", ""))
    except ValueError:
        return False
    if not args:
        return False
    executable = Path(args[0]).name
    if executable == "mayi":
        args = args[1:]
    elif executable.startswith("python") and args[1:3] == ["-m", "mayi"]:
        args = args[3:]
    else:
        return False
    if args[:1] == ["--config"]:
        args = args[2:]
    return args in (["hook", agent], ["hook", agent, "--user-prompt"])


def hook_settings(value, agent, executable, config_path, timeout):
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Expected a hooks object")
    for event, capture in (("UserPromptSubmit", True), ("PermissionRequest", False)):
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError("Expected a hook event array")
        config_args = ["--config", str(config_path)] if config_path else []
        command = shlex.join([*executable, *config_args, "hook", agent,
                              *(["--user-prompt"] if capture else [])])
        desired = {"type": "command", "command": command, "timeout": timeout}
        matches = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("Invalid hook handler group")
            for index, handler in enumerate(group["hooks"]):
                if owns_hook(handler, agent):
                    # Scoped or conditional handlers are not ours to broaden.
                    if set(group) - {"hooks"}:
                        raise ValueError("Existing scoped MayI hook; adjust it manually")
                    matches.append((group["hooks"], index))
        if matches:
            first, index = matches[0]
            first[index] = desired
            for handlers, index in reversed(matches[1:]):
                del handlers[index]
        else:
            groups.append({"hooks": [desired]})
    return value


def opencode_settings(text, value, executable, config_path, timeout, plugin, target):
    comments, cleaned = jsonc_text(text)
    root_start, root_end = cleaned.index("{"), cleaned.rindex("}")
    fields = value_spans(cleaned, root_start, root_end, object_values=True)
    desired = {"package": str(plugin), "options": {
        "mayiExecutable": executable[0],
        "timeoutMs": timeout * 1000,
    }}
    if config_path:
        desired["options"]["mayiConfig"] = str(config_path)
    if len(executable) != 1:
        raise ValueError("OpenCode setup requires the installed mayi executable")
    encoded = json.dumps(desired, ensure_ascii=False)
    field = next((item for item in fields if item[0] == "plugins"), None)
    if field is None:
        has_comma = bool(fields and "," in comments[fields[-1][2]:root_end])
        prefix = "," if fields and not has_comma else ""
        return text[:root_end] + prefix + '\n  "plugins": [' + encoded + "]\n" + text[root_end:]
    _, start, end, plugins = field
    if not isinstance(plugins, list):
        raise ValueError("Expected a plugins array")
    entries = value_spans(cleaned, start, end - 1)
    matches = []
    for entry in entries:
        item = entry[3]
        package = item.get("package") if isinstance(item, dict) else item
        if isinstance(package, str) and package.startswith(("/", ".", "file://", "~")):
            path = Path(package.removeprefix("file://")).expanduser()
            if not path.is_absolute():
                path = target.parent / path
            if path.resolve() == plugin.resolve():
                matches.append(entry)
    if len(matches) > 1:
        raise ValueError("Duplicate MayI plugins; remove duplicate entries first")
    if matches:
        _, start, end, item = matches[0]
        if item == desired:
            return text
        return text[:start] + encoded + text[end:]
    closing = end - 1
    has_comma = bool(entries and "," in comments[entries[-1][2]:closing])
    prefix = "," if entries and not has_comma else ""
    return text[:closing] + prefix + "\n    " + encoded + "\n  " + text[closing:]


def configure_agent(agent, target, executable, config_path, timeout, *, plugin=None):
    target = Path(target).expanduser().absolute()
    if target.is_symlink():
        raise ValueError("Agent configuration is a symlink; select its real --target")
    original = target.read_bytes().decode("utf-8") if target.exists() else None
    text = original if original is not None else "{}\n"
    value = decode(jsonc_text(text)[1] if agent == "opencode" else text)
    if not isinstance(value, dict):
        raise ValueError("Expected an agent configuration object")
    if agent == "opencode":
        updated = opencode_settings(text, value, executable, config_path, timeout, plugin, target)
        decode(jsonc_text(updated)[1])
    else:
        updated_value = hook_settings(value, agent, executable, config_path, timeout)
        updated = text if updated_value == decode(text) else json.dumps(updated_value, indent=2) + "\n"
    result = {"target": str(target), "changed": updated != original, "backup": None}
    if updated == original:
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    if original is not None:
        with tempfile.NamedTemporaryFile(prefix=target.name + ".backup-", dir=target.parent,
                                         delete=False, mode="wb") as backup:
            backup.write(original.encode("utf-8"))
            result["backup"] = backup.name
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False, mode="w") as output:
            temporary = Path(output.name)
            output.write(updated)
        if target.is_symlink() or (target.read_bytes().decode("utf-8") if target.exists() else None) != original:
            raise ValueError("Agent configuration changed during setup")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return result


def plugin_directory():
    path = Path(__file__).resolve().parents[2] / "plugins" / "opencode"
    if not (path / "package.json").is_file():
        raise ValueError("OpenCode setup requires a MayI source checkout")
    return path


async def install_plugin(plugin):
    bun = shutil.which("bun")
    if bun is None:
        raise ValueError("Install Bun before setting up OpenCode V2")
    print("Installing OpenCode V2 plugin dependencies...", file=sys.stderr)
    process = await asyncio.create_subprocess_exec(
        bun, "install", "--cwd", str(plugin), "--frozen-lockfile",
        stdout=sys.stderr,
    )
    try:
        async with asyncio.timeout(180):
            if await process.wait() != 0:
                raise RuntimeError("OpenCode plugin dependency installation failed")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def check_daemon(config):
    from .hook_client import query_endpoints
    from .server.unix import query
    try:
        if config.hook_endpoints:
            value = await query_endpoints(config.hook_endpoints, {"action": "status"},
                timeout=config.hook_timeout, connect_timeout=config.hook_connect_timeout,
                state_file=config.hook_state_file)
        else:
            value = await query(config.unix_socket, {"action": "status"},
                                timeout=config.request_timeout + 2)
        if value.get("status") != "ready" or type(value.get("model_available")) is not bool:
            return {"daemon": "unavailable"}
        return {"daemon": "ready", "model_available": value["model_available"]}
    except OSError, ValueError, TimeoutError:
        return {"daemon": "unavailable"}


async def setup_agent(agent, config, *, config_path=None, target=None):
    target = Path(target).expanduser().absolute() if target else agent_target(agent)
    config_path = (Path(config_path).expanduser().absolute() if config_path
                   else DEFAULT_PATH.expanduser().absolute() if DEFAULT_PATH.expanduser().exists()
                   else None)
    executable = executable_command()
    timeout = math.ceil(config.hook_timeout if config.hook_endpoints else config.request_timeout + 2) + 3
    plugin = None
    if agent == "opencode":
        if len(executable) != 1:
            installed = Path(sys.executable).parent / "mayi"
            if not installed.is_file():
                raise ValueError("OpenCode setup requires the installed mayi executable")
            executable = [str(installed.absolute())]
        plugin = plugin_directory()
        await install_plugin(plugin)
    result = configure_agent(agent, target, executable, config_path, timeout, plugin=plugin)
    print("Checking MayI daemon connectivity...", file=sys.stderr)
    result.update(await check_daemon(config))
    result["agent"] = agent
    result["next"] = (
        "Review/trust the hooks in Codex /hooks."
        if agent == "codex" else "Restart the agent to load the MayI configuration."
    )
    return result
