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
from notify_me.deliver import Deliverer  # noqa: E402


class CodexPackageTests(unittest.TestCase):
    def test_portable_and_compatibility_manifests_use_new_identity(self):
        portable = json.loads((PLUGIN_ROOT / "plugin.json").read_text())
        compatibility = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text()
        )
        self.assertEqual(portable["name"], "notify-me-codex")
        self.assertEqual(compatibility["name"], portable["name"])
        self.assertEqual(compatibility["mcpServers"], "./.mcp.json")

        mcp = json.loads((PLUGIN_ROOT / "mcp.json").read_text())
        server = mcp["mcpServers"]["notifyme_codex"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["args"][-1], "${PLUGIN_ROOT}/scripts/mcp_server.py")
        self.assertEqual(server["cwd"], "${PLUGIN_ROOT}")

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
        finally:
            agents_rule.agents_path = original

    def test_dry_run_keeps_workspace_identity_without_network(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state"
            workspace = Path(raw) / "workspace"
            workspace.mkdir()
            result = Deliverer(binding=Binding(state)).dispatch(
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
            self.assertEqual(result["status"], "dry_run")
            self.assertIn(workspace.name, result["title"])
            self.assertFalse((state / "accepted.json").exists())

    def test_send_rejects_missing_workspace(self):
        from notify_me.errors import NotifyMeError

        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(NotifyMeError) as raised:
                Deliverer(binding=Binding(Path(raw) / "state")).dispatch(
                    {
                        "op": "send",
                        "condition": "answer",
                        "item_id": "question-1",
                        "state": "waiting",
                        "message": "请提供必要信息",
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
                self.assertEqual(
                    [tool["name"] for tool in listed["result"]["tools"]], ["notifyme"]
                )
            finally:
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main()
