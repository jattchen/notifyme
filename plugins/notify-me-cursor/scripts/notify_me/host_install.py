"""Write Cursor MCP config and copy the user-invoked skill."""

import json
import os
import tempfile

from .errors import NotifyMeError
from .paths import mcp_path, plugin_root, skill_path


MCP_SERVER_NAME = "notifyme_cursor"


def mcp_server_path():
    return plugin_root() / "scripts" / "mcp_server.py"


def skill_source():
    return plugin_root() / "skills" / "notify-me-cursor" / "SKILL.md"


def mcp_entry():
    return {
        "command": "python3",
        "args": ["-u", str(mcp_server_path())],
    }


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".notify-me.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def commit_mcp():
    path = mcp_path()
    current = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NotifyMeError("mcp_unreadable", "无法读取 Cursor mcp.json") from exc
        if not isinstance(loaded, dict):
            raise NotifyMeError("mcp_unreadable", "Cursor mcp.json 不是对象")
        current = loaded
    servers = current.get("mcpServers")
    if servers is None:
        servers = {}
        current["mcpServers"] = servers
    elif not isinstance(servers, dict):
        raise NotifyMeError("mcp_unreadable", "mcpServers 不是对象")
    action = "replaced" if MCP_SERVER_NAME in servers else "appended"
    servers[MCP_SERVER_NAME] = mcp_entry()
    _atomic_write(path, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    return {
        "ok": True,
        "status": "committed",
        "target": str(path),
        "action": action,
        "server": MCP_SERVER_NAME,
    }


def mcp_points_at_plugin():
    path = mcp_path()
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return False
    entry = servers.get(MCP_SERVER_NAME)
    if not isinstance(entry, dict):
        return False
    args = entry.get("args") or []
    needle = str(mcp_server_path())
    return any(str(item) == needle for item in args)


def commit_skill():
    source = skill_source()
    if not source.is_file():
        raise NotifyMeError("skill_missing", "插件内未找到 Skill")
    dest = skill_path()
    text = source.read_text(encoding="utf-8").replace("<plugin-root>", str(plugin_root()))
    _atomic_write(dest, text)
    return {"ok": True, "status": "committed", "target": str(dest)}
