import json
import os
import subprocess
import sys
import tempfile
import shlex
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from notify_me.agents_rule import (  # noqa: E402
    MANAGED_END,
    MANAGED_START,
    has_managed_block,
    managed_file,
)
from notify_me.binding import Binding  # noqa: E402
from notify_me.bark import BarkEndpoint, TransportResult  # noqa: E402
from notify_me.deliver import DEFAULT_BARK_ICON_URL, DEFAULT_GROUP, Deliverer, TOOL_SCHEMA  # noqa: E402
from notify_me.host_install import PLUGIN_ID, commit_permission, permission_allowed  # noqa: E402

REPO_ROOT = PLUGIN_ROOT.parents[1]
TOOL = "mcp__plugin_notify-me-claude_notifyme_claude__notifyme"


class ClaudeEnv:
    """Point every Claude Code and Notify Me path at a temp directory."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "claude"
        self.home.mkdir()
        self._saved = {
            key: os.environ.get(key)
            for key in ("CLAUDE_CONFIG_DIR", "CLAUDE_NOTIFY_ME_HOME")
        }
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.home)
        os.environ["CLAUDE_NOTIFY_ME_HOME"] = str(root / "state")
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()


class ClaudePackageTests(unittest.TestCase):
    def test_plugin_identity_is_claude(self):
        manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "notify-me-claude")
        self.assertEqual(manifest["version"], "0.1.0")

        mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())
        server = mcp["mcpServers"]["notifyme_claude"]
        self.assertEqual(server["command"], "python3")
        self.assertEqual(server["args"][-1], "${CLAUDE_PLUGIN_ROOT}/scripts/mcp_server.py")

        skill = (PLUGIN_ROOT / "skills" / "notify-me-claude" / "SKILL.md").read_text()
        self.assertIn("disable-model-invocation: true", skill)
        self.assertIn(TOOL, skill)
        self.assertNotIn("notifyme_cursor", skill)
        self.assertNotIn("mcp__notifyme_codex__notifyme", skill)

    def test_marketplace_lists_claude_plugin(self):
        market = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(market["name"], "notifyme")
        self.assertEqual(PLUGIN_ID, "notify-me-claude@" + market["name"])
        entries = {entry["name"]: entry for entry in market["plugins"]}
        self.assertEqual(entries["notify-me-claude"]["source"], "./plugins/notify-me-claude")
        manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(entries["notify-me-claude"]["version"], manifest["version"])

    def test_claude_schema_is_flat_and_explicit(self):
        schema = TOOL_SCHEMA["inputSchema"]
        self.assertEqual(
            set(schema["properties"]),
            {"condition", "item_id", "state", "message", "dry_run", "workspace", "url"},
        )
        self.assertNotIn("op", schema["properties"])
        self.assertEqual(
            schema["required"],
            ["condition", "item_id", "state", "message", "workspace"],
        )
        for combinator in ("allOf", "anyOf", "oneOf", "if", "then"):
            self.assertNotIn(combinator, schema)
        self.assertIs(schema["additionalProperties"], False)

    def test_claude_rule_is_idempotent_and_names_plugin_tool(self):
        from notify_me import agents_rule

        with ClaudeEnv() as env:
            target = env.home / "rules" / "notify-me-claude.md"
            first = agents_rule.commit()
            second = agents_rule.commit()
            text = target.read_text()
            self.assertEqual(first["action"], "appended")
            self.assertEqual(second["action"], "replaced")
            self.assertEqual(text, managed_file())
            self.assertEqual(text.count(MANAGED_START), 1)
            self.assertEqual(text.count(MANAGED_END), 1)
            # A rule without `paths` frontmatter loads in every project.
            self.assertFalse(text.startswith("---"))
            self.assertIn("覆盖全部内容", text)
            self.assertIn(TOOL, text)
            self.assertTrue(has_managed_block(text))
            self.assertTrue(has_managed_block())
            for field in ("condition", "item_id", "state", "message", "workspace"):
                self.assertIn(field, agents_rule.managed_block())
            self.assertIn("不得传 op", agents_rule.managed_block())

    def test_permission_created_when_settings_missing(self):
        with ClaudeEnv() as env:
            self.assertFalse(permission_allowed())
            result = commit_permission()
            data = json.loads((env.home / "settings.json").read_text())
            self.assertEqual(result["action"], "appended")
            self.assertEqual(data, {"permissions": {"allow": [TOOL]}})
            self.assertTrue(permission_allowed())

    def test_permission_merge_preserves_other_settings(self):
        with ClaudeEnv() as env:
            settings = env.home / "settings.json"
            original = {
                "model": "opus",
                "enabledPlugins": {"other@market": True},
                "permissions": {"allow": ["Bash(git status)"], "deny": ["Read(.env)"]},
            }
            settings.write_text(json.dumps(original), encoding="utf-8")
            os.chmod(settings, 0o600)
            first = commit_permission()
            second = commit_permission()
            data = json.loads(settings.read_text())
            self.assertEqual(first["action"], "appended")
            self.assertEqual(second["action"], "unchanged")
            self.assertEqual(data["model"], "opus")
            self.assertEqual(data["enabledPlugins"], {"other@market": True})
            self.assertEqual(data["permissions"]["deny"], ["Read(.env)"])
            self.assertEqual(data["permissions"]["allow"], ["Bash(git status)", TOOL])
            self.assertEqual(settings.stat().st_mode & 0o777, 0o600)

    def test_permission_adds_allow_when_permissions_has_no_allow(self):
        with ClaudeEnv() as env:
            settings = env.home / "settings.json"
            settings.write_text(json.dumps({"permissions": {"defaultMode": "auto"}}))
            commit_permission()
            data = json.loads(settings.read_text())
            self.assertEqual(data["permissions"], {"defaultMode": "auto", "allow": [TOOL]})

    def test_permission_refuses_to_clobber_unreadable_settings(self):
        from notify_me.errors import NotifyMeError

        with ClaudeEnv() as env:
            settings = env.home / "settings.json"
            settings.write_text("{not json", encoding="utf-8")
            with self.assertRaises(NotifyMeError) as raised:
                commit_permission()
            self.assertEqual(raised.exception.code, "settings_unreadable")
            self.assertEqual(settings.read_text(), "{not json")

    def test_deny_or_ask_rule_blocks_permission(self):
        from notify_me.host_install import permission_blocked

        server = TOOL.rsplit("__", 1)[0]
        for kind, rule in (("deny", TOOL), ("ask", server), ("deny", server + "__*")):
            with ClaudeEnv() as env:
                (env.home / "settings.json").write_text(
                    json.dumps({"permissions": {"allow": [TOOL], kind: [rule, "Bash(rm *)"]}})
                )
                self.assertFalse(permission_allowed(), rule)
                self.assertEqual(permission_blocked(), [rule])
        with ClaudeEnv() as env:
            (env.home / "settings.json").write_text(json.dumps({"permissions": {"allow": [server]}}))
            self.assertTrue(permission_allowed())
            self.assertEqual(permission_blocked(), [])

    def test_doctor_reports_claude_install_state(self):
        from notify_me import cli

        with ClaudeEnv() as env:
            plugins = env.home / "plugins"
            plugins.mkdir()
            (plugins / "installed_plugins.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "plugins": {PLUGIN_ID: [{"scope": "user", "installPath": str(PLUGIN_ROOT)}]},
                    }
                )
            )
            (env.home / "settings.json").write_text(
                json.dumps({"enabledPlugins": {PLUGIN_ID: True}})
            )
            from notify_me import agents_rule

            agents_rule.commit()
            commit_permission()
            report = cli._doctor(Deliverer())
            self.assertEqual(report["host"], "claude")
            self.assertTrue(report["rule_managed"])
            self.assertTrue(report["permission_allowed"])
            self.assertEqual(report["permission_blocked_by"], [])
            self.assertTrue(report["plugin_installed"])
            self.assertTrue(report["plugin_enabled"])
            self.assertTrue(report["mcp_server_present"])
            self.assertFalse(report["bound"])

    def test_tool_name_derives_from_manifests(self):
        from notify_me.agents_rule import TOOL_PERMISSION
        from notify_me.deliver import TOOL_NAME

        manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
        (server,) = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())["mcpServers"]
        derived = "mcp__plugin_{}_{}__{}".format(manifest["name"], server, TOOL_NAME)
        self.assertEqual(TOOL_PERMISSION, derived)
        self.assertEqual(TOOL, derived)
        self.assertIn(derived, (PLUGIN_ROOT / "skills" / "notify-me-claude" / "SKILL.md").read_text())
        self.assertIn(derived, (REPO_ROOT / "README.md").read_text())

    def test_open_setup_forwards_config_and_quotes_paths(self):
        from notify_me.cli import setup_terminal_script

        odd = "/tmp/a b'c/claude"
        script = setup_terminal_script(
            env={"CLAUDE_CONFIG_DIR": odd, "CLAUDE_NOTIFY_ME_HOME": odd + "-state"},
            python="/usr/bin/python3",
        )
        literal = script.split("do script ", 1)[1].rsplit("\nend tell", 1)[0]
        self.assertTrue(literal.startswith('"') and literal.endswith('"'))
        command = literal[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        outer = shlex.split(command)
        self.assertEqual(outer[:2], ["bash", "-lc"])
        inner = shlex.split(outer[2])
        self.assertEqual(
            inner,
            [
                "env",
                "CLAUDE_CONFIG_DIR=" + odd,
                "CLAUDE_NOTIFY_ME_HOME=" + odd + "-state",
                "/usr/bin/python3",
                str(PLUGIN_ROOT / "scripts" / "notify_me.py"),
                "setup",
            ],
        )
        bare = setup_terminal_script(env={"CLAUDE_CONFIG_DIR": ""}, python="/usr/bin/python3")
        literal = bare.split("do script ", 1)[1].rsplit("\nend tell", 1)[0]
        inner = shlex.split(shlex.split(literal[1:-1])[2])
        self.assertEqual(
            inner[:5], ["env", "-u", "CLAUDE_CONFIG_DIR", "-u", "CLAUDE_NOTIFY_ME_HOME"]
        )
        self.assertNotIn("CLAUDE_CONFIG_DIR=", bare)

    def test_open_setup_unset_vars_clear_login_shell_exports(self):
        from notify_me.cli import setup_terminal_script

        script = setup_terminal_script(env={}, python="/usr/bin/printenv")
        literal = script.split("do script ", 1)[1].rsplit("\nend tell", 1)[0]
        inner = shlex.split(shlex.split(literal[1:-1])[2])
        probe = inner[: inner.index("/usr/bin/printenv") + 1]
        polluted = dict(os.environ, CLAUDE_CONFIG_DIR="/wrong", CLAUDE_NOTIFY_ME_HOME="/wrong")
        out = subprocess.run(probe, capture_output=True, text=True, env=polluted).stdout
        self.assertNotIn("CLAUDE_CONFIG_DIR=", out)
        self.assertNotIn("CLAUDE_NOTIFY_ME_HOME=", out)

    def test_open_setup_reports_plugin_state_and_next_step(self):
        from notify_me import cli

        with ClaudeEnv() as env:
            result = cli._open_setup({"dry_run": True})
            self.assertEqual(result["config_dir"], str(env.home))
            self.assertEqual(result["warnings"], ["plugin_not_installed", "plugin_not_enabled"])
            self.assertIn("claude plugin install", result["hint"])
            self.assertIn("doctor", result["next"])
            skill = (PLUGIN_ROOT / "skills" / "notify-me-claude" / "SKILL.md").read_text()
            for field in ("status=opened", "config_dir", "warnings", "hint", "doctor"):
                self.assertIn(field, skill)

    def test_bark_uses_official_claude_icon(self):
        self.assertIn(
            "/plugins/notify-me-claude/assets/claude-icon.png",
            DEFAULT_BARK_ICON_URL,
        )
        self.assertIn("v=1", DEFAULT_BARK_ICON_URL)
        self.assertTrue(DEFAULT_BARK_ICON_URL.startswith("https://"))
        self.assertTrue((PLUGIN_ROOT / "assets" / "claude-icon.png").is_file())

    def test_refresh_icons_posts_new_icon_to_known_groups(self):
        class FakeTransport:
            def __init__(self):
                self.payloads = []

            def send_with_retry(self, endpoint, payload, sleep=None, max_attempts=2):
                self.payloads.append(
                    {key: value for key, value in payload.items() if key != "device_key"}
                )
                return TransportResult(True, False, "accepted", 200, 1)

        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state"
            workspace = Path(raw) / "wg-easy-mac"
            workspace.mkdir()
            binding = Binding(state)
            binding.save(BarkEndpoint.parse("https://api.day.app/Abcdefgh1234"))
            transport = FakeTransport()
            deliverer = Deliverer(binding=binding, transport=transport)
            sent = deliverer.send(
                {
                    "condition": "done",
                    "item_id": "task-1",
                    "state": "finished",
                    "message": "已完成",
                    "workspace": str(workspace),
                }
            )
            self.assertEqual(sent["status"], "accepted")
            preview = deliverer.refresh_icons({"dry_run": True})
            self.assertEqual(preview["groups"], [DEFAULT_GROUP, "wg-easy-mac"])
            refreshed = deliverer.refresh_icons({})
            self.assertEqual(refreshed["status"], "accepted")
            self.assertEqual(transport.payloads[1]["group"], DEFAULT_GROUP)
            self.assertEqual(transport.payloads[2]["group"], "wg-easy-mac")
            self.assertEqual(transport.payloads[2]["icon"], DEFAULT_BARK_ICON_URL)

    def test_dry_run_keeps_workspace_identity_without_network(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state"
            workspace = Path(raw) / "workspace"
            workspace.mkdir()
            result = Deliverer(binding=Binding(state)).dispatch(
                {
                    "condition": "answer",
                    "item_id": "question-1",
                    "state": "waiting",
                    "message": "请提供必要信息",
                    "workspace": str(workspace),
                    "dry_run": True,
                }
            )
            self.assertEqual(result["status"], "dry_run")
            self.assertIn(workspace.name, result["title"])
            self.assertFalse((state / "accepted.json").exists())

    def test_send_includes_click_url_in_bark_payload(self):
        from notify_me.errors import NotifyMeError

        class FakeTransport:
            def __init__(self):
                self.payloads = []
                self.calls = 0

            def send_with_retry(self, endpoint, payload, sleep=None, max_attempts=2):
                self.calls += 1
                self.payloads.append(
                    {key: value for key, value in payload.items() if key != "device_key"}
                )
                return TransportResult(True, False, "accepted", 200, 1)

        click = "https://github.com/jattchen/notifyme/issues/187"
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state"
            workspace = Path(raw) / "workspace"
            workspace.mkdir()
            binding = Binding(state)
            binding.save(BarkEndpoint.parse("https://api.day.app/Abcdefgh1234"))
            transport = FakeTransport()
            deliverer = Deliverer(binding=binding, transport=transport)
            sent = deliverer.send(
                {
                    "condition": "action",
                    "item_id": "bug-187",
                    "state": "open",
                    "message": "点开这条推送查看 issue",
                    "workspace": str(workspace),
                    "url": click,
                }
            )
            self.assertEqual(sent["status"], "accepted")
            self.assertEqual(transport.payloads[0]["url"], click)
            with self.assertRaises(NotifyMeError) as raised:
                deliverer.send(
                    {
                        "condition": "action",
                        "item_id": "bad-url",
                        "state": "open",
                        "message": "请查看",
                        "workspace": str(workspace),
                        "url": "https://api.day.app/Abcdefgh1234",
                    }
                )
            self.assertEqual(raised.exception.code, "invalid_arguments")

    def test_send_rejects_missing_workspace(self):
        from notify_me.errors import NotifyMeError

        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(NotifyMeError) as raised:
                Deliverer(binding=Binding(Path(raw) / "state")).dispatch(
                    {
                        "condition": "answer",
                        "item_id": "question-1",
                        "state": "waiting",
                        "message": "请提供必要信息",
                        "dry_run": True,
                    }
                )
            self.assertEqual(raised.exception.code, "invalid_arguments")

    def test_legacy_op_shape_is_rejected(self):
        from notify_me.errors import NotifyMeError

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "workspace"
            workspace.mkdir()
            with self.assertRaises(NotifyMeError) as raised:
                Deliverer(binding=Binding(Path(raw) / "state")).dispatch(
                    {
                        "op": "send",
                        "condition": "answer",
                        "item_id": "question-1",
                        "state": "waiting",
                        "message": "请提供必要信息",
                        "workspace": str(workspace),
                        "dry_run": True,
                    }
                )
            self.assertEqual(raised.exception.code, "invalid_arguments")

    def test_mcp_initialize_ndjson_and_lsp(self):
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
        listed = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        env = os.environ.copy()
        with tempfile.TemporaryDirectory() as raw:
            env["CLAUDE_NOTIFY_ME_HOME"] = str(Path(raw) / "state")
            server = str(PLUGIN_ROOT / "scripts" / "mcp_server.py")

            ndjson = subprocess.Popen(
                ["python3", "-u", server],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            try:
                ndjson.stdin.write((json.dumps(init) + "\n").encode("utf-8"))
                ndjson.stdin.flush()
                line = ndjson.stdout.readline()
                reply = json.loads(line.decode("utf-8"))
                self.assertIn("instructions", reply["result"])
                ndjson.stdin.write((json.dumps(listed) + "\n").encode("utf-8"))
                ndjson.stdin.flush()
                tools = json.loads(ndjson.stdout.readline().decode("utf-8"))
                self.assertEqual(
                    [tool["name"] for tool in tools["result"]["tools"]], ["notifyme"]
                )
            finally:
                ndjson.stdin.close()
                ndjson.stdout.close()
                ndjson.stderr.close()
                ndjson.kill()
                ndjson.wait()

            body = json.dumps(init, separators=(",", ":")).encode("utf-8")
            frame = b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
            lsp = subprocess.Popen(
                ["python3", "-u", server],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            try:
                lsp.stdin.write(frame)
                lsp.stdin.flush()
                header = lsp.stdout.readline()
                self.assertTrue(header.lower().startswith(b"content-length:"))
                blank = lsp.stdout.readline()
                self.assertIn(blank, (b"\r\n", b"\n"))
                length = int(header.split(b":", 1)[1])
                payload = lsp.stdout.read(length)
                reply = json.loads(payload.decode("utf-8"))
                self.assertEqual(reply["id"], 1)
                self.assertIn("instructions", reply["result"])
            finally:
                lsp.stdin.close()
                lsp.stdout.close()
                lsp.stderr.close()
                lsp.kill()
                lsp.wait()


class ClaudeSetupTests(unittest.TestCase):
    """Drive `setup` with the terminal, prompt and Bark test stubbed out."""

    def _run_setup(self, test_status="accepted"):
        from notify_me import cli

        prompted = []

        def fake_getpass(prompt):
            prompted.append(prompt)
            return "https://api.day.app/Abcdefgh1234"

        tested = {"ok": test_status == "accepted", "status": test_status}
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), mock.patch.object(
            cli.getpass, "getpass", side_effect=fake_getpass
        ), mock.patch.object(cli.Deliverer, "test", return_value=tested), mock.patch.object(
            cli.sys, "stderr"
        ):
            try:
                return cli._setup({}), prompted
            except cli.NotifyMeError as exc:
                return exc, prompted

    def _install_plugin(self, env, enabled=True):
        plugins = env.home / "plugins"
        plugins.mkdir(exist_ok=True)
        (plugins / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {PLUGIN_ID: [{"scope": "user", "installPath": str(PLUGIN_ROOT)}]}})
        )
        if enabled:
            (env.home / "settings.json").write_text(json.dumps({"enabledPlugins": {PLUGIN_ID: True}}))

    def _assert_stopped_before_prompt(self, env, result, prompted, code, label):
        self.assertEqual(getattr(result, "code", None), code, label)
        self.assertEqual(prompted, [], label)
        self.assertFalse((env.home / "rules" / "notify-me-claude.md").is_file(), label)
        self.assertFalse(Path(os.environ["CLAUDE_NOTIFY_ME_HOME"], "binding.json").exists(), label)

    def test_unmergeable_settings_fail_before_prompt_or_writes(self):
        for raw in (
            "[]",
            "{not json",
            json.dumps({"permissions": {"allow": "x"}}),
            json.dumps({"permissions": []}),
        ):
            with ClaudeEnv() as env:
                (env.home / "settings.json").write_text(raw)
                result, prompted = self._run_setup()
                self._assert_stopped_before_prompt(env, result, prompted, "settings_unreadable", raw)
                self.assertEqual((env.home / "settings.json").read_text(), raw)

    def test_non_file_settings_fail_before_prompt(self):
        for kind in ("directory", "symlink-to-directory"):
            with ClaudeEnv() as env:
                settings = env.home / "settings.json"
                if kind == "directory":
                    settings.mkdir()
                else:
                    (env.home / "elsewhere").mkdir()
                    settings.symlink_to(env.home / "elsewhere")
                result, prompted = self._run_setup()
                self._assert_stopped_before_prompt(env, result, prompted, "settings_unreadable", kind)

    def test_non_file_rule_target_fails_before_prompt(self):
        with ClaudeEnv() as env:
            (env.home / "rules" / "notify-me-claude.md").mkdir(parents=True)
            result, prompted = self._run_setup()
            self._assert_stopped_before_prompt(env, result, prompted, "rule_unwritable", "rule dir")
            self.assertFalse((env.home / "settings.json").exists())

    def test_null_permissions_or_allow_are_treated_as_missing(self):
        for raw, kept in (
            ({"permissions": None, "model": "opus"}, {}),
            ({"permissions": {"allow": None, "deny": ["Read(.env)"]}}, {"deny": ["Read(.env)"]}),
        ):
            with ClaudeEnv() as env:
                (env.home / "settings.json").write_text(json.dumps(raw))
                result, _ = self._run_setup()
                self.assertTrue(result["ok"], result)
                data = json.loads((env.home / "settings.json").read_text())
                self.assertEqual(data["permissions"], dict(kept, allow=[TOOL]))
                self.assertTrue(permission_allowed())

    def test_empty_settings_is_treated_as_empty_object(self):
        with ClaudeEnv() as env:
            self._install_plugin(env, enabled=False)
            (env.home / "settings.json").write_text("  \n")
            result, _ = self._run_setup()
            self.assertTrue(result["ok"], result)
            data = json.loads((env.home / "settings.json").read_text())
            self.assertEqual(data["permissions"]["allow"], [TOOL])

    def test_success_writes_permission_then_rule_and_reports_config_dir(self):
        with ClaudeEnv() as env:
            self._install_plugin(env)
            result, prompted = self._run_setup()
            self.assertTrue(result["ok"], result)
            self.assertEqual(len(prompted), 1)
            self.assertEqual(result["config_dir"], str(env.home))
            self.assertEqual(result["warnings"], [])
            self.assertTrue(result["plugin_installed"])
            self.assertTrue(result["plugin_enabled"])
            self.assertTrue(permission_allowed())
            self.assertTrue(has_managed_block())
            settings = json.loads((env.home / "settings.json").read_text())
            self.assertEqual(settings["enabledPlugins"], {PLUGIN_ID: True})

    def test_missing_plugin_is_reported_as_warning(self):
        with ClaudeEnv():
            result, _ = self._run_setup()
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["warnings"], ["plugin_not_installed", "plugin_not_enabled"])
            self.assertIn("claude plugin install", result["hint"])

    def test_rule_failure_keeps_permission_and_names_the_step(self):
        from notify_me import cli

        with ClaudeEnv(), mock.patch.object(cli, "commit_agents", side_effect=OSError("disk full")):
            result, _ = self._run_setup()
            self.assertFalse(result["ok"])
            self.assertEqual(result["test"], "accepted")
            self.assertEqual(result["error"]["code"], "rule_write_failed")
            self.assertEqual(result["permission"]["permission"], TOOL)
            self.assertTrue(permission_allowed())

    def test_rejected_test_push_writes_no_host_config(self):
        with ClaudeEnv() as env:
            result, _ = self._run_setup(test_status="failed")
            self.assertEqual(result["error"]["code"], "test_not_accepted")
            self.assertFalse((env.home / "settings.json").exists())
            self.assertFalse((env.home / "rules" / "notify-me-claude.md").exists())


if __name__ == "__main__":
    unittest.main()
