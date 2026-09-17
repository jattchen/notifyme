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
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["doctor"])
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

    def _bound_deliverer(self):
        binding = Binding(Path(self.tmpdir.name))
        binding.save(BarkEndpoint.parse("https://api.day.app/Abcdefgh1234"))
        transport = _CountTransport()
        return Deliverer(binding=binding, transport=transport), transport

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
