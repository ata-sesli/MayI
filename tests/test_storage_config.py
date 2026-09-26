import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_core import Model, request

from mayi import Decision
from mayi.config import load_config
from mayi.core.evaluator import Evaluator
from mayi.storage.zova import AuditStore


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_zova_roundtrip_and_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.zova"
            store = AuditStore(path)
            evaluator = Evaluator(Model(), audit=store)
            for command in ("git status", "sudo x", "git commit -m 'quoted'"):
                await evaluator.authorize(request(command))
            store.close()
            store = AuditStore(path)
            try:
                rows = store.logs()
                self.assertEqual(len(rows), 3)
                semantic = next(row for row in rows if row["source"] == "julia")
                self.assertEqual(semantic["julia_approve_probability"], 0.99)
                self.assertEqual(semantic["operation"], "git commit -m 'quoted'")
                self.assertIsNone(semantic["human_decision"])
                self.assertNotIn("input", semantic)
                self.assertEqual(len(store.logs(decision="deny")), 1)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                store.close()

    async def test_audit_failure_holds_but_preserves_deny(self):
        class BrokenStore:
            def record(self, *args):
                raise OSError("disk unavailable")

        evaluator = Evaluator(Model(), audit=BrokenStore())
        self.assertEqual(
            (await evaluator.authorize(request("git status"))).decision, Decision.HOLD
        )
        self.assertEqual(
            (await evaluator.authorize(request("sudo x"))).decision, Decision.DENY
        )

    async def test_unknown_requests_are_audited_without_raw_body(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "audit.zova")
            try:
                await Evaluator(audit=store).authorize({"secret": "private"})
                row = store.logs()[0]
                self.assertEqual(row["decision"], "hold")
                self.assertIsNone(row["agent"])
                self.assertNotIn("private", str(row))
            finally:
                store.close()


class ConfigTests(unittest.TestCase):
    def config(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text)
            return load_config(path)

    def test_defaults_and_environment(self):
        config = self.config("")
        self.assertEqual(config.approval_threshold, 0.98)
        self.assertFalse(config.http_enabled)
        self.assertEqual(config.device, "cpu")
        with patch.dict(os.environ, {"MAYI_BEARER_TOKEN": "secret"}):
            config = self.config('[http]\nenabled=true\nhost="0.0.0.0"')
            self.assertEqual(config.bearer_token, "secret")

    def test_non_loopback_authentication_required(self):
        with patch.dict(os.environ, {}, clear=True):
            for host in ("0.0.0.0", "::", "example.test"):
                with self.subTest(host=host), self.assertRaises(ValueError):
                    self.config(f'[http]\nenabled=true\nhost="{host}"')

    def test_invalid_config_rejected(self):
        for text in (
            "[julia]\napproval_threshold=nan",
            "[julia]\napproval_threshold=2",
            '[http]\nenabled="yes"',
            "[http]\nport=0",
            '[http]\nssl_cert="cert.pem"',
            "[server]\nrequest_timeout=-1",
            '[http]\nbearer_token=""',
            "[unknown]\nx=1",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.config(text)
