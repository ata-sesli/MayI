import asyncio
import json
import shutil
import ssl
import sys
import tempfile
import unittest
from pathlib import Path

from test_core import RULES

from mayi.adapters.codex import run_hook
from mayi.config import load_config
from mayi.core.evaluator import Evaluator
from mayi.server.unix import UnixServer

EVENT = {
    "hook_event_name": "PermissionRequest",
    "tool_name": "Bash",
    "tool_input": {"command": "git status"},
    "cwd": "/work",
}


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mayi-fallback-", dir="/tmp")
        self.root = Path(self.tmp.name)
        self.calls = []
        self.servers = []
        self.token = self.root / "token"
        self.token.write_text("test-secret\n")
        self.token.chmod(0o600)

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        self.tmp.cleanup()

    async def endpoint(
        self, decision="approve", status=200, raw=None, delay=0, context=None
    ):
        async def handle(reader, writer):
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                size = next(
                    int(line.split(b":")[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                body = await reader.readexactly(size)
                self.calls.append((status, headers, json.loads(body)))
                await asyncio.sleep(delay)
                payload = (
                    raw
                    if raw is not None
                    else json.dumps({"decision": decision}).encode()
                )
                writer.write(
                    f"HTTP/1.1 {status} Test\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                    + payload
                )
                await writer.drain()
            except ConnectionError, asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
        self.servers.append(server)
        scheme = "https" if context else "http"
        return f"{scheme}://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/decide"

    def config(self, endpoints, timeout=2):
        path = self.root / "config.toml"
        text = f"[hook]\ntimeout={timeout}\nconnect_timeout=0.2\nstate_file={json.dumps(str(self.root / 'state.json'))}\n"
        for endpoint in endpoints:
            text += "\n[[hook.endpoints]]\n"
            for key, value in endpoint.items():
                text += f"{key}={json.dumps(str(value) if isinstance(value, Path) else value)}\n"
        path.write_text(text)
        return load_config(path)

    async def run_config(self, config):
        return await run_hook(
            EVENT,
            config.unix_socket,
            endpoints=config.hook_endpoints,
            timeout=config.hook_timeout,
            connect_timeout=config.hook_connect_timeout,
        )

    async def test_missing_unix_falls_back_to_authenticated_http(self):
        url = await self.endpoint()
        config = self.config(
            [
                {"unix_socket": str(self.root / "missing.sock")},
                {"url": url, "token_file": self.token},
            ]
        )
        result = await self.run_config(config)
        self.assertEqual(result["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertEqual(len(self.calls), 1)
        self.assertIn(b"Authorization: Bearer test-secret", self.calls[0][1])
        self.assertEqual(self.calls[0][2]["operation"], "git status")

    async def test_every_valid_decision_stops_before_next_endpoint(self):
        for decision, behavior in [
            ("approve", "allow"),
            ("deny", "deny"),
            ("hold", None),
        ]:
            with self.subTest(decision=decision):
                self.calls.clear()
                first = await self.endpoint(decision)
                second = await self.endpoint()
                result = await self.run_config(
                    self.config([{"url": first}, {"url": second}])
                )
                self.assertEqual(
                    result.get("hookSpecificOutput", {})
                    .get("decision", {})
                    .get("behavior"),
                    behavior,
                )
                self.assertEqual(len(self.calls), 1)

    async def test_live_unix_decision_does_not_contact_http(self):
        path = self.root / "live.sock"
        server = UnixServer(Evaluator(policy_rules=RULES), path)
        await server.start()
        try:
            url = await self.endpoint()
            config = self.config([{"unix_socket": str(path)}, {"url": url}])
            for command, expected in (
                ("git status", "allow"),
                ("sudo x", "deny"),
                ("unknown", None),
            ):
                event = {**EVENT, "tool_input": {"command": command}}
                result = await run_hook(event, path, endpoints=config.hook_endpoints)
                self.assertEqual(
                    result.get("hookSpecificOutput", {})
                    .get("decision", {})
                    .get("behavior"),
                    expected,
                )
            self.assertEqual(self.calls, [])
        finally:
            await server.close()

    async def test_cli_uses_endpoint_config_from_another_directory(self):
        url = await self.endpoint()
        self.config([{"unix_socket": str(self.root / "missing")}, {"url": url}])
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mayi",
            "--config",
            str(self.root / "config.toml"),
            "hook",
            "codex",
            cwd=self.root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            process.communicate(json.dumps(EVENT).encode()), 5
        )
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(
            json.loads(out)["hookSpecificOutput"]["decision"]["behavior"], "allow"
        )

    async def test_refused_http_connection_tries_next(self):
        first = await self.endpoint()
        self.servers[-1].close()
        await self.servers[-1].wait_closed()
        second = await self.endpoint()
        result = await self.run_config(self.config([{"url": first}, {"url": second}]))
        self.assertEqual(result["hookSpecificOutput"]["decision"]["behavior"], "allow")

    @unittest.skipUnless(
        shutil.which("openssl"), "openssl needed for TLS certificate test"
    )
    async def test_untrusted_tls_certificate_stops_before_next_endpoint(self):
        cert, key = self.root / "cert.pem", self.root / "key.pem"
        process = await asyncio.create_subprocess_exec(
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.assertEqual(await process.wait(), 0)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        first = await self.endpoint(context=context)
        second = await self.endpoint()
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        # The server also sees the expected client rejection of its certificate.
        loop.set_exception_handler(lambda loop, context: None)
        try:
            self.assertEqual(
                await self.run_config(self.config([{"url": first}, {"url": second}])),
                {},
            )
            self.assertEqual(self.calls, [])
        finally:
            loop.set_exception_handler(previous)

    async def test_404_and_timeout_try_next(self):
        for options in (
            {"status": 404},
            {"status": 502},
            {"status": 503},
            {"status": 504},
            {"delay": 0.5},
        ):
            with self.subTest(options=options):
                first = await self.endpoint(**options)
                second = await self.endpoint()
                result = await self.run_config(
                    self.config([{"url": first, "timeout": 0.2}, {"url": second}])
                )
                self.assertEqual(
                    result["hookSpecificOutput"]["decision"]["behavior"], "allow"
                )

    async def test_auth_redirect_server_error_and_invalid_reply_stop(self):
        for options in (
            {"status": 401},
            {"status": 403},
            {"status": 302},
            {"status": 500},
            {"raw": b"{bad"},
            {"decision": "bogus"},
            {"raw": b"[]"},
            {"raw": b'{"decision":"hold","decision":"approve"}'},
            {"raw": b"x" * 65537},
        ):
            with self.subTest(options=str(options)[:80]):
                self.calls.clear()
                first = await self.endpoint(**options)
                second = await self.endpoint()
                self.assertEqual(
                    await self.run_config(
                        self.config([{"url": first}, {"url": second}])
                    ),
                    {},
                )
                self.assertEqual(len(self.calls), 1)

    async def test_all_missing_and_total_deadline_fall_through(self):
        self.assertEqual(
            await self.run_config(
                self.config([{"unix_socket": str(self.root / "missing")}])
            ),
            {},
        )
        first = await self.endpoint(delay=0.5)
        second = await self.endpoint()
        config = self.config(
            [{"url": first, "timeout": 2}, {"url": second}], timeout=0.2
        )
        start = asyncio.get_running_loop().time()
        self.assertEqual(await self.run_config(config), {})
        self.assertLess(asyncio.get_running_loop().time() - start, 0.7)
        self.assertEqual(len(self.calls), 1)

    async def test_insecure_token_file_is_not_sent(self):
        self.token.chmod(0o644)
        first = await self.endpoint()
        second = await self.endpoint()
        self.assertEqual(
            await self.run_config(
                self.config([{"url": first, "token_file": self.token}, {"url": second}])
            ),
            {},
        )
        self.assertEqual(self.calls, [])

    async def test_rejects_unsafe_or_ambiguous_configuration(self):
        for endpoint in (
            {"url": "http://server.example.test:7411/v1/decide"},
            {"url": "https://user:secret@server.example.test/x"},
            {"url": "https://server.example.test/x#fragment"},
            {"url": "https://server.example.test/x?token=secret"},
            {"url": "https://server.example.test/x", "unix_socket": "/tmp/x"},
            {"unix_socket": "/tmp/x", "token_file": "/tmp/token"},
            {"url": "https://server.example.test/x", "timeout": 0},
            {"unknown": "x"},
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.config([endpoint])

    async def test_remembers_success_across_hook_processes_and_switches_when_unavailable(
        self,
    ):
        first = await self.endpoint(status=404)
        second = await self.endpoint(decision="hold")
        third = await self.endpoint(decision="deny")
        self.config([{"url": first}, {"url": second}, {"url": third}])
        state = self.root / "state.json"
        path = self.root / "config.toml"

        async def invoke():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "mayi",
                "--config",
                str(path),
                "hook",
                "codex",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await process.communicate(json.dumps(EVENT).encode())
            self.assertEqual(process.returncode, 0, err)
            return json.loads(out)

        self.assertEqual(await invoke(), {})
        self.assertEqual([call[0] for call in self.calls], [404, 200])
        self.calls.clear()
        self.assertEqual(await invoke(), {})
        self.assertEqual([call[0] for call in self.calls], [200])
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        # Preferred endpoint disappears; the next configured endpoint is tried.
        self.servers[1].close()
        await self.servers[1].wait_closed()
        self.calls.clear()
        result = await invoke()
        self.assertEqual(result["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertEqual([call[0] for call in self.calls], [200])
        self.calls.clear()
        await invoke()
        self.assertEqual([call[0] for call in self.calls], [200])
        # Changing the configured list invalidates the saved preference.
        self.config([{"url": first}, {"url": third}])
        self.calls.clear()
        await invoke()
        self.assertEqual([call[0] for call in self.calls], [404, 200])
        state.write_text("corrupt state")
        self.calls.clear()
        await invoke()
        self.assertEqual([call[0] for call in self.calls], [404, 200])

    async def test_outcomes_send_once_and_do_not_move_preferred_endpoint(self):
        first = await self.endpoint(raw=b'{"stored":true,"event_id":"event"}')
        second = await self.endpoint()
        config = self.config([{"url": first}, {"url": second}])
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "s",
            "tool_use_id": "t",
            "tool_response": {"exit_code": 0},
            "tool_input": {"command": "secret"},
        }
        result = await run_hook(
            event,
            config.unix_socket,
            endpoints=config.hook_endpoints,
            state_file=config.hook_state_file,
        )
        self.assertEqual(result, {})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2]["event"]["exit_code"], 0)
        self.assertNotIn("secret", json.dumps(self.calls[0][2]))
        self.assertFalse(config.hook_state_file.exists())
