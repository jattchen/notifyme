import json
import os
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(ROOT))

from notify_me.agents_rule import (  # noqa: E402
    MANAGED_VERSION,
    commit,
    managed_block,
    plan,
)
from notify_me.cli import main  # noqa: E402


class AgentsRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["GROK_HOME"] = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("GROK_HOME", None)

    def test_plan_does_not_write(self):
        result = plan()
        self.assertEqual(result["status"], "plan")
        self.assertIn(MANAGED_VERSION, result["block"])
        self.assertIn("notifyme__notifyme", result["block"])
        self.assertIn("notify_me__notify_me", result["block"])
        self.assertIn("没有该工具时用", result["block"])
        self.assertIn("直接调 MCP 工具", result["block"])
        self.assertIn("不是 Skill", result["block"])
        self.assertIn("condition=answer", result["block"])
        self.assertIn("condition=done", result["block"])
        self.assertIn("等用户去操作", result["block"])
        self.assertNotIn("SKILL.md", result["block"])
        self.assertNotIn("要去外部操作", result["block"])
        self.assertNotIn("终端或浏览器", result["block"])
        self.assertNotIn("condition=blocking", result["block"])
        self.assertFalse((Path(self.tmpdir.name) / "AGENTS.md").exists())

    def test_commit_writes_without_prompt(self):
        result = commit()
        self.assertEqual(result["status"], "committed")
        path = Path(self.tmpdir.name) / "AGENTS.md"
        self.assertTrue(path.is_file())
        self.assertIn(managed_block(), path.read_text(encoding="utf-8"))

    def test_commit_replaces_old_managed_block(self):
        path = Path(self.tmpdir.name) / "AGENTS.md"
        path.write_text(
            "# 全局\n\n<!-- notify-me:managed:start version=grok-1 -->\nold\n<!-- notify-me:managed:end -->\n",
            encoding="utf-8",
        )
        result = commit()
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["action"], "replaced")
        text = path.read_text(encoding="utf-8")
        self.assertIn(managed_block(), text)
        self.assertNotIn("version=grok-1", text)
        self.assertNotIn("\nold\n", text)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["GROK_HOME"] = self.tmpdir.name
        os.environ["GROK_NOTIFY_ME_HOME"] = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("GROK_HOME", None)
        os.environ.pop("GROK_NOTIFY_ME_HOME", None)

    def test_install_without_tty_refuses(self):
        from io import StringIO
        from unittest import mock

        buf = StringIO()
        with mock.patch("sys.stdin.isatty", return_value=False), mock.patch("sys.stdout", buf):
            code = main(["install"])
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["error"]["code"], "tty_required")

    def test_setup_without_tty_refuses(self):
        from io import StringIO
        from unittest import mock

        buf = StringIO()
        with mock.patch("sys.stdin.isatty", return_value=False), mock.patch("sys.stdout", buf):
            code = main(["setup"])
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["error"]["code"], "tty_required")

    def test_setup_from_stdin_does_not_bypass_tty(self):
        from io import StringIO
        from unittest import mock

        fake_stdin = StringIO("https://api.day.app/Abcdefgh1234\n")
        fake_stdin.isatty = lambda: False
        buf = StringIO()
        with mock.patch("sys.stdin", fake_stdin), mock.patch("sys.stdout", buf):
            code = main(["setup", "--from-stdin"])
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["error"]["code"], "tty_required")
        self.assertFalse((Path(self.tmpdir.name) / "binding.json").exists())

    def test_agents_rule_plan_json(self):
        from io import StringIO
        from unittest import mock

        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["agents-rule", "plan"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "plan")
