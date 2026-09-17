#!/usr/bin/env python3
"""stdio MCP server. Grok's MCP client is rmcp and speaks NDJSON on stdio.

Content-Length/LSP frames are not a supported transport. A header line is
just another NDJSON line (usually a Parse error) so a bad or oversized
length cannot hang or kill the process. Replies are always NDJSON.
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


_PARSE_ERROR = object()


def _read_message():
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        try:
            return json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _PARSE_ERROR


def _write_message(payload):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def _write_rpc_error(code, message, msg_id=None):
    _write_message(
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


def serve(deliverer=None, tool_name=None):
    service = deliverer
    tool_name = TOOL_NAME if tool_name is None else tool_name
    while True:
        message = _read_message()
        if message is None:
            return
        if message is _PARSE_ERROR:
            _write_rpc_error(-32700, "Parse error")
            continue
        if not isinstance(message, dict):
            _write_rpc_error(-32600, "Invalid Request")
            continue
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            params = _object_or_invalid(message.get("params"))
            if params is None:
                _write_rpc_error(-32602, "Invalid params", msg_id)
                continue
            version = _negotiate_version(params)
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": tool_name, "version": "1.0.0"},
                    },
                }
            )
            continue
        if method == "notifications/initialized" or msg_id is None:
            continue
        if service is None:
            service = Deliverer()
        if method == "tools/list":
            _write_message(
                {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [_tool_schema(tool_name)]}}
            )
            continue
        if method == "tools/call":
            params = _object_or_invalid(message.get("params"))
            if params is None:
                _write_rpc_error(-32602, "Invalid params", msg_id)
                continue
            name = params.get("name")
            arguments = _object_or_invalid(params.get("arguments"))
            if arguments is None:
                _write_rpc_error(-32602, "Invalid params", msg_id)
                continue
            if name != tool_name:
                _write_message(
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
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": _result_text(result, is_error),
                    }
                )
            except NotifyMeError as exc:
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": _result_text(exc.as_dict(), True),
                    }
                )
            except Exception as exc:
                _write_message(
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
            _write_message({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            continue
        _write_message(
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
