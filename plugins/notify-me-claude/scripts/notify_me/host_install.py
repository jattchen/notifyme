"""Pre-approve the notify tool in Claude Code settings and inspect the plugin install."""

import json
import os
import stat
import tempfile

from .agents_rule import TOOL_PERMISSION
from .errors import NotifyMeError
from .paths import installed_plugins_path, settings_path, write_problem


PLUGIN_ID = "notify-me-claude@notifyme"


def _write_destination(path):
    if not path.is_symlink():
        return path
    try:
        dest = path.resolve()
    except (OSError, RuntimeError) as exc:
        # Python < 3.13 raises RuntimeError for a symlink loop.
        raise NotifyMeError("settings_unreadable", "settings.json 是符号链接，但无法解析目标") from exc
    if dest.exists() and dest.is_dir():
        raise NotifyMeError("settings_unreadable", "settings.json 是符号链接，不能指向目录")
    return dest


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    if path.is_file():
        mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".notify-me.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644 if mode is None else mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json_object(path, code, label):
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        loaded = json.loads(text)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NotifyMeError(code, "无法读取 {}".format(label)) from exc
    if not isinstance(loaded, dict):
        raise NotifyMeError(code, "{} 不是对象".format(label))
    return loaded


def check_settings():
    """Load settings.json and fail before any write if the allow list can't be merged.

    Returns the settings with `permissions.allow` present as a list; a null
    `permissions` or `allow` is treated like a missing one.
    """
    path = settings_path()
    problem = write_problem(_write_destination(path))
    if problem:
        raise NotifyMeError("settings_unreadable", "Claude Code settings.json 无法写入：{}".format(problem))
    current = _read_json_object(path, "settings_unreadable", "Claude Code settings.json")
    if current is None:
        current = {}
    permissions = current.get("permissions")
    if permissions is None:
        permissions = {}
    elif not isinstance(permissions, dict):
        raise NotifyMeError("settings_unreadable", "permissions 不是对象")
    allow = permissions.get("allow")
    if allow is None:
        allow = []
    elif not isinstance(allow, list):
        raise NotifyMeError("settings_unreadable", "permissions.allow 不是数组")
    permissions["allow"] = allow
    current["permissions"] = permissions
    return current


def commit_permission():
    path = settings_path()
    current = check_settings()
    allow = current["permissions"]["allow"]
    if TOOL_PERMISSION in allow:
        action = "unchanged"
    else:
        allow.append(TOOL_PERMISSION)
        action = "appended"
        _atomic_write(
            _write_destination(path),
            json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        )
    return {
        "ok": True,
        "status": "committed",
        "target": str(path),
        "action": action,
        "permission": TOOL_PERMISSION,
    }


def _settings_or_empty():
    try:
        return _read_json_object(settings_path(), "settings_unreadable", "settings.json") or {}
    except NotifyMeError:
        return {}


_SERVER_RULE = TOOL_PERMISSION.rsplit("__", 1)[0]
_MATCHING_RULES = (TOOL_PERMISSION, _SERVER_RULE, _SERVER_RULE + "__*")


def _matching_rules(kind):
    permissions = _settings_or_empty().get("permissions")
    if not isinstance(permissions, dict):
        return []
    rules = permissions.get(kind)
    if not isinstance(rules, list):
        return []
    return [rule for rule in rules if rule in _MATCHING_RULES]


def permission_blocked():
    """Return deny/ask rules that override the allow entry for the notify tool."""
    return _matching_rules("deny") + _matching_rules("ask")


def permission_allowed():
    return bool(_matching_rules("allow")) and not permission_blocked()


def plugin_enabled():
    enabled = _settings_or_empty().get("enabledPlugins")
    return isinstance(enabled, dict) and enabled.get(PLUGIN_ID) is True


def plugin_installs():
    try:
        data = _read_json_object(
            installed_plugins_path(), "plugins_unreadable", "installed_plugins.json"
        )
    except NotifyMeError:
        return []
    plugins = (data or {}).get("plugins")
    if not isinstance(plugins, dict):
        return []
    entries = plugins.get(PLUGIN_ID)
    if not isinstance(entries, list):
        return []
    return [
        entry.get("installPath")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("installPath"), str)
    ]
