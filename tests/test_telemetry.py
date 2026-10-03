import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_core import RULES

from mayi.adapters.codex import run_hook
from mayi.config import HookEndpoint
from mayi.core.evaluator import Evaluator
from mayi.server.protocol import dispatch, encode, status
from mayi.storage.zova import AuditStore


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_decision_context_timings_and_persistent_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.zova"
            audit = AuditStore(path)
            evaluator = Evaluator(
                audit=audit, policy_name="approve-or-hold", policy_rules=RULES
            )
            result = await evaluator.authorize(
                {"agent": "test", "tool": "shell", "operation": "git status"}
            )
            value = result.to_dict()
            self.assertTrue(value["request_id"])
            self.assertEqual(value["context"]["threshold"], 0.98)
            self.assertTrue(value["context"]["config_version"])
            for name in ("policy_ms", "model_ms", "audit_ms", "total_ms"):
                self.assertGreaterEqual(value["timings"][name], 0)
            self.assertGreaterEqual(
                value["timings"]["total_ms"], value["timings"]["audit_ms"]
            )
            self.assertEqual(audit.logs()[0]["request_id"], value["request_id"])
            audit.close()
            audit = AuditStore(path)
            try:
                event = audit.events()[0]
                self.assertEqual(event["kind"], "decision_completed")
                self.assertEqual(event["request_id"], value["request_id"])
                self.assertEqual(event["timings"], value["timings"])
            finally:
                audit.close()
            health = status(evaluator)["telemetry"]
            self.assertEqual(health["requests"], 1)
            self.assertEqual(health["decisions"]["approve"], 1)
            self.assertEqual(health["active_requests"], 0)

    async def test_health_tracks_timeout_and_audit_failure_without_approval(self):
        class SlowModel:
            async def evaluate(self, request):
                await asyncio.sleep(1)

        class BrokenAudit:
            def record(self, *args):
                raise OSError("secret")

        evaluator = Evaluator(SlowModel(), timeout=0.001, audit=BrokenAudit())
        result = await evaluator.authorize(
            {"agent": "test", "tool": "shell", "operation": "unknown"}
        )
        self.assertEqual(result.decision, "hold")
        health = status(evaluator)["telemetry"]
        self.assertEqual(health["model_timeouts"], 1)
        self.assertEqual(health["audit_failures"], 1)
        self.assertNotIn("secret", json.dumps(result.to_dict()))

    async def test_event_validation_persistence_and_explicit_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = AuditStore(Path(directory) / "audit.zova")
            evaluator = Evaluator(audit=audit)
            try:
                event = {
                    "kind": "tool_outcome",
                    "session_id": "session",
                    "tool_use_id": "call",
                    "tool": "Bash",
                    "exit_code": 1,
                    "duration_ms": 20,
                }
                response = await dispatch(
                    evaluator, encode({"action": "event", "event": event})
                )
                self.assertTrue(response["stored"])
                saved = audit.events()[0]
                self.assertEqual(saved["exit_code"], 1)
                self.assertIsNone(saved.get("human_decision"))
                bad = await dispatch(
                    evaluator,
                    encode({"action": "event", "event": {**event, "output": "secret"}}),
                )
                self.assertFalse(bad["stored"])
                self.assertEqual(len(audit.events()), 1)
                result = await evaluator.authorize({"agent": "test", "tool": "shell"})
                feedback = {
                    "kind": "human_feedback",
                    "request_id": result.request_id,
                    "human_decision": "approve",
                }
                self.assertTrue(
                    (
                        await dispatch(
                            evaluator, encode({"action": "event", "event": feedback})
                        )
                    )["stored"]
                )
                self.assertEqual(audit.events()[0]["source"], "explicit_feedback")
                self.assertEqual(status(evaluator)["telemetry"]["requests"], 1)
            finally:
                audit.close()

    async def test_hook_records_private_routing_without_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "routing.jsonl"
            endpoints = (
                HookEndpoint(unix_socket=Path("/missing")),
                HookEndpoint(unix_socket=Path("/available")),
            )
            event = {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"command": "secret"},
                "session_id": "session",
            }
            with patch(
                "mayi.hook_client.query",
                AsyncMock(
                    side_effect=[FileNotFoundError(2, "missing"), {"decision": "hold"}]
                ),
            ):
                self.assertEqual(
                    await run_hook(
                        event, Path("/unused"), endpoints=endpoints, telemetry_file=log
                    ),
                    {},
                )
            record = json.loads(log.read_text())
            self.assertEqual(record["selected_endpoint"], 1)
            self.assertEqual(
                [item["outcome"] for item in record["attempts"]],
                ["unavailable", "reply"],
            )
            self.assertNotIn("secret", log.read_text())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    async def test_post_tool_use_only_sends_observed_metadata(self):
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "s",
            "tool_use_id": "t",
            "tool_input": {"command": "secret"},
            "tool_response": {
                "exit_code": 2,
                "wall_time_seconds": 0.25,
                "output": "secret",
            },
        }
        with patch(
            "mayi.adapters.common.query", AsyncMock(return_value={"stored": True})
        ) as query:
            self.assertEqual(await run_hook(event, Path("/unused")), {})
            payload = query.call_args.args[1]
            self.assertEqual(payload["action"], "event")
            self.assertEqual(payload["event"]["exit_code"], 2)
            self.assertEqual(payload["event"]["duration_ms"], 250)
            self.assertNotIn("secret", json.dumps(payload))
            self.assertNotIn("human_decision", payload["event"])

    async def test_http_events_require_auth_and_do_not_evaluate_operations(self):
        from test_transports import http_call

        from mayi.server.http import Application

        with tempfile.TemporaryDirectory() as directory:
            audit = AuditStore(Path(directory) / "audit.zova")
            evaluator = Evaluator(audit=audit)
            app = Application(evaluator, token="test-token")
            event = {
                "kind": "tool_outcome",
                "tool": "Bash",
                "session_id": "s",
                "tool_use_id": "t",
            }
            try:
                code, _ = await http_call(app, path="/v1/events", data=event)
                self.assertEqual(code, 401)
                self.assertEqual(audit.events(), [])
                headers = ((b"authorization", b"Bearer test-token"),)
                code, result = await http_call(
                    app, path="/v1/events", data=event, headers=headers
                )
                self.assertEqual(code, 200)
                self.assertTrue(result["stored"])
                self.assertEqual(evaluator.health()["requests"], 0)
                self.assertEqual(audit.logs(), [])
                for bad in (
                    {**event, "duration_ms": -1},
                    {**event, "exit_code": True},
                    {**event, "kind": []},
                ):
                    self.assertEqual(
                        (
                            await http_call(
                                app, path="/v1/events", data=bad, headers=headers
                            )
                        )[0],
                        400,
                    )
            finally:
                audit.close()

    async def test_unknown_feedback_and_missing_outcome_details_stay_unknown(self):
        from mayi.adapters.codex import tool_outcome

        event = tool_outcome(
            {
                "tool_name": "Bash",
                "session_id": "s",
                "tool_use_id": "t",
                "tool_response": "Process exited with code 0; secret",
            }
        )
        self.assertIsNone(event["exit_code"])
        self.assertIsNone(event["duration_ms"])
        self.assertNotIn("secret", json.dumps(event))
        with tempfile.TemporaryDirectory() as directory:
            audit = AuditStore(Path(directory) / "audit.zova")
            try:
                evaluator = Evaluator(audit=audit)
                result = evaluator.record_event(
                    {
                        "kind": "human_feedback",
                        "request_id": "00000000-0000-0000-0000-000000000000",
                        "human_decision": "deny",
                    }
                )
                self.assertFalse(result["stored"])
                self.assertEqual(audit.events(), [])
            finally:
                audit.close()

    async def test_routing_logging_cannot_follow_symlinks_or_change_decisions(self):
        from mayi.telemetry import append_routing

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "keep"
            target.write_text("keep")
            link = Path(directory) / "routing"
            link.symlink_to(target)
            append_routing(link, {"attempts": []})
            self.assertEqual(target.read_text(), "keep")
            with patch(
                "mayi.adapters.common.query",
                AsyncMock(return_value={"decision": "approve"}),
            ):
                result = await run_hook(
                    {
                        "hook_event_name": "PermissionRequest",
                        "tool_name": "Bash",
                        "tool_input": {"command": "git status"},
                    },
                    Path("/unused"),
                    telemetry_file=link,
                )
                self.assertEqual(
                    result["hookSpecificOutput"]["decision"]["behavior"], "allow"
                )
