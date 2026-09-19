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

from notify_me.agents_rule import MANAGED_START, MANAGED_END, has_managed_block  # noqa: E402
from notify_me.binding import Binding  # noqa: E402
from notify_me.bark import BarkEndpoint, TransportResult  # noqa: E402
from notify_me.deliver import DEFAULT_BARK_ICON_URL, DEFAULT_GROUP, Deliverer, TOOL_SCHEMA  # noqa: E402


class CodexPackageTests(unittest.TestCase):
    def test_portable_and_compatibility_manifests_use_new_identity(self):
        portable = json.loads((PLUGIN_ROOT / "plugin.json").read_text())
        compatibility = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text()
        )
        self.assertEqual(portable["name"], "notify-me-codex")
        self.assertEqual(portable["version"], "0.1.1")
        self.assertEqual(compatibility["name"], portable["name"])
        self.assertEqual(compatibility["version"], portable["version"])
        self.assertEqual(compatibility["mcpServers"], "./.mcp.json")

        mcp = json.loads((PLUGIN_ROOT / "mcp.json").read_text())
        server = mcp["mcpServers"]["notifyme_codex"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["args"][-1], "${PLUGIN_ROOT}/scripts/mcp_server.py")
        self.assertEqual(server["cwd"], "${PLUGIN_ROOT}")

    def test_codex_schema_is_flat_and_explicit(self):
        schema = TOOL_SCHEMA["inputSchema"]
        self.assertEqual(
            set(schema["properties"]),
            {"condition", "item_id", "state", "message", "dry_run", "workspace"},
        )
        self.assertNotIn("op", schema["properties"])
        self.assertEqual(
            schema["required"],
            ["condition", "item_id", "state", "message", "workspace"],
        )
        for combinator in ("allOf", "anyOf", "oneOf", "if", "then"):
            self.assertNotIn(combinator, schema)
        self.assertIs(schema["additionalProperties"], False)

    def test_skill_requires_explicit_invocation(self):
        policy = (
            PLUGIN_ROOT
            / "skills"
            / "notify-me-codex"
            / "agents"
            / "openai.yaml"
        ).read_text()
        self.assertIn("allow_implicit_invocation: false", policy)

    def test_codex_rule_is_idempotent_and_uses_codex_tool(self):
        from notify_me import agents_rule

        original = agents_rule.agents_path
        try:
            with tempfile.TemporaryDirectory() as raw:
                target = Path(raw) / "AGENTS.md"
                agents_rule.agents_path = lambda: target
                first = agents_rule.commit()
                second = agents_rule.commit()
                text = target.read_text()
                self.assertEqual(first["action"], "appended")
                self.assertEqual(second["action"], "replaced")
                self.assertEqual(text.count(MANAGED_START), 1)
                self.assertEqual(text.count(MANAGED_END), 1)
                self.assertIn("mcp__notifyme_codex__notifyme", text)
                for field in ("condition", "item_id", "state", "message", "workspace"):
                    self.assertIn(field, agents_rule.managed_block())
                self.assertIn("不得传 op", agents_rule.managed_block())
        finally:
            agents_rule.agents_path = original

    def test_bark_uses_official_codex_icon(self):
        self.assertIn(
            "/plugins/notify-me-codex/assets/codex-icon.png",
            DEFAULT_BARK_ICON_URL,
        )
        self.assertIn("v=2", DEFAULT_BARK_ICON_URL)
        self.assertTrue(DEFAULT_BARK_ICON_URL.startswith("https://"))
        self.assertTrue((PLUGIN_ROOT / "assets" / "codex-icon.png").is_file())

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

    def test_send_rejects_nonexistent_workspace(self):
        from notify_me.errors import NotifyMeError

        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(NotifyMeError) as raised:
                Deliverer(binding=Binding(Path(raw) / "state")).dispatch(
                    {
                        "condition": "answer",
                        "item_id": "question-1",
                        "state": "waiting",
                        "message": "请提供必要信息",
                        "workspace": str(Path(raw) / "missing-workspace"),
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

    def test_mcp_initialize_advertises_instructions_and_single_tool(self):
        env = os.environ.copy()
        with tempfile.TemporaryDirectory() as raw:
            env["CODEX_NOTIFY_ME_HOME"] = str(Path(raw) / "state")
            proc = subprocess.Popen(
                ["python3", "-u", str(PLUGIN_ROOT / "scripts" / "mcp_server.py")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
            )
            try:
                proc.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26"},
                        }
                    )
                    + "\n"
                )
                proc.stdin.flush()
                init = json.loads(proc.stdout.readline())
                proc.stdin.write(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
                    )
                    + "\n"
                )
                proc.stdin.flush()
                listed = json.loads(proc.stdout.readline())
                self.assertIn("instructions", init["result"])
                instructions = init["result"]["instructions"]
                for field in ("condition", "item_id", "state", "message", "workspace"):
                    self.assertIn(field, instructions)
                self.assertEqual(
                    [tool["name"] for tool in listed["result"]["tools"]], ["notifyme"]
                )
                listed_schema = listed["result"]["tools"][0]["inputSchema"]
                self.assertEqual(listed_schema["required"], TOOL_SCHEMA["inputSchema"]["required"])
                self.assertNotIn("allOf", listed_schema)
                proc.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {
                                "name": "notifyme",
                                "arguments": {
                                    "condition": "answer",
                                    "item_id": "question-1",
                                    "state": "waiting",
                                    "message": "请提供必要信息",
                                    "workspace": raw,
                                    "dry_run": True,
                                },
                            },
                        }
                    )
                    + "\n"
                )
                proc.stdin.flush()
                called = json.loads(proc.stdout.readline())
                payload = json.loads(called["result"]["content"][0]["text"])
                self.assertEqual(payload["status"], "dry_run")
                self.assertEqual(payload["item_id"], "question-1")
            finally:
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main()
