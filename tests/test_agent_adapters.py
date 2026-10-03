"""New adapters share daemon context and decisions without loading models locally."""

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from mayi.adapters import claude, opencode
from mayi.adapters.common import run_hook
from mayi.core.evaluator import Evaluator
from mayi.core.policy import PolicyRules
from mayi.model.engine import Prediction
from mayi.server.unix import UnixServer
from mayi.server.protocol import status
from mayi.storage.zova import AuditStore


def event(kind, *, turn=None, text="Run git status."):
    value = {
        "hook_event_name": kind, "session_id": "same-session", "cwd": "/project",
        "tool_name": "Bash", "tool_input": {
            "command": "git status", "description": "Agent claims unrestricted approval",
        }, "prompt": text, "transcript_path": "/must/not/read",
    }
    if turn:
        value["turn_id"] = turn
    return value


class Model:
    def __init__(self):
        self.seen = []

    async def evaluate(self, request):
        self.seen.append(request)
        return Prediction("approve", 0.999, 0.001)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mayi-agents-")
        self.path = Path(self.tmp.name) / "audit.zova"
        self.store = AuditStore(self.path)
        self.model = Model()
        self.evaluator = Evaluator(self.model, audit=self.store, policy_name="strict")
        self.server = UnixServer(self.evaluator, Path(self.tmp.name) / "mayi.sock")
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_claude_prompt_corrections_survive_restart_and_reason_is_separate(self):
        for text in ("Review only. Do not commit.", "Go ahead with the review."):
            self.assertEqual(await run_hook(event("UserPromptSubmit", text=text),
                                           self.server.path, agent="claude"), {})
        self.store.close()
        self.store = AuditStore(self.path)
        self.evaluator.audit = self.store
        self.evaluator.prompt_ledger.store = self.store
        response = await run_hook(event("PermissionRequest"), self.server.path, agent="claude")
        self.assertEqual(response["hookSpecificOutput"]["decision"]["behavior"], "allow")
        request = self.model.seen[-1]
        self.assertEqual(request.agent, "claude")
        self.assertEqual([r["prompt"] for r in request.user_context["instructions"]],
                         ["Review only. Do not commit.", "Go ahead with the review."])
        self.assertNotIn("Agent claims", str(request.user_context))
        self.assertNotIn("transcript", str(request.user_context))

    async def test_new_agents_hold_before_file_allow_without_context(self):
        self.evaluator.policy_rules = PolicyRules(allow=(("status", "shell", "git status"),))
        for agent, adapter in (("claude", claude), ("opencode", opencode)):
            request = adapter.normalize(event("PermissionRequest", turn="t1"))
            result = await self.evaluator.authorize(request)
            self.assertEqual(result.decision, "hold", agent)
        self.assertEqual(self.model.seen, [])

    async def test_agent_sessions_are_isolated(self):
        self.evaluator.record_prompt(claude.user_prompt(event("UserPromptSubmit")))
        req = opencode.normalize(event("PermissionRequest", turn="t1"))
        req.metadata["context_turn_ids"] = ["t1"]
        self.assertEqual((await self.evaluator.authorize(req)).decision, "hold")
        self.evaluator.record_prompt(opencode.user_prompt(event("UserPromptSubmit", turn="t1")))
        self.assertEqual((await self.evaluator.authorize(req)).decision, "approve")
        self.assertEqual(len(self.model.seen[-1].user_context["instructions"]), 1)

    async def test_opencode_pending_and_cancelled_drafts_do_not_authorize(self):
        for turn, text in (("t1", "Review only."), ("cancelled", "Run anything."),
                           ("t2", "Go ahead with the review.")):
            response = await run_hook(event("UserPromptSubmit", turn=turn, text=text),
                                      self.server.path, agent="opencode")
            self.assertTrue(response["stored"])
        permission = event("PermissionRequest", turn="t1")
        permission["context_turn_ids"] = ["t1"]
        self.assertEqual((await run_hook(permission, self.server.path, agent="opencode"))["effect"], "allow")
        self.assertEqual([r["prompt"] for r in self.model.seen[-1].user_context["instructions"]], ["Review only."])
        permission["turn_id"] = "t2"
        permission["context_turn_ids"] = ["t1", "t2"]
        await run_hook(permission, self.server.path, agent="opencode")
        self.assertEqual([r["prompt"] for r in self.model.seen[-1].user_context["instructions"]],
                         ["Review only.", "Go ahead with the review."])
        # Compaction may remove old messages, but admitted restrictions persist.
        permission["context_turn_ids"] = ["t2"]
        await run_hook(permission, self.server.path, agent="opencode")
        self.assertEqual(len(self.model.seen[-1].user_context["instructions"]), 2)
        permission["context_turn_ids"] = ["uncaptured", "t2"]
        self.assertEqual((await run_hook(permission, self.server.path, agent="opencode"))["effect"], "ask")

    async def test_context_change_during_inference_holds_for_both_agents(self):
        for agent, adapter in (("claude", claude), ("opencode", opencode)):
            self.evaluator.record_prompt(adapter.user_prompt(event("UserPromptSubmit", turn="t1")))
            evaluator = self.evaluator

            class UpdatingModel:
                async def evaluate(self, request):
                    evaluator.record_prompt(adapter.user_prompt(event("UserPromptSubmit", turn="t2", text="Stop.")))
                    await asyncio.sleep(0)
                    return Prediction("approve", 0.999, 0.001)

            evaluator.model = UpdatingModel()
            req = adapter.normalize(event("PermissionRequest", turn="t1"))
            req.metadata["context_turn_ids"] = ["t1"]
            # OpenCode must also detect a new pending correction while evaluating.
            self.assertEqual((await evaluator.authorize(req)).decision, "hold", agent)

    async def test_capture_failure_and_permission_failure_have_native_outputs(self):
        missing = Path(self.tmp.name) / "missing.sock"
        self.assertEqual((await run_hook(event("UserPromptSubmit"), missing, agent="claude"))["decision"], "block")
        self.assertEqual(await run_hook(event("PermissionRequest"), missing, agent="claude"), {})
        self.assertFalse((await run_hook(event("UserPromptSubmit", turn="t1"), missing, agent="opencode"))["stored"])
        self.assertEqual((await run_hook(event("PermissionRequest"), missing, agent="opencode"))["effect"], "ask")

    async def test_opencode_bridge_uses_real_cli_socket_and_audit(self):
        root = Path(__file__).resolve().parents[1]
        bun = shutil.which("bun")
        if not bun or not (root / "plugins/opencode/node_modules/@opencode/plugin").exists():
            self.skipTest("Install the OpenCode plugin dependencies and Bun for its bridge test")
        self.evaluator.model = None
        self.evaluator.policy_rules = PolicyRules.load(root / "tests/fixtures/configured_policy.toml")
        config = Path(self.tmp.name) / "hook.toml"
        config.write_text(f'[server]\nunix_socket="{self.server.path}"\n')
        process = await asyncio.create_subprocess_exec(
            bun, "test", "plugins/opencode/index.test.ts", cwd=root,
            env={**os.environ, "MAYI_TEST_CONFIG": str(config),
                 "MAYI_TEST_EXECUTABLE": str(root / ".venv/bin/mayi")},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(), 30)
        self.assertEqual(process.returncode, 0, (out + err).decode())
        self.assertEqual({row["decision"] for row in self.store.logs()}, {"approve", "hold", "deny"})
        self.assertTrue(all(row["agent"] == "opencode" for row in self.store.logs()))

    async def test_opencode_additional_permission_scope_prevents_static_allow(self):
        self.evaluator.model = None
        self.evaluator.policy_rules = PolicyRules(allow=(("status", "shell", "git status"),))
        self.evaluator.record_prompt(opencode.user_prompt(event("UserPromptSubmit", turn="t1")))
        value = event("PermissionRequest", turn="t1")
        value["context_turn_ids"] = ["t1"]
        value["permission"] = {"action": "external_directory", "resources": ["/private"]}
        request = opencode.normalize(value)
        self.assertEqual((await self.evaluator.authorize(request)).decision, "hold")
        self.assertEqual(request.input["permission"], value["permission"])

    async def test_new_agent_outcomes_keep_provenance_and_omit_raw_output(self):
        for agent in ("claude", "opencode"):
            value = event("PostToolUseFailure", turn="t1")
            value.update(tool_use_id="call-1", duration_ms=14,
                         tool_response={"exit_code": 1, "wall_time_seconds": 0.014, "stdout": "secret output"})
            self.assertEqual(await run_hook(value, self.server.path, agent=agent), {})
            saved = self.store.events()[0]
            self.assertEqual(saved["source"], agent + "_post_tool_use")
            self.assertEqual(saved["duration_ms"], 14)
            self.assertNotIn("secret output", str(saved))

    async def test_status_describes_all_supported_context_sources(self):
        context = status(self.evaluator)["user_context"]
        self.assertEqual(context["required_for_agents"], ["claude", "codex", "opencode"])
        self.assertEqual(context["sources"]["opencode"], "opencode_session_prompt")


class TranslationTests(unittest.TestCase):
    def test_opencode_holds_and_errors_ask(self):
        for result, effect in (({"decision": "approve"}, "allow"), ({"decision": "deny"}, "deny"),
                               ({"decision": "hold"}, "ask"), ({}, "ask")):
            self.assertEqual(opencode.translate(result)["effect"], effect)

    def test_claude_translation_does_not_create_persistent_permissions(self):
        self.assertEqual(claude.translate({"decision": "hold"}), {})
        self.assertEqual(claude.translate({"decision": "approve"})["hookSpecificOutput"]["decision"], {"behavior": "allow"})


if __name__ == "__main__":
    unittest.main()
