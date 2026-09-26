import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path

from mayi.adapters.codex import normalize, run_hook, translate
from mayi.core.evaluator import Evaluator
from mayi.server.http import Application
from mayi.server.unix import UnixServer, query


class UnixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mayi-", dir="/tmp")
        self.path = Path(self.tmp.name) / "mayi.sock"
        self.server = UnixServer(
            Evaluator(), self.path, idle_timeout=0.2, max_bytes=1024
        )
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.close()
        self.tmp.cleanup()

    async def test_clients_and_permissions(self):
        result = await query(
            self.path, {"agent": "test", "tool": "shell", "operation": "git status"}
        )
        self.assertEqual(result["decision"], "approve")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        responses = await asyncio.gather(
            *[query(self.path, {"action": "status"}) for _ in range(4)]
        )
        self.assertTrue(all(r["status"] == "ready" for r in responses))

    async def test_bad_json_oversize_and_idle(self):
        for payload in (b"{bad}\n", b"[]\n", b"x" * 1100 + b"\n"):
            reader, writer = await asyncio.open_unix_connection(self.path)
            writer.write(payload)
            await writer.drain()
            result = json.loads(await reader.readline())
            self.assertEqual(result["decision"], "hold")
            writer.close()
            await writer.wait_closed()
        reader, writer = await asyncio.open_unix_connection(self.path)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        writer.close()
        await writer.wait_closed()

    async def test_refuses_live_socket_and_regular_file(self):
        with self.assertRaises(OSError):
            await UnixServer(Evaluator(), self.path).start()
        self.assertTrue(self.path.exists())
        await self.server.close()
        self.path.write_text("keep")
        with self.assertRaises(OSError):
            await UnixServer(Evaluator(), self.path).start()
        self.assertEqual(self.path.read_text(), "keep")

    async def test_stale_socket_removed(self):
        await self.server.close()
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(self.path))
        await self.server.start()
        self.assertEqual(
            (await query(self.path, {"action": "status"}))["status"], "ready"
        )

    async def test_concurrent_start_cannot_replace_another_listener(self):
        await self.server.close()
        other = UnixServer(Evaluator(), self.path)
        try:
            results = await asyncio.gather(
                self.server.start(), other.start(), return_exceptions=True
            )
            self.assertEqual(sum(result is None for result in results), 1)
            self.assertEqual(
                (await query(self.path, {"action": "status"}))["status"], "ready"
            )
        finally:
            await other.close()

    async def test_hook_end_to_end_and_failure_fallthrough(self):
        for command, expected in (
            ("git status", "allow"),
            ("sudo x", "deny"),
            ("unknown", None),
        ):
            event = {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "cwd": "/work",
                "tool_input": {"command": command},
            }
            response = await run_hook(event, self.path)
            if expected:
                self.assertEqual(
                    response["hookSpecificOutput"]["decision"]["behavior"], expected
                )
            else:
                self.assertEqual(response, {})
        await self.server.close()
        self.assertEqual(await run_hook(event, self.path), {})


async def http_call(
    app, method="POST", path="/v1/decide", data=None, headers=(), raw=None
):
    messages = []

    async def receive():
        return {
            "type": "http.request",
            "body": raw if raw is not None else json.dumps(data).encode(),
            "more_body": False,
        }

    async def send(message):
        messages.append(message)

    await app(
        {"type": "http", "method": method, "path": path, "headers": headers},
        receive,
        send,
    )
    return messages[0]["status"], json.loads(messages[1]["body"])


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_policy_and_routes(self):
        app = Application(Evaluator())
        for command, expected in (
            ("git status", "approve"),
            ("sudo x", "deny"),
            ("unknown", "hold"),
        ):
            status, response = await http_call(
                app, data={"agent": "test", "tool": "shell", "operation": command}
            )
            self.assertEqual(status, 200)
            self.assertEqual(response["decision"], expected)
        for path in ("/v1/health", "/v1/status"):
            self.assertEqual((await http_call(app, "GET", path))[0], 200)
        self.assertEqual((await http_call(app, "GET", "/missing"))[0], 404)
        self.assertEqual((await http_call(app, "GET"))[0], 405)

    async def test_authentication_and_bad_body(self):
        app = Application(Evaluator(), token="secret", max_bytes=100)
        for headers in ((), ((b"authorization", b"Bearer bad"),)):
            self.assertEqual((await http_call(app, headers=headers))[0], 401)
        headers = ((b"authorization", b"Bearer secret"),)
        for raw, code in (
            (b"{bad", 400),
            (b"x" * 101, 413),
            (b'{"operation":"sudo x","operation":"git status"}', 400),
        ):
            status, response = await http_call(app, raw=raw, headers=headers)
            self.assertEqual(status, code)
            self.assertEqual(response["decision"], "hold")


class CodexTests(unittest.TestCase):
    def test_normalization_preserves_tool_input_and_context(self):
        event = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "git status", "description": "inspect"},
            "session_id": "session",
            "cwd": "/work",
        }
        req = normalize(event)
        self.assertEqual(req.operation, "git status")
        self.assertEqual(req.reason, "inspect")
        self.assertEqual(req.input, event["tool_input"])
        self.assertEqual(req.metadata["session_id"], "session")

    def test_malformed_hook_does_not_approve(self):
        for value in ({}, {"hook_event_name": "Other"}, []):
            with self.assertRaises(ValueError):
                normalize(value)
        self.assertEqual(translate({"decision": "bogus"}), {})
        self.assertEqual(translate({"decision": "hold"}), {})
