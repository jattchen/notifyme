"""Claude Code-specific paths and private state helpers."""

import os
import stat
from pathlib import Path


def claude_home():
    """Return the Claude Code configuration directory without creating it."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def state_home():
    """Return Notify Me's private Claude Code state directory."""
    override = os.environ.get("CLAUDE_NOTIFY_ME_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "claude-notify-me"


def plugin_root():
    """Return the installed plugin root for diagnostics and local tooling."""
    override = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2]


def agents_path():
    return claude_home() / "rules" / "notify-me-claude.md"


def settings_path():
    return claude_home() / "settings.json"


def installed_plugins_path():
    return claude_home() / "plugins" / "installed_plugins.json"


def write_problem(dest):
    """Explain why an atomic replace of `dest` would fail, or return None.

    Checks without creating anything: an existing `dest` must be a regular
    file, and the nearest existing ancestor of its directory must be a
    directory we can write into (to create the temp file, or missing dirs).
    """
    if dest.exists() and not dest.is_file():
        return "{} 不是普通文件".format(dest)
    ancestor = dest.parent
    while not ancestor.exists():
        if ancestor.is_symlink():
            return "{} 是失效的符号链接".format(ancestor)
        if ancestor.parent == ancestor:
            break
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        return "{} 不是目录".format(ancestor)
    if not os.access(str(ancestor), os.W_OK | os.X_OK):
        return "{} 不可写".format(ancestor)
    return None


def ensure_private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, stat.S_IRWXU)


def chmod_private_file(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
