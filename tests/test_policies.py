import tempfile
import unittest
from pathlib import Path

from test_core import RULES, Model, request

from mayi.config import Config, load_config
from mayi.core.evaluator import Evaluator
from mayi.server.protocol import status
from mayi.storage.zova import AuditStore


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_approve_or_hold_never_denies_or_consults_model_for_deny_rules(self):
        model = Model()
        evaluator = Evaluator(model, policy_name="approve-or-hold", policy_rules=RULES)
        for command in (
            "sudo -n true",
            "rm -rf /",
            "git push --force",
            "cat ~/.ssh/config",
        ):
            result = await evaluator.authorize(request(command))
            self.assertEqual(result.decision, "hold")
            self.assertEqual(result.policy, "approve-or-hold")
            self.assertIsNotNone(result.matched_rule)
        self.assertEqual(model.calls, 0)
        self.assertEqual(
            (await evaluator.authorize(request("git status"))).decision, "approve"
        )
        self.assertEqual((await evaluator.authorize({})).decision, "hold")
        self.assertEqual(status(evaluator)["policy"], "approve-or-hold")

    async def test_default_policy_holds_deny_rules(self):
        result = await Evaluator(policy_rules=RULES).authorize(request("sudo x"))
        self.assertEqual(result.decision, "hold")
        self.assertEqual(result.policy, "approve-or-hold")
        self.assertEqual(Config().policy_mode, "approve-or-hold")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("")
            self.assertEqual(load_config(path).policy_mode, "approve-or-hold")

    async def test_explicit_strict_and_invalid_policy_rejected(self):
        result = await Evaluator(policy_name="strict", policy_rules=RULES).authorize(
            request("sudo x")
        )
        self.assertEqual(result.decision, "deny")
        self.assertEqual(result.policy, "strict")
        with self.assertRaises(ValueError):
            Evaluator(policy_name="allow-all")

    async def test_config_and_audit_preserve_policy_and_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text('[policy]\nmode="approve-or-hold"\n')
            config = load_config(config_path)
            self.assertEqual(config.policy_mode, "approve-or-hold")
            store = AuditStore(Path(tmp) / "audit.zova", retain_input=True)
            try:
                req = request("sudo x")
                req.reason = "user supplied explanation"
                result = await Evaluator(
                    policy_name=config.policy_mode, audit=store, policy_rules=RULES
                ).authorize(req)
                row = store.logs()[0]
                self.assertEqual(row["policy"], "approve-or-hold")
                self.assertEqual(row["matched_rule"], result.matched_rule)
                self.assertEqual(row["decision"], "hold")
                self.assertEqual(row["model_choice"], None)
            finally:
                store.close()
            config_path.write_text('[policy]\nmode="unknown"\n')
            with self.assertRaises(ValueError):
                load_config(config_path)

    async def test_audit_failure_cannot_restore_a_deny(self):
        class Broken:
            def record(self, *args):
                raise OSError("unavailable")

        result = await Evaluator(
            policy_name="approve-or-hold", audit=Broken(), policy_rules=RULES
        ).authorize(request("sudo x"))
        self.assertEqual(result.decision, "hold")
        self.assertEqual(result.policy, "approve-or-hold")
        self.assertIsNotNone(result.matched_rule)
