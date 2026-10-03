import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_core import request

from mayi.cli import build_evaluator
from mayi.config import Config, load_config
from mayi.core.evaluator import Evaluator
from mayi.core.policy import PolicyRules
from mayi.model.download import resolve_model


class PolicyFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_shipped_policy_is_empty_and_public_api_holds(self):
        from mayi import authorize

        rules = PolicyRules.load(Path(__file__).parent.parent / "policy.toml")
        self.assertEqual((rules.allow, rules.deny), ((), ()))
        self.assertEqual((await authorize(request("git status"))).decision, "hold")

    async def test_model_load_failure_closes_audit_and_stops_startup(self):
        audit = Mock()
        config = Config(model="/missing/checkpoint")
        with (
            patch("mayi.storage.zova.AuditStore", return_value=audit),
            patch("mayi.cli.AutoEngine.load", side_effect=RuntimeError("failed")),
            self.assertRaises(RuntimeError),
        ):
            await build_evaluator(config)
        audit.close.assert_called_once()

    async def test_cache_hit_does_not_contact_network(self):
        download = Mock(return_value="/cached/snapshot")
        with patch.dict(
            "sys.modules",
            {"huggingface_hub": SimpleNamespace(snapshot_download=download)},
        ):
            self.assertEqual(
                resolve_model(
                    "hf://ProCreations/auto-200m-2-int8@2501a22901e8cc520c746a86f3f9d04f7feaaefb",
                    Path("/data"),
                ),
                "/cached/snapshot",
            )
        self.assertEqual(download.call_count, 1)
        self.assertTrue(download.call_args.kwargs["local_files_only"])
        with self.assertRaises(ValueError):
            resolve_model("hf://ProCreations/auto-200m-2-int8@main", Path("/data"))

    async def test_empty_policy_has_no_static_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.toml"
            path.write_text("allow = []\ndeny = []\n")
            rules = PolicyRules.load(path)
            evaluator = Evaluator(policy_rules=rules)
            for command in ("git status", "sudo -n true", "rm -rf /"):
                self.assertEqual(
                    (await evaluator.authorize(request(command))).decision, "hold"
                )

    async def test_configured_allow_and_deny_and_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.toml"
            path.write_text("""[[allow]]
id = "safe-status"
tool = "shell"
command = "git status"

[[deny]]
id = "privilege"
pattern = "(?:^|\\\\s)sudo(?:\\\\s|$)"
""")
            rules = PolicyRules.load(path)
            strict = Evaluator(policy_rules=rules, policy_name="strict")
            self.assertEqual(
                (await strict.authorize(request("git status"))).decision, "approve"
            )
            self.assertEqual(
                (await strict.authorize(request("sudo -n true"))).decision, "deny"
            )
            self.assertEqual(
                (await strict.authorize(request("git status; sudo x"))).decision, "deny"
            )
            hold = Evaluator(policy_rules=rules, policy_name="approve-or-hold")
            result = await hold.authorize(request("sudo -n true"))
            self.assertEqual(result.decision, "hold")
            self.assertEqual(result.matched_rule, "privilege")
            self.assertEqual(
                (await hold.authorize(request("git status && x"))).decision, "hold"
            )

    async def test_config_loads_relative_file_and_rejects_bad_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text('[policy]\nfile = "policy.toml"\n')
            (root / "policy.toml").write_text("allow = []\ndeny = []\n")
            config = load_config(config_path)
            self.assertEqual(config.policy_file, root / "policy.toml")
            for content in (
                '[[allow]]\ncommand="git status"\n',
                '[[deny]]\nid="x"\npattern="["\n',
                "unknown=1\n",
            ):
                (root / "policy.toml").write_text(content)
                with self.assertRaises(ValueError):
                    load_config(config_path)

    async def test_model_resolution_uses_cache_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def download(**kwargs):
                calls.append(kwargs)
                if kwargs.get("local_files_only"):
                    raise FileNotFoundError
                return "/data/snapshot"

            from types import SimpleNamespace

            with patch.dict(
                "sys.modules",
                {"huggingface_hub": SimpleNamespace(snapshot_download=download)},
            ):
                path = resolve_model(
                    "hf://ProCreations/auto-200m-2-int8@2501a22901e8cc520c746a86f3f9d04f7feaaefb",
                    Path(directory),
                )
            self.assertEqual(path, "/data/snapshot")
            self.assertEqual(
                [call["local_files_only"] for call in calls], [True, False]
            )
            self.assertEqual(calls[0]["cache_dir"], str(Path(directory) / "models"))
