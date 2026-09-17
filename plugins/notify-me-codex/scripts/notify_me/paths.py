"""Codex-specific paths and private state helpers."""

import os
import stat
from pathlib import Path


def codex_home():
    """Return the Codex configuration directory without creating it."""
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def state_home():
    """Return Notify Me's private Codex state directory."""
    override = os.environ.get("CODEX_NOTIFY_ME_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "codex-notify-me"


def plugin_root():
    """Return the installed plugin root for diagnostics and local tooling."""
    override = os.environ.get("PLUGIN_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2]


def agents_path():
    return codex_home() / "AGENTS.md"


def ensure_private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, stat.S_IRWXU)


def chmod_private_file(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
