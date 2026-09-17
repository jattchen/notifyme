import json
import os
import select
import tempfile
import time
import unittest
from pathlib import Path
import subprocess

SERVER = Path(__file__).resolve().parents[1] / "scripts" / "mcp_server.py"


def _ndjson_session(home, extra_args=None):
    env = os.environ.copy()
    env["GROK_NOTIFY_ME_HOME"] = home
    command = ["python3", "-u", str(SERVER)]
    if extra_args:
        command.extend(extra_args)
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return proc


def _send_line(proc, obj):
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
    proc.stdin.write(raw)
    proc.stdin.flush()


def _read_line(proc, timeout=2.0):
    start = time.time()
    buf = b""
    while time.time() - start < timeout:
        ready, _, _ = select.select([proc.stdout], [], [], 0.05)
        if not ready:
            continue
        chunk = proc.stdout.read1(4096)
        if not chunk:
            break
        buf += chunk
        if b"\n" in buf:
            line, _rest = buf.split(b"\n", 1)
            return json.loads(line.decode("utf-8"))
    raise AssertionError("no NDJSON response: %r poll=%s" % (buf, proc.poll()))


def _write_raw(proc, raw):
    proc.stdin.write(raw)
    proc.stdin.flush()


def _alive(proc):
    return proc.poll() is None


def _assert_rpc_error(test, reply, codes):
    test.assertIn("error", reply)
    test.assertIn(reply["error"]["code"], codes)


def _assert_ping(test, proc, msg_id):
    _send_line(proc, {"jsonrpc": "2.0", "id": msg_id, "method": "ping"})
    reply = _read_line(proc)
    test.assertTrue(_alive(proc))
    test.assertEqual(reply.get("id"), msg_id)
    test.assertEqual(reply.get("result"), {})
    return reply


def _handshake_and_unsupported_call(proc, call_id=20):
    _send_line(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
    )
    init = _read_line(proc)
    _send_line(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    _send_line(
        proc,
        {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": "notify_me", "arguments": {"op": "status"}},
        },
    )
    reply = _read_line(proc)
    return init, reply


class McpHandshakeTests(unittest.TestCase):
    def test_initialize_and_minimal_schema(self):
        home = tempfile.mkdtemp(prefix="notify-me-mcp-")
        proc = _ndjson_session(home)
        try:
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "repro", "version": "0"},
                    },
                },
            )
            message = _read_line(proc)
            self.assertEqual(message["id"], 1)
            self.assertEqual(message["result"]["serverInfo"]["name"], "notify_me")
            _send_line(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send_line(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            listed = _read_line(proc)
            tools = listed["result"]["tools"]
            self.assertEqual(len(tools), 1)
            tool = tools[0]
            self.assertEqual(tool["name"], "notify_me")
            props = tool["inputSchema"]["properties"]
            self.assertEqual(set(props), {"op", "condition", "item_id", "state", "message", "dry_run"})
            self.assertEqual(props["op"]["enum"], ["send", "test"])
            self.assertEqual(
                props["condition"]["enum"],
                ["answer", "auth", "action", "severe-risk", "done"],
            )
            dumped = json.dumps(tool)
            self.assertNotIn("subscribe", dumped)
            self.assertNotIn("bark_url", dumped)
            self.assertNotIn("fulfillment_id", dumped)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait(timeout=2)

    def test_unknown_op_is_error(self):
        home = tempfile.mkdtemp(prefix="notify-me-mcp-")
        proc = _ndjson_session(home)
        try:
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
                },
            )
            _read_line(proc)
            _send_line(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "notify_me", "arguments": {"op": "status"}},
                },
            )
            reply = _read_line(proc)
            payload = json.loads(reply["result"]["content"][0]["text"])
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "unsupported_command")
            self.assertTrue(reply["result"]["isError"])
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait(timeout=2)

    def test_name_flag_advertises_notifyme_only(self):
        home = tempfile.mkdtemp(prefix="notify-me-mcp-")
        proc = _ndjson_session(home, ["--name", "notifyme"])
        try:
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "repro", "version": "0"},
                    },
                },
            )
            message = _read_line(proc)
            self.assertEqual(message["result"]["serverInfo"]["name"], "notifyme")
            _send_line(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send_line(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            listed = _read_line(proc)
            tools = listed["result"]["tools"]
            self.assertEqual([tool["name"] for tool in tools], ["notifyme"])
            props = tools[0]["inputSchema"]["properties"]
            self.assertEqual(set(props), {"op", "condition", "item_id", "state", "message", "dry_run"})
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait(timeout=2)

    def test_notifyme_tool_call_matches_notify_me(self):
        home = tempfile.mkdtemp(prefix="notify-me-mcp-")
        proc = _ndjson_session(home, ["--name", "notifyme"])
        try:
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                },
            )
            _read_line(proc)
            _send_line(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "notifyme", "arguments": {"op": "status"}},
                },
            )
            reply = _read_line(proc)
            payload = json.loads(reply["result"]["content"][0]["text"])
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "unsupported_command")
            self.assertTrue(reply["result"]["isError"])
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait(timeout=2)

    def test_illegal_and_non_object_json_keep_serving(self):
        home = tempfile.mkdtemp(prefix="notify-me-mcp-")
        proc = _ndjson_session(home)
        try:
            _write_raw(proc, b"this is not json\n")
            _assert_rpc_error(self, _read_line(proc), (-32700, -32600))
            self.assertTrue(_alive(proc), "illegal JSON killed the MCP process")
            _assert_ping(self, proc, 1)

            for raw, label in (
                (b"[]\n", "empty array"),
                (b"true\n", "bare true"),
                (b'[{"jsonrpc":"2.0","id":3,"method":"ping"}]\n', "batch array"),
            ):
                _write_raw(proc, raw)
                _assert_rpc_error(self, _read_line(proc), (-32700, -32600))
                self.assertTrue(_alive(proc), "%s killed the MCP process" % label)
                _assert_ping(self, proc, 2)

            _init, call = _handshake_and_unsupported_call(proc)
            self.assertTrue(_alive(proc))
            payload = json.loads(call["result"]["content"][0]["text"])
            self.assertEqual(payload["error"]["code"], "unsupported_command")
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait(timeout=2)

    def test_content_length_garbage_does_not_kill_or_hang(self):
        home = tempfile.mkdtemp(prefix="notify-me-mcp-")
        proc = _ndjson_session(home)
        try:
            _write_raw(proc, b"Content-Length: abc\r\n\r\n")
            _assert_rpc_error(self, _read_line(proc), (-32700, -32600))
            self.assertTrue(_alive(proc), "non-numeric Content-Length killed the process")
            _assert_ping(self, proc, 1)

            _write_raw(proc, b"Content-Length: 999999\r\n\r\n")
            _assert_rpc_error(self, _read_line(proc), (-32700, -32600))
            self.assertTrue(_alive(proc), "oversized Content-Length killed the process")
            _assert_ping(self, proc, 2)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait(timeout=2)

    def test_non_object_initialize_params_keep_serving(self):
        home = tempfile.mkdtemp(prefix="notify-me-mcp-")
        proc = _ndjson_session(home)
        try:
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": "not-an-object",
                },
            )
            reply = _read_line(proc)
            _assert_rpc_error(self, reply, (-32602, -32600))
            self.assertEqual(reply.get("id"), 1)
            self.assertTrue(_alive(proc), "non-object initialize params killed the MCP process")
            _assert_ping(self, proc, 2)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait(timeout=2)

    def test_non_object_tools_call_params_keep_serving(self):
        home = tempfile.mkdtemp(prefix="notify-me-mcp-")
        proc = _ndjson_session(home)
        try:
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                },
            )
            _read_line(proc)
            _send_line(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": "not-an-object",
                },
            )
            reply = _read_line(proc)
            _assert_rpc_error(self, reply, (-32602, -32600))
            self.assertEqual(reply.get("id"), 2)
            self.assertTrue(_alive(proc), "non-object tools/call params killed the MCP process")
            _assert_ping(self, proc, 3)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait(timeout=2)

    def test_non_object_tools_call_arguments_keep_serving(self):
        home = tempfile.mkdtemp(prefix="notify-me-mcp-")
        proc = _ndjson_session(home)
        try:
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                },
            )
            _read_line(proc)
            _send_line(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "notify_me", "arguments": "not-an-object"},
                },
            )
            reply = _read_line(proc)
            _assert_rpc_error(self, reply, (-32602,))
            self.assertEqual(reply.get("id"), 2)
            self.assertNotIn("result", reply)
            self.assertTrue(_alive(proc), "non-object tools/call arguments killed the MCP process")
            _assert_ping(self, proc, 3)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait(timeout=2)

    def test_consecutive_blank_lines_do_not_recurse(self):
        home = tempfile.mkdtemp(prefix="notify-me-mcp-")
        proc = _ndjson_session(home)
        try:
            _write_raw(proc, b"\n" * 1500)
            _assert_ping(self, proc, 1)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait(timeout=2)
