#!/usr/bin/env python3
"""stdio MCP server for Cursor.

Accepts NDJSON (one JSON-RPC object per line) and Content-Length/LSP frames.
The first non-empty stdin message selects the reply framing for the process.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from notify_me.deliver import TOOL_NAME, TOOL_NAMES, TOOL_SCHEMA, Deliverer  # noqa: E402
from notify_me.errors import NotifyMeError  # noqa: E402


PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_PARSE_ERROR = object()


def advertised_tool_name(argv=None):
    tokens = sys.argv[1:] if argv is None else list(argv)
    name = TOOL_NAME
    index = 0
    while index < len(tokens):
        if tokens[index] == "--name" and index + 1 < len(tokens):
            name = tokens[index + 1]
            index += 2
            continue
        index += 1
    if name not in TOOL_NAMES:
        return TOOL_NAME
    return name


def _tool_schema(name):
    schema = dict(TOOL_SCHEMA)
    schema["name"] = name
    return schema


class Transport:
    def __init__(self):
        self.mode = None

    def read_message(self):
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            if line.lower().startswith(b"content-length:"):
                self.mode = "lsp"
                try:
                    length = int(line.split(b":", 1)[1])
                except ValueError:
                    return _PARSE_ERROR
                if length < 0:
                    return _PARSE_ERROR
                while True:
                    header = sys.stdin.buffer.readline()
                    if header in (b"\r\n", b"\n"):
                        break
                    if not header:
                        return None
                body = sys.stdin.buffer.read(length)
                if len(body) < length:
                    return None
                try:
                    return json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return _PARSE_ERROR
            stripped = line.strip()
            if not stripped:
                continue
            self.mode = "ndjson"
            try:
                return json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _PARSE_ERROR

    def write_message(self, payload):
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if self.mode == "lsp":
            header = "Content-Length: {}\r\n\r\n".format(len(encoded)).encode("ascii")
            sys.stdout.buffer.write(header + encoded)
        else:
            sys.stdout.buffer.write(encoded + b"\n")
        sys.stdout.buffer.flush()


def _write_rpc_error(transport, code, message, msg_id=None):
    transport.write_message(
        {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }
    )


def _result_text(data, is_error=False):
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "isError": is_error,
    }


def _negotiate_version(params):
    requested = (params or {}).get("protocolVersion")
    if requested in PROTOCOL_VERSIONS:
        return requested
    return PROTOCOL_VERSIONS[0]


def _object_or_invalid(value):
    """Missing/None becomes {}. {} stays valid. Falsey non-dicts stay invalid."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return None


def serve(deliverer=None, tool_name=None, transport=None):
    service = deliverer
    tool_name = TOOL_NAME if tool_name is None else tool_name
    transport = Transport() if transport is None else transport
    while True:
        message = transport.read_message()
        if message is None:
            return
        if message is _PARSE_ERROR:
            _write_rpc_error(transport, -32700, "Parse error")
            continue
        if not isinstance(message, dict):
            _write_rpc_error(transport, -32600, "Invalid Request")
            continue
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            params = _object_or_invalid(message.get("params"))
            if params is None:
                _write_rpc_error(transport, -32602, "Invalid params", msg_id)
                continue
            version = _negotiate_version(params)
            transport.write_message(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": tool_name, "version": "1.0.0"},
                        "instructions": (
                            "Notify Me 只由当前顶层主 Agent 使用。"
                            "工具参数是扁平对象，必须包含 condition、item_id、state、message、workspace；"
                            "有可点开的网页时另传 url（http/https，不是 Bark 设备地址）。"
                            "condition 只能是 answer、auth、action、severe-risk 或 done。"
                            "不要传入 op 或 Bark 设备地址。"
                            "仅在等待用户、授权、外部操作、严重不可逆风险或整件任务完成时发送；"
                            "普通进度和中间步骤不发送。workspace 必须是当前项目绝对路径，"
                            "并以 accepted 才能报告服务已接受。"
                        ),
                    },
                }
            )
            continue
        if method == "notifications/initialized" or msg_id is None:
            continue
        if service is None:
            service = Deliverer()
        if method == "tools/list":
            transport.write_message(
                {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [_tool_schema(tool_name)]}}
            )
            continue
        if method == "tools/call":
            params = _object_or_invalid(message.get("params"))
            if params is None:
                _write_rpc_error(transport, -32602, "Invalid params", msg_id)
                continue
            name = params.get("name")
            arguments = _object_or_invalid(params.get("arguments"))
            if arguments is None:
                _write_rpc_error(transport, -32602, "Invalid params", msg_id)
                continue
            if name != tool_name:
                transport.write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": _result_text(
                            {"ok": False, "error": {"code": "unknown_tool", "message": name}},
                            True,
                        ),
                    }
                )
                continue
            try:
                result = service.dispatch(arguments, os.environ)
                is_error = not result.get("ok", False)
                transport.write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": _result_text(result, is_error),
                    }
                )
            except NotifyMeError as exc:
                transport.write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": _result_text(exc.as_dict(), True),
                    }
                )
            except Exception as exc:
                transport.write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": _result_text(
                            {"ok": False, "error": {"code": "internal_error", "message": str(exc)}},
                            True,
                        ),
                    }
                )
            continue
        if method == "ping":
            transport.write_message({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            continue
        transport.write_message(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )


if __name__ == "__main__":
    try:
        serve(tool_name=advertised_tool_name())
    except KeyboardInterrupt:
        raise SystemExit(0)
