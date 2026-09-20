"""Cursor-specific paths and private state helpers."""

import os
import stat
from pathlib import Path


def cursor_home():
    """Return the Cursor configuration directory without creating it."""
    override = os.environ.get("CURSOR_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cursor"


def state_home():
    """Return Notify Me's private Cursor state directory."""
    override = os.environ.get("CURSOR_NOTIFY_ME_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "cursor-notify-me"


def plugin_root():
    """Return the installed plugin root for diagnostics and local tooling."""
    override = os.environ.get("PLUGIN_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2]


def agents_path():
    return cursor_home() / "rules" / "notify-me.mdc"


def mcp_path():
    return cursor_home() / "mcp.json"


def skill_path():
    return cursor_home() / "skills" / "notify-me-cursor" / "SKILL.md"


def ensure_private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, stat.S_IRWXU)


def chmod_private_file(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
