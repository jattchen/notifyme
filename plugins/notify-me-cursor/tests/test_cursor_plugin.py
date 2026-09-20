import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
from notify_me.host_install import MCP_SERVER_NAME, commit_mcp, mcp_points_at_plugin  # noqa: E402


class CursorPackageTests(unittest.TestCase):
    def test_plugin_identity_is_cursor(self):
        portable = json.loads((PLUGIN_ROOT / "plugin.json").read_text())
        self.assertEqual(portable["name"], "notify-me-cursor")
        self.assertEqual(portable["version"], "0.1.0")
        self.assertNotIn("extensions", portable)

        mcp = json.loads((PLUGIN_ROOT / "mcp.json").read_text())
        server = mcp["mcpServers"]["notifyme_cursor"]
        self.assertEqual(server["command"], "python3")
        self.assertEqual(server["args"][-1], "${PLUGIN_ROOT}/scripts/mcp_server.py")

        skill = (PLUGIN_ROOT / "skills" / "notify-me-cursor" / "SKILL.md").read_text()
        self.assertIn("disable-model-invocation: true", skill)
        self.assertIn("notify-me-cursor", skill)
        self.assertNotIn("mcp__notifyme_codex__notifyme", skill)

    def test_cursor_schema_is_flat_and_explicit(self):
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

    def test_cursor_rule_is_idempotent_and_uses_cursor_tool(self):
        from notify_me import agents_rule

        original = agents_rule.agents_path
        try:
            with tempfile.TemporaryDirectory() as raw:
                target = Path(raw) / "notify-me.mdc"
                agents_rule.agents_path = lambda: target
                first = agents_rule.commit()
                second = agents_rule.commit()
                text = target.read_text()
                self.assertEqual(first["action"], "appended")
                self.assertEqual(second["action"], "replaced")
                self.assertEqual(text, managed_file())
                self.assertEqual(text.count(MANAGED_START), 1)
                self.assertEqual(text.count(MANAGED_END), 1)
                self.assertIn("alwaysApply: true", text)
                self.assertIn("notifyme_cursor", text)
                self.assertTrue(has_managed_block(text))
                for field in ("condition", "item_id", "state", "message", "workspace"):
                    self.assertIn(field, agents_rule.managed_block())
                self.assertIn("不得传 op", agents_rule.managed_block())
        finally:
            agents_rule.agents_path = original

    def test_mcp_json_merges_without_clobbering_other_servers(self):
        from notify_me import host_install, paths

        original_mcp = paths.mcp_path
        original_plugin = paths.plugin_root
        try:
            with tempfile.TemporaryDirectory() as raw:
                home = Path(raw) / "cursor"
                home.mkdir()
                existing = home / "mcp.json"
                existing.write_text(
                    json.dumps(
                        {
                            "mcpServers": {
                                "other": {"command": "echo", "args": ["ok"]}
                            }
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                paths.mcp_path = lambda: existing
                host_install.mcp_path = lambda: existing
                paths.plugin_root = lambda: PLUGIN_ROOT
                host_install.plugin_root = lambda: PLUGIN_ROOT
                first = commit_mcp()
                second = commit_mcp()
                data = json.loads(existing.read_text(encoding="utf-8"))
                self.assertEqual(first["action"], "appended")
                self.assertEqual(second["action"], "replaced")
                self.assertEqual(data["mcpServers"]["other"]["command"], "echo")
                self.assertEqual(
                    data["mcpServers"][MCP_SERVER_NAME]["args"][-1],
                    str(PLUGIN_ROOT / "scripts" / "mcp_server.py"),
                )
                self.assertTrue(mcp_points_at_plugin())
        finally:
            paths.mcp_path = original_mcp
            paths.plugin_root = original_plugin
            host_install.mcp_path = original_mcp
            host_install.plugin_root = original_plugin

    def test_bark_uses_official_cursor_icon(self):
        self.assertIn(
            "/plugins/notify-me-cursor/assets/cursor-icon.png",
            DEFAULT_BARK_ICON_URL,
        )
        self.assertIn("v=1", DEFAULT_BARK_ICON_URL)
        self.assertTrue(DEFAULT_BARK_ICON_URL.startswith("https://"))
        self.assertTrue((PLUGIN_ROOT / "assets" / "cursor-icon.png").is_file())

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
            env["CURSOR_NOTIFY_ME_HOME"] = str(Path(raw) / "state")
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


if __name__ == "__main__":
    unittest.main()
