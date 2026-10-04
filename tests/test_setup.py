import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mayi.cli import parser
from mayi.config import Config
from mayi.setup import configure_agent, executable_command, jsonc_text, setup_agent
from mayi.hook_client import valid_reply


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "settings.json"
        self.executable = ["/tmp/MayI with spaces/bin/mayi"]
        self.config = self.root / "hook.toml"

    def test_cli_accepts_each_agent_and_target(self):
        for agent in ("codex", "claude", "opencode"):
            args = parser().parse_args(["setup", agent, "--target", str(self.target)])
            self.assertEqual(args.agent, agent)

    def test_hook_merge_backup_and_repeat_without_duplicates(self):
        for agent in ("codex", "claude"):
            original = '{"theme":"dark","hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"other"}]}]}}\n'
            self.target.write_text(original)
            result = configure_agent(agent, self.target, self.executable, self.config, 30)
            self.assertEqual(Path(result["backup"]).read_text(), original)
            value = json.loads(self.target.read_text())
            self.assertEqual(value["theme"], "dark")
            self.assertEqual(value["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], "other")
            hook = value["hooks"]["PermissionRequest"][0]["hooks"][0]
            self.assertIn("--config", hook["command"])
            self.assertIn(f"hook {agent}", hook["command"])
            self.assertEqual(hook["timeout"], 30)
            before = self.target.read_bytes()
            result = configure_agent(agent, self.target, self.executable, self.config, 30)
            self.assertFalse(result["changed"])
            self.assertEqual(self.target.read_bytes(), before)

    def test_opencode_preserves_comments_and_other_plugins(self):
        self.target = self.root / "opencode.jsonc"
        original = '{\n // keep me\n "model":"example",\n "plugins": ["other", /* keep plugin comment */],\n}\n'
        self.target.write_text(original)
        plugin = self.root / "plugin"
        configure_agent("opencode", self.target, self.executable, self.config, 30, plugin=plugin)
        text = self.target.read_text()
        self.assertIn("// keep me", text)
        self.assertIn("/* keep plugin comment */", text)
        self.assertIn('"other"', text)
        self.assertIn(str(plugin), text)
        before = self.target.read_bytes()
        result = configure_agent("opencode", self.target, self.executable, self.config, 30, plugin=plugin)
        self.assertFalse(result["changed"])
        self.assertEqual(self.target.read_bytes(), before)
        configure_agent("opencode", self.target, self.executable, self.config, 40, plugin=plugin)
        self.assertEqual(self.target.read_text().count(str(plugin)), 1)

    def test_invalid_settings_and_symlinks_are_untouched(self):
        for text in ("invalid", '{"hooks":[]}', '{"hooks":{},"hooks":{}}'):
            self.target.write_text(text)
            with self.assertRaises(ValueError):
                configure_agent("codex", self.target, self.executable, self.config, 15)
            self.assertEqual(self.target.read_text(), text)
        real = self.root / "real.json"
        real.write_text("{}")
        self.target.unlink()
        self.target.symlink_to(real)
        with self.assertRaises(ValueError):
            configure_agent("claude", self.target, self.executable, self.config, 15)
        self.assertEqual(real.read_text(), "{}")

    def test_existing_mayi_entry_is_updated_without_removing_other_handlers(self):
        self.target.write_text(json.dumps({"hooks":{"PermissionRequest":[{"hooks":[
            {"type":"command","command":"/old/mayi hook codex"},
            {"type":"command","command":"echo 'mayi hook codex'"}
        ]}]}}))
        configure_agent("codex", self.target, self.executable, self.config, 15)
        hooks = json.loads(self.target.read_text())["hooks"]["PermissionRequest"]
        commands = [h["command"] for group in hooks for h in group["hooks"]]
        self.assertEqual(len(commands), 2)
        self.assertIn("echo 'mayi hook codex'", commands)
        self.assertNotIn("/old/mayi hook codex", commands)

    def test_module_invocation_uses_current_python(self):
        with patch("mayi.setup.sys.argv", ["/tmp/mayi/__main__.py"]):
            command = executable_command()
        self.assertEqual(command[1:], ["-m", "mayi"])

    def test_jsonc_string_contents_and_relative_plugin_are_preserved(self):
        self.target = self.root / "opencode.jsonc"
        plugin = self.root / "plugin"
        self.target.write_text('{"description":"keep ,} and ,]", "plugins":["./plugin"]}')
        configure_agent("opencode", self.target, self.executable, self.config, 15, plugin=plugin)
        self.assertIn('"keep ,} and ,]"', self.target.read_text())
        self.assertNotIn('"./plugin"', self.target.read_text())
        self.assertEqual(json.loads(jsonc_text(self.target.read_text())[1])["description"],
                         "keep ,} and ,]")

    def test_backup_preserves_original_bytes(self):
        original = b'{\r\n  "theme": "dark"\r\n}\r\n'
        self.target.write_bytes(original)
        result = configure_agent("codex", self.target, self.executable, self.config, 15)
        self.assertEqual(Path(result["backup"]).read_bytes(), original)

    def test_status_reply_is_valid_without_an_authorization_decision(self):
        self.assertTrue(valid_reply({"action":"status"}, {"status":"ready", "model_available":False}))
        self.assertFalse(valid_reply({"action":"status"}, {"decision":"approve"}))


class SetupIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_reports_offline_without_starting_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "hooks.json"
            config = Config(unix_socket=Path(directory) / "missing.sock")
            with patch("mayi.setup.executable_command", return_value=["/tmp/mayi"]):
                result = await setup_agent("codex", config, target=target)
            self.assertEqual(result["daemon"], "unavailable")
            self.assertTrue(target.exists())

    async def test_absent_default_config_is_not_made_explicit_in_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "hooks.json"
            with patch("mayi.setup.DEFAULT_PATH", Path(directory) / "missing.toml"), patch(
                "mayi.setup.check_daemon", new_callable=AsyncMock, return_value={"daemon":"unavailable"}
            ):
                await setup_agent("codex", Config(), target=target)
            command = json.loads(target.read_text())["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
            self.assertNotIn("--config", command)

    async def test_opencode_installs_dependencies_before_config_write(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "opencode.json"
            with patch("mayi.setup.install_plugin", new_callable=AsyncMock) as install, patch(
                "mayi.setup.check_daemon", new_callable=AsyncMock,
                return_value={"daemon":"ready", "model_available":True}
            ), patch("mayi.setup.executable_command", return_value=["/tmp/mayi"]):
                result = await setup_agent("opencode", Config(), target=target)
            install.assert_awaited_once()
            self.assertEqual(result["daemon"], "ready")
            self.assertIn("plugins", json.loads(target.read_text()))
