import asyncio
import json
import signal
import socket
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path


class CliTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mayi-cli-", dir="/tmp")
        self.directory = Path(self.tmp.name)
        self.config = self.directory / "config.toml"
        self.config.write_text(
            f'[server]\nunix_socket="{self.directory}/mayi.sock"\n'
            f'[storage]\npath="{self.directory}/audit.zova"\n'
            f'[policy]\nfile="{Path(__file__).parent / "fixtures/configured_policy.toml"}"\n'
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def command(self, *args, input=b""):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mayi",
            "--config",
            str(self.config),
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(input), 10)
        return process.returncode, out, err

    async def test_direct_decide_and_logs(self):
        for command, decision in (
            ("git status", "approve"),
            ("sudo x", "deny"),
            ("unknown", "hold"),
        ):
            code, out, err = await self.command("decide", "--command", command)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["decision"], decision)
        code, out, err = await self.command("logs", "--decision", "hold")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)), 1)

    async def test_hook_failure_outputs_empty_object_and_success_exit(self):
        for input in (b"{bad", b"x" * 65537, b"{}"):
            code, out, err = await self.command("hook", "codex", input=input)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out), {})
        self.config.write_text("invalid toml")
        code, out, err = await self.command("hook", "codex", input=b"{}")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {})

    async def test_prompt_capture_cli_failure_blocks_submission(self):
        for body in (b"{bad", b"x" * 65537, b"{}"):
            code, out, err = await self.command(
                "hook", "codex", "--user-prompt", input=body
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["decision"], "block")
        self.config.write_text("invalid toml")
        code, out, err = await self.command(
            "hook", "codex", "--user-prompt", input=b"{}"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["decision"], "block")

    async def test_new_agent_hook_commands_preserve_native_failures(self):
        for agent, expected in (("claude", {}), ("opencode", {"effect": "ask", "message": "MayI: ask"})):
            code, out, err = await self.command("hook", agent, input=b"{bad")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out), expected)
            code, out, err = await self.command("hook", agent, "--user-prompt", input=b"{bad")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out).get("decision", "block") if agent == "claude"
                             else json.loads(out)["stored"], "block" if agent == "claude" else False)

    async def test_daemon_status_hook_and_granian(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.config.open("a") as f:
            f.write(f'[http]\nenabled=true\nport={port}\nbearer_token="test-token"\n')
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mayi",
            "--config",
            str(self.config),
            "serve",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            for _ in range(80):
                if (self.directory / "mayi.sock").exists():
                    break
                if process.returncode is not None:
                    break
                await asyncio.sleep(0.05)
            code, out, err = await self.command("status")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["status"], "ready")
            event = {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"command": "git status"},
            }
            code, out, err = await self.command(
                "hook", "codex", input=json.dumps(event).encode()
            )
            self.assertEqual(json.loads(out), {})

            def post():
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/decide",
                    data=json.dumps(
                        {"agent": "test", "tool": "shell", "operation": "sudo x"}
                    ).encode(),
                    headers={
                        "Authorization": "Bearer test-token",
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.load(response)

            for attempt in range(40):
                try:
                    result = await asyncio.to_thread(post)
                    break
                except urllib.error.URLError:
                    if attempt == 39:
                        raise
                    await asyncio.sleep(0.05)
            self.assertEqual(result["decision"], "deny")
        finally:
            if process.returncode is None:
                process.send_signal(signal.SIGTERM)
            out, err = await asyncio.wait_for(process.communicate(), 10)
        self.assertEqual(process.returncode, 0, err)
        self.assertFalse((self.directory / "mayi.sock").exists())
