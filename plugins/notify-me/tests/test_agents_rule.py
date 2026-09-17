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
    has_managed_block,
    managed_block,
    plan,
)
from notify_me.bark import BarkEndpoint, TransportResult  # noqa: E402
from notify_me.binding import Binding  # noqa: E402
from notify_me.cli import _options, main  # noqa: E402
from notify_me.deliver import Deliverer  # noqa: E402
from notify_me.errors import NotifyMeError  # noqa: E402
from notify_me.install import run_install  # noqa: E402


class _CountTransport:
    def __init__(self):
        self.calls = 0

    def send_with_retry(self, endpoint, payload, sleep=None, max_attempts=2):
        self.calls += 1
        return TransportResult(True, False, "accepted", 200, 1)


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

    def test_commit_replaces_start_only_leftover(self):
        path = Path(self.tmpdir.name) / "AGENTS.md"
        path.write_text(
            "# 全局\n\n<!-- notify-me:managed:start version=grok-1 -->\nleftover chunk\n",
            encoding="utf-8",
        )
        result = commit()
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["action"], "replaced")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- notify-me:managed:start"), 1)
        self.assertEqual(text.count("<!-- notify-me:managed:end -->"), 1)
        self.assertIn(managed_block(), text)
        self.assertNotIn("leftover chunk", text)
        self.assertNotIn("version=grok-1", text)

    def test_commit_keeps_user_sections_after_dangling_start(self):
        path = Path(self.tmpdir.name) / "AGENTS.md"
        path.write_text(
            "# 全局\n\n"
            "<!-- notify-me:managed:start version=grok-1 -->\n"
            "leftover chunk\n"
            "## 我的其他规则\n"
            "- 用户自己的规则\n",
            encoding="utf-8",
        )
        result = commit()
        self.assertEqual(result["status"], "committed")
        text = path.read_text(encoding="utf-8")
        self.assertIn("## 我的其他规则", text)
        self.assertIn("- 用户自己的规则", text)
        self.assertIn("# 全局", text)
        self.assertIn(managed_block(), text)
        self.assertEqual(text.count("<!-- notify-me:managed:start"), 1)
        self.assertEqual(text.count("<!-- notify-me:managed:end -->"), 1)
        self.assertNotIn("<!-- notify-me:managed:start version=grok-1 -->", text)
        self.assertNotIn("leftover chunk", text)

    def test_commit_keeps_user_sections_between_dangling_start_and_later_block(self):
        path = Path(self.tmpdir.name) / "AGENTS.md"
        path.write_text(
            "# 全局\n\n"
            "<!-- notify-me:managed:start version=grok-1 -->\n"
            "leftover chunk\n"
            "## 我的其他规则\n"
            "- 用户自己的规则\n"
            "<!-- notify-me:managed:start version=6 -->\n"
            "old complete\n"
            "<!-- notify-me:managed:end -->\n",
            encoding="utf-8",
        )
        result = commit()
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["action"], "replaced")
        text = path.read_text(encoding="utf-8")
        self.assertIn("## 我的其他规则", text)
        self.assertIn("- 用户自己的规则", text)
        self.assertIn("# 全局", text)
        self.assertIn(managed_block(), text)
        self.assertEqual(text.count("<!-- notify-me:managed:start"), 1)
        self.assertEqual(text.count("<!-- notify-me:managed:end -->"), 1)
        self.assertNotIn("<!-- notify-me:managed:start version=grok-1 -->", text)
        self.assertNotIn("leftover chunk", text)
        self.assertNotIn("old complete", text)
        self.assertNotIn("version=6", text)

    def test_commit_follows_agents_symlink_instead_of_replacing_it(self):
        home = Path(self.tmpdir.name)
        target = home / "dotfiles" / "AGENTS.md"
        target.parent.mkdir()
        target.write_text("# 来自 dotfiles\n", encoding="utf-8")
        path = home / "AGENTS.md"
        path.symlink_to(target)
        self.assertTrue(path.is_symlink())

        result = commit()

        self.assertEqual(result["status"], "committed")
        self.assertTrue(path.is_symlink(), "commit must not replace AGENTS.md symlink with a regular file")
        self.assertEqual(path.resolve(), target.resolve())
        text = target.read_text(encoding="utf-8")
        self.assertIn(managed_block(), text)
        self.assertIn("# 来自 dotfiles", text)

    def test_commit_writes_backslash_body_literally(self):
        from unittest import mock

        from notify_me import agents_rule

        block = (
            "<!-- notify-me:managed:start version={} -->\n"
            "keep \\1 and \\\\ here\n"
            "<!-- notify-me:managed:end -->"
        ).format(MANAGED_VERSION)
        path = Path(self.tmpdir.name) / "AGENTS.md"
        path.write_text(
            "# 全局\n\n<!-- notify-me:managed:start version=1 -->\nold\n<!-- notify-me:managed:end -->\n",
            encoding="utf-8",
        )
        with mock.patch.object(agents_rule, "managed_block", return_value=block):
            result = commit()
        self.assertEqual(result["status"], "committed")
        text = path.read_text(encoding="utf-8")
        self.assertIn("keep \\1 and \\\\ here", text)
        self.assertNotIn("\nold\n", text)

    def test_has_managed_block_false_for_start_only_leftover(self):
        path = Path(self.tmpdir.name) / "AGENTS.md"
        path.write_text(
            "<!-- notify-me:managed:start version={} -->\n".format(MANAGED_VERSION),
            encoding="utf-8",
        )
        self.assertFalse(has_managed_block())
        self.assertFalse(has_managed_block(path.read_text(encoding="utf-8")))


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

    def test_install_dry_run_does_not_install(self):
        from io import StringIO
        from unittest import mock

        buf = StringIO()
        with mock.patch("notify_me.cli.run_install") as run_install, mock.patch(
            "sys.stdout", buf
        ):
            code = main(["install", "--dry-run"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "dry_run")
        run_install.assert_not_called()

    def test_install_misspelled_dry_run_does_not_install(self):
        from io import StringIO
        from unittest import mock

        buf = StringIO()
        with mock.patch("notify_me.cli.run_install") as run_install, mock.patch(
            "sys.stdout", buf
        ):
            code = main(["install", "--dry-ru"])
        payload = json.loads(buf.getvalue())
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error"]["code"], "invalid_arguments")
        run_install.assert_not_called()

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
        self.assertEqual(payload["error"]["code"], "invalid_arguments")
        self.assertFalse((Path(self.tmpdir.name) / "binding.json").exists())

    def test_setup_dry_run_does_not_write_binding(self):
        from io import StringIO
        from unittest import mock

        buf = StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
            "notify_me.cli.getpass.getpass",
            return_value="https://api.day.app/Abcdefgh1234",
        ), mock.patch("sys.stdout", buf):
            code = main(["setup", "--dry-run"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse((Path(self.tmpdir.name) / "binding.json").exists())

    def test_setup_does_not_claim_bound_without_test_or_agents(self):
        from io import StringIO
        from unittest import mock

        transport = _CountTransport()
        buf = StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
            "notify_me.cli.getpass.getpass",
            return_value="https://api.day.app/Abcdefgh1234",
        ), mock.patch(
            "notify_me.cli.Deliverer",
            side_effect=lambda *args, **kwargs: Deliverer(
                binding=kwargs.get("binding") or Binding(Path(self.tmpdir.name)),
                transport=transport,
            ),
        ), mock.patch("sys.stdout", buf):
            code = main(["setup"])
        payload = json.loads(buf.getvalue())
        agents = Path(self.tmpdir.name) / "AGENTS.md"
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertNotEqual(
            (payload.get("status"), payload.get("test"), transport.calls, agents.is_file()),
            ("bound", None, 0, False),
            "setup must not report bound when no test ran and AGENTS was not written",
        )
        self.assertGreater(transport.calls, 0)
        self.assertEqual(payload.get("test"), "accepted")
        self.assertTrue(agents.is_file())
        self.assertTrue(has_managed_block(agents.read_text(encoding="utf-8")))
        self.assertIn("agents", payload)

    def test_setup_accepted_test_does_not_look_unbound_when_agents_write_fails(self):
        from io import StringIO
        from unittest import mock

        transport = _CountTransport()
        agents = Path(self.tmpdir.name) / "AGENTS.md"
        agents.mkdir()
        buf = StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
            "notify_me.cli.getpass.getpass",
            return_value="https://api.day.app/Abcdefgh1234",
        ), mock.patch(
            "notify_me.cli.Deliverer",
            side_effect=lambda *args, **kwargs: Deliverer(
                binding=kwargs.get("binding") or Binding(Path(self.tmpdir.name)),
                transport=transport,
            ),
        ), mock.patch("sys.stdout", buf):
            code = main(["setup"])
        payload = json.loads(buf.getvalue())
        error = payload.get("error") or {}
        self.assertNotEqual(code, 0)
        self.assertFalse(payload.get("ok"))
        self.assertGreater(transport.calls, 0)
        self.assertEqual(payload.get("status"), "bound")
        self.assertEqual(payload.get("test"), "accepted")
        self.assertEqual(payload.get("host"), "api.day.app")
        self.assertEqual(error.get("code"), "agents_write_failed")
        self.assertEqual(error.get("message"), "测试通知已被接受，卡在写入托管规则")
        self.assertNotEqual(error.get("code"), "internal_error")
        self.assertNotEqual(error.get("code"), "test_not_accepted")
        self.assertNotEqual(error.get("code"), "test_unconfirmed")
        bound = Binding().load()
        self.assertEqual(bound.host, "api.day.app")
        self.assertEqual(bound.key, "Abcdefgh1234")
        self.assertFalse(has_managed_block())
        self.assertNotIn("state_home", payload)
        self.assertNotIn("state_home", buf.getvalue())

    def test_install_accepted_test_does_not_look_unbound_when_agents_write_fails(self):
        from unittest import mock

        transport = _CountTransport()
        agents = Path(self.tmpdir.name) / "AGENTS.md"
        agents.mkdir()
        with mock.patch("notify_me.install._require_tty"), mock.patch(
            "notify_me.install._ensure_plugin",
            return_value=Path(self.tmpdir.name),
        ), mock.patch("notify_me.install._ensure_mcp"), mock.patch(
            "notify_me.install.getpass.getpass",
            return_value="https://api.day.app/Abcdefgh1234",
        ), mock.patch(
            "notify_me.install.Deliverer",
            side_effect=lambda *args, **kwargs: Deliverer(
                binding=kwargs.get("binding") or Binding(Path(self.tmpdir.name)),
                transport=transport,
            ),
        ):
            result = run_install()
        error = result.get("error") or {}
        self.assertFalse(result.get("ok"))
        self.assertGreater(transport.calls, 0)
        self.assertEqual(result.get("status"), "bound")
        self.assertEqual(result.get("test"), "accepted")
        self.assertEqual(result.get("host"), "api.day.app")
        self.assertEqual(error.get("code"), "agents_write_failed")
        self.assertEqual(error.get("message"), "测试通知已被接受，卡在写入托管规则")
        self.assertNotEqual(error.get("code"), "internal_error")
        self.assertNotEqual(error.get("code"), "test_not_accepted")
        self.assertNotEqual(error.get("code"), "test_unconfirmed")
        bound = Binding().load()
        self.assertEqual(bound.host, "api.day.app")
        self.assertEqual(bound.key, "Abcdefgh1234")
        self.assertFalse(has_managed_block())
        self.assertNotIn("state_home", result)

    def test_setup_timeout_keeps_binding_without_writing_agents(self):
        from io import StringIO
        from unittest import mock

        timed_out = {
            "ok": False,
            "status": "failed",
            "category": "timeout",
            "http_status": None,
            "attempts": 1,
        }
        buf = StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
            "notify_me.cli.getpass.getpass",
            return_value="https://bark.example.com/NewSlowKey1234",
        ), mock.patch("notify_me.cli.Deliverer") as deliverer_cls, mock.patch(
            "sys.stdout", buf
        ):
            deliverer_cls.return_value.test.return_value = timed_out
            code = main(["setup"])
        payload = json.loads(buf.getvalue())
        self.assertNotEqual(code, 0)
        self.assertFalse(payload.get("ok"))
        self.assertEqual(payload.get("error", {}).get("code"), "test_unconfirmed")
        self.assertNotEqual(payload.get("status"), "bound")
        bound = Binding().load()
        self.assertEqual(bound.host, "bark.example.com")
        self.assertEqual(bound.key, "NewSlowKey1234")
        self.assertFalse((Path(self.tmpdir.name) / "AGENTS.md").exists())

    def test_setup_unconfirmed_rebind_keeps_old_working_key(self):
        from io import StringIO
        from unittest import mock

        old = BarkEndpoint.parse("https://api.day.app/OldWorkingKey1")
        Binding().save(old)
        timed_out = {
            "ok": False,
            "status": "failed",
            "category": "timeout",
            "http_status": None,
            "attempts": 1,
        }
        buf = StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
            "notify_me.cli.getpass.getpass",
            return_value="https://bark.example.com/NewSlowKey1234",
        ), mock.patch("notify_me.cli.Deliverer") as deliverer_cls, mock.patch(
            "sys.stdout", buf
        ):
            deliverer_cls.return_value.test.return_value = timed_out
            code = main(["setup"])
        payload = json.loads(buf.getvalue())
        self.assertNotEqual(code, 0)
        self.assertFalse(payload.get("ok"))
        self.assertEqual(payload.get("error", {}).get("code"), "test_unconfirmed")
        self.assertNotEqual(payload.get("status"), "bound")
        bound = Binding().load()
        self.assertEqual(bound.host, "api.day.app")
        self.assertEqual(bound.key, "OldWorkingKey1")
        self.assertNotEqual(bound.key, "NewSlowKey1234")
        self.assertFalse((Path(self.tmpdir.name) / "AGENTS.md").exists())
        self.assertNotIn("state_home", payload)
        self.assertNotIn("state_home", buf.getvalue())
        self.assertNotIn("OldWorkingKey1", buf.getvalue())
        self.assertNotIn("NewSlowKey1234", buf.getvalue())

    def test_install_unconfirmed_rebind_keeps_old_working_key(self):
        from unittest import mock

        old = BarkEndpoint.parse("https://api.day.app/OldWorkingKey1")
        Binding().save(old)
        timed_out = {
            "ok": False,
            "status": "failed",
            "category": "timeout",
            "http_status": None,
            "attempts": 1,
        }
        with mock.patch("notify_me.install._require_tty"), mock.patch(
            "notify_me.install._ensure_plugin",
            return_value=Path(self.tmpdir.name),
        ), mock.patch("notify_me.install._ensure_mcp"), mock.patch(
            "notify_me.install.getpass.getpass",
            return_value="https://bark.example.com/NewSlowKey1234",
        ), mock.patch("notify_me.install.Deliverer") as deliverer_cls:
            deliverer_cls.return_value.test.return_value = timed_out
            result = run_install()
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error", {}).get("code"), "test_unconfirmed")
        self.assertNotEqual(result.get("status"), "bound")
        bound = Binding().load()
        self.assertEqual(bound.host, "api.day.app")
        self.assertEqual(bound.key, "OldWorkingKey1")
        self.assertNotEqual(bound.key, "NewSlowKey1234")
        self.assertFalse((Path(self.tmpdir.name) / "AGENTS.md").exists())
        self.assertNotIn("state_home", result)
        self.assertNotIn("OldWorkingKey1", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("NewSlowKey1234", json.dumps(result, ensure_ascii=False))

    def test_setup_rejected_test_does_not_claim_bound_or_write_agents(self):
        from io import StringIO
        from unittest import mock

        old = BarkEndpoint.parse("https://api.day.app/OldWorkingKey1")
        Binding().save(old)
        rejected = {
            "ok": False,
            "status": "failed",
            "category": "http",
            "http_status": 400,
            "attempts": 1,
        }
        buf = StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
            "notify_me.cli.getpass.getpass",
            return_value="https://bark.example.com/NewTypoKey123",
        ), mock.patch("notify_me.cli.Deliverer") as deliverer_cls, mock.patch(
            "sys.stdout", buf
        ):
            deliverer_cls.return_value.test.return_value = rejected
            code = main(["setup"])
        payload = json.loads(buf.getvalue())
        self.assertNotEqual(code, 0)
        self.assertFalse(payload.get("ok"))
        self.assertEqual(payload.get("error", {}).get("code"), "test_not_accepted")
        self.assertNotEqual(payload.get("status"), "bound")
        bound = Binding().load()
        self.assertEqual(bound.host, "api.day.app")
        self.assertEqual(bound.key, "OldWorkingKey1")
        self.assertFalse((Path(self.tmpdir.name) / "AGENTS.md").exists())

    def test_setup_wide_state_dir_is_not_clean_first_bind(self):
        import stat
        from io import StringIO
        from unittest import mock

        home = Path(self.tmpdir.name)
        Binding(home).save(BarkEndpoint.parse("https://api.day.app/OldWorkingKey1"))
        before = (home / "binding.json").read_text(encoding="utf-8")
        home.chmod(0o777)
        transport = _CountTransport()
        buf = StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
            "notify_me.cli.getpass.getpass",
            return_value="https://api.day.app/Abcdefgh1234",
        ), mock.patch(
            "notify_me.cli.Deliverer",
            side_effect=lambda *args, **kwargs: Deliverer(
                binding=kwargs.get("binding") or Binding(home),
                transport=transport,
            ),
        ), mock.patch("sys.stdout", buf):
            code = main(["setup"])
        payload = json.loads(buf.getvalue())
        self.assertFalse(
            bool(payload.get("ok")) and payload.get("status") == "bound",
            "setup must not look like a clean first bind after a previously-wide state dir",
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(payload.get("ok"))
        self.assertEqual(payload.get("error", {}).get("code"), "insecure_binding")
        self.assertNotEqual(payload.get("status"), "bound")
        self.assertNotEqual(payload.get("test"), "accepted")
        self.assertEqual(transport.calls, 0)
        self.assertNotIn("state_home", payload)
        self.assertNotIn("state_home", buf.getvalue())
        self.assertNotIn("OldWorkingKey1", buf.getvalue())
        self.assertNotIn("Abcdefgh1234", buf.getvalue())
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o777)
        self.assertEqual((home / "binding.json").read_text(encoding="utf-8"), before)
        self.assertFalse((home / "AGENTS.md").exists())

    def test_setup_wide_binding_file_is_kept_on_rejected_rebind(self):
        import stat
        from io import StringIO
        from unittest import mock

        home = Path(self.tmpdir.name)
        Binding(home).save(BarkEndpoint.parse("https://api.day.app/OldWorkingKey1"))
        path = home / "binding.json"
        before = path.read_text(encoding="utf-8")
        path.chmod(0o644)
        rejected = {
            "ok": False,
            "status": "failed",
            "category": "http",
            "http_status": 400,
            "attempts": 1,
        }
        buf = StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
            "notify_me.cli.getpass.getpass",
            return_value="https://bark.example.com/NewTypoKey123",
        ), mock.patch("notify_me.cli.Deliverer") as deliverer_cls, mock.patch(
            "sys.stdout", buf
        ):
            deliverer_cls.return_value.test.return_value = rejected
            code = main(["setup"])
        payload = json.loads(buf.getvalue())
        self.assertNotEqual(code, 0)
        self.assertFalse(payload.get("ok"))
        self.assertNotEqual(payload.get("status"), "bound")
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertNotIn("state_home", payload)
        self.assertNotIn("state_home", buf.getvalue())
        self.assertNotIn("OldWorkingKey1", buf.getvalue())
        self.assertNotIn("NewTypoKey123", buf.getvalue())
        self.assertFalse((home / "AGENTS.md").exists())

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

    def test_agents_rule_commit_dry_run_does_not_write_agents(self):
        from io import StringIO
        from unittest import mock

        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["agents-rule", "commit", "--dry-run"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertNotEqual(payload["status"], "committed")
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse((Path(self.tmpdir.name) / "AGENTS.md").exists())

    def test_doctor_agents_managed_false_for_start_only_leftover(self):
        from io import StringIO
        from unittest import mock

        Path(self.tmpdir.name).joinpath("AGENTS.md").write_text(
            "<!-- notify-me:managed:start version={} -->\n".format(MANAGED_VERSION),
            encoding="utf-8",
        )
        old_path = self._install_current_plugin_grok()
        buf = StringIO()
        try:
            with mock.patch("sys.stdout", buf):
                code = main(["doctor"])
        finally:
            self._restore_grok_path(old_path)
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["agents_managed"])

    def test_doctor_corrupt_binding_is_not_unbound_ok(self):
        from io import StringIO
        from unittest import mock

        path = Path(self.tmpdir.name) / "binding.json"
        path.write_text("{not-valid-json", encoding="utf-8")
        path.chmod(0o600)
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["doctor"])
        payload = json.loads(buf.getvalue())
        self.assertFalse(
            bool(payload.get("ok")) and payload.get("bound") is False,
            "doctor must not treat a corrupt binding as unbound-and-ok",
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload.get("error", {}).get("code"), "invalid_binding")

    def test_doctor_insecure_binding_is_not_unbound_ok(self):
        from io import StringIO
        from unittest import mock

        Binding(Path(self.tmpdir.name)).save(
            BarkEndpoint.parse("https://api.day.app/Abcdefgh1234")
        )
        path = Path(self.tmpdir.name) / "binding.json"
        path.chmod(0o644)
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["doctor"])
        payload = json.loads(buf.getvalue())
        self.assertFalse(
            bool(payload.get("ok")) and payload.get("bound") is False,
            "doctor must not treat a world-readable binding as unbound-and-ok",
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload.get("error", {}).get("code"), "insecure_binding")
        self.assertNotIn("Abcdefgh1234", buf.getvalue())

    def test_doctor_insecure_state_dir_is_not_bound_ok(self):
        import stat
        from io import StringIO
        from unittest import mock

        Binding(Path(self.tmpdir.name)).save(
            BarkEndpoint.parse("https://api.day.app/Abcdefgh1234")
        )
        home = Path(self.tmpdir.name)
        path = home / "binding.json"
        home.chmod(0o777)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["doctor"])
        payload = json.loads(buf.getvalue())
        self.assertFalse(
            bool(payload.get("ok")) and payload.get("bound") is True,
            "doctor must not treat a world-writable state dir as bound-and-ok",
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload.get("error", {}).get("code"), "insecure_binding")
        self.assertNotIn("Abcdefgh1234", buf.getvalue())
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o777)

    def test_doctor_corrupt_accepted_is_not_ok(self):
        from io import StringIO
        from unittest import mock

        path = Path(self.tmpdir.name) / "accepted.json"
        path.write_text("{not-valid-json", encoding="utf-8")
        path.chmod(0o600)
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["doctor"])
        payload = json.loads(buf.getvalue())
        self.assertFalse(
            bool(payload.get("ok")),
            "doctor must not treat a corrupt accepted.json as ok",
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload.get("error", {}).get("code"), "invalid_accepted")

    def test_doctor_does_not_leak_state_home(self):
        from io import StringIO
        from unittest import mock

        old_path = self._install_current_plugin_grok()
        buf = StringIO()
        try:
            with mock.patch("sys.stdout", buf):
                code = main(["doctor"])
        finally:
            self._restore_grok_path(old_path)
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertNotIn("state_home", payload)
        self.assertNotIn("state_home", buf.getvalue())

    def test_doctor_mcp_pointing_at_leftover_is_not_ok(self):
        from io import StringIO
        from unittest import mock

        home = Path(self.tmpdir.name)
        installed = home / "installed-plugins"
        leftover = installed / "notify-me-oldhash"
        current = installed / "notify-me-current"
        for plugin, mtime in ((leftover, 1_000), (current, 2_000)):
            scripts = plugin / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "notify_me.py").write_text("# notify-me\n", encoding="utf-8")
            (scripts / "mcp_server.py").write_text("# mcp\n", encoding="utf-8")
            package = scripts / "notify_me"
            package.mkdir()
            (package / "paths.py").write_text("# paths\n", encoding="utf-8")
            os.utime(plugin, (mtime, mtime))
        leftover_server = leftover.resolve() / "scripts" / "mcp_server.py"
        listed = (
            "  notify_me: python3 -u {0}\n"
            "  notifyme: python3 -u {0} --name notifyme\n"
        ).format(leftover_server)
        bindir = home / "bin"
        bindir.mkdir()
        grok = bindir / "grok"
        grok.write_text(
            """#!/bin/sh
if [ "$1" = mcp ] && [ "$2" = list ]; then
  printf '%s\\n' "$GROK_MCP_LIST"
  exit 0
fi
if [ "$1" = mcp ]; then
  printf '%s\\n' "$*" >> "$GROK_MCP_ADD_LOG"
fi
exit 0
""",
            encoding="utf-8",
        )
        grok.chmod(0o755)
        mutate_log = home / "mcp-mutate.log"
        buf = StringIO()
        old_path = os.environ.get("PATH")
        os.environ["PATH"] = "{}:{}".format(bindir, old_path or "")
        os.environ["GROK_MCP_LIST"] = listed
        os.environ["GROK_MCP_ADD_LOG"] = str(mutate_log)
        try:
            with mock.patch("sys.stdout", buf):
                code = main(["doctor"])
        finally:
            if old_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old_path
            os.environ.pop("GROK_MCP_LIST", None)
            os.environ.pop("GROK_MCP_ADD_LOG", None)
        payload = json.loads(buf.getvalue())
        self.assertFalse(
            bool(payload.get("ok")),
            "doctor must not treat leftover MCP as ok",
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(payload.get("error", {}).get("code"), "mcp_stale")
        self.assertNotIn("state_home", payload)
        self.assertNotIn("state_home", buf.getvalue())
        self.assertFalse(mutate_log.exists())

    def test_doctor_missing_plugin_dest_is_not_ok(self):
        from io import StringIO
        from unittest import mock

        Binding(Path(self.tmpdir.name)).save(
            BarkEndpoint.parse("https://api.day.app/Abcdefgh1234")
        )
        Path(self.tmpdir.name).joinpath("AGENTS.md").write_text(
            managed_block() + "\n",
            encoding="utf-8",
        )
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["doctor"])
        payload = json.loads(buf.getvalue())
        self.assertFalse(
            bool(payload.get("ok")),
            "doctor must not treat a missing plugin dest as ok",
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(payload.get("error", {}).get("code"), "plugin_missing")
        self.assertNotIn("state_home", payload)
        self.assertNotIn("state_home", buf.getvalue())
        self.assertNotIn("Abcdefgh1234", buf.getvalue())

    def test_doctor_unreadable_mcp_list_is_not_ok(self):
        from io import StringIO
        from unittest import mock

        home = Path(self.tmpdir.name)
        current = home / "installed-plugins" / "notify-me-current"
        scripts = current / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "notify_me.py").write_text("# notify-me\n", encoding="utf-8")
        (scripts / "mcp_server.py").write_text("# mcp\n", encoding="utf-8")
        package = scripts / "notify_me"
        package.mkdir()
        (package / "paths.py").write_text("# paths\n", encoding="utf-8")
        Binding(home).save(BarkEndpoint.parse("https://api.day.app/Abcdefgh1234"))
        (home / "AGENTS.md").write_text(managed_block() + "\n", encoding="utf-8")
        bindir = home / "bin"
        bindir.mkdir()
        buf = StringIO()
        old_path = os.environ.get("PATH")
        os.environ["PATH"] = str(bindir)
        try:
            with mock.patch("sys.stdout", buf):
                code = main(["doctor"])
        finally:
            if old_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old_path
        payload = json.loads(buf.getvalue())
        self.assertFalse(
            bool(payload.get("ok")),
            "doctor must not treat an unreadable MCP list as ok",
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(payload.get("error", {}).get("code"), "mcp_unreadable")
        self.assertNotIn("state_home", payload)
        self.assertNotIn("state_home", buf.getvalue())
        self.assertNotIn("Abcdefgh1234", buf.getvalue())

    def test_doctor_symlink_binding_is_not_unbound_ok(self):
        from io import StringIO
        from unittest import mock

        Binding(Path(self.tmpdir.name)).save(
            BarkEndpoint.parse("https://api.day.app/Abcdefgh1234")
        )
        path = Path(self.tmpdir.name) / "binding.json"
        planted = Path(self.tmpdir.name) / "planted-binding.json"
        planted.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        planted.chmod(0o600)
        path.unlink()
        path.symlink_to(planted)
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["doctor"])
        payload = json.loads(buf.getvalue())
        self.assertFalse(
            bool(payload.get("ok")) and payload.get("bound") is not False,
            "doctor must not treat a symlink binding as bound-and-ok",
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload.get("error", {}).get("code"), "insecure_binding")
        self.assertNotIn("Abcdefgh1234", buf.getvalue())
        self.assertTrue(path.is_symlink())

    def _bound_deliverer(self):
        binding = Binding(Path(self.tmpdir.name))
        binding.save(BarkEndpoint.parse("https://api.day.app/Abcdefgh1234"))
        transport = _CountTransport()
        return Deliverer(binding=binding, transport=transport), transport

    def _install_current_plugin_grok(self, listed=None):
        home = Path(self.tmpdir.name)
        current = home / "installed-plugins" / "notify-me-current"
        scripts = current / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "notify_me.py").write_text("# notify-me\n", encoding="utf-8")
        (scripts / "mcp_server.py").write_text("# mcp\n", encoding="utf-8")
        package = scripts / "notify_me"
        package.mkdir()
        (package / "paths.py").write_text("# paths\n", encoding="utf-8")
        server = current.resolve() / "scripts" / "mcp_server.py"
        if listed is None:
            listed = (
                "  notify_me: python3 -u {0}\n"
                "  notifyme: python3 -u {0} --name notifyme\n"
            ).format(server)
        bindir = home / "bin"
        bindir.mkdir(exist_ok=True)
        grok = bindir / "grok"
        grok.write_text(
            """#!/bin/sh
if [ "$1" = mcp ] && [ "$2" = list ]; then
  printf '%s\\n' "$GROK_MCP_LIST"
  exit 0
fi
exit 0
""",
            encoding="utf-8",
        )
        grok.chmod(0o755)
        old_path = os.environ.get("PATH")
        os.environ["PATH"] = "{}:{}".format(bindir, old_path or "")
        os.environ["GROK_MCP_LIST"] = listed
        return old_path

    def _restore_grok_path(self, old_path):
        if old_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old_path
        os.environ.pop("GROK_MCP_LIST", None)

    def test_options_rejects_misspelled_dry_run(self):
        with self.assertRaises(NotifyMeError) as caught:
            _options(["--dry-ru"])
        self.assertEqual(caught.exception.code, "invalid_arguments")

    def test_options_rejects_message_without_value(self):
        with self.assertRaises(NotifyMeError) as caught:
            _options(["--message", "--dry-run"])
        self.assertEqual(caught.exception.code, "invalid_arguments")

    def test_unknown_option_names_are_rejected(self):
        for tokens in (
            ["--dry-ru"],
            ["--dry_run"],
            ["--dry-runn"],
            ["--from-stdin"],
            ["--verbose"],
            ["--message"],
        ):
            with self.subTest(tokens=tokens):
                with self.assertRaises(NotifyMeError) as caught:
                    _options(tokens)
                self.assertEqual(caught.exception.code, "invalid_arguments")

    def test_test_misspelled_dry_run_does_not_post(self):
        from io import StringIO
        from unittest import mock

        deliverer, transport = self._bound_deliverer()
        buf = StringIO()
        with mock.patch("notify_me.cli.Deliverer", return_value=deliverer), mock.patch(
            "sys.stdout", buf
        ):
            code = main(["test", "--dry-ru"])
        self.assertNotEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["error"]["code"], "invalid_arguments")
        self.assertNotEqual(payload["error"]["message"], "message 必须是字符串")
        self.assertEqual(transport.calls, 0)

    def test_test_dry_run_still_dry_runs(self):
        from io import StringIO
        from unittest import mock

        deliverer, transport = self._bound_deliverer()
        buf = StringIO()
        with mock.patch("notify_me.cli.Deliverer", return_value=deliverer), mock.patch(
            "sys.stdout", buf
        ):
            code = main(["test", "--dry-run"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(transport.calls, 0)

    def test_test_message_flag_without_string_does_not_post(self):
        from io import StringIO
        from unittest import mock

        deliverer, transport = self._bound_deliverer()
        buf = StringIO()
        with mock.patch("notify_me.cli.Deliverer", return_value=deliverer), mock.patch(
            "sys.stdout", buf
        ):
            code = main(["test", "--message", "--dry-run"])
        self.assertNotEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["error"]["code"], "invalid_arguments")
        self.assertNotEqual(payload["error"]["message"], "message 必须是字符串")
        self.assertEqual(transport.calls, 0)
