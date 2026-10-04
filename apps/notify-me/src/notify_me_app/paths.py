# -*- coding: utf-8 -*-
"""Explicit application paths. version --json never imports a call into here."""

import os
import stat
import sys
from pathlib import Path

from .errors import NotifyMeError


_PLATFORM_LINK_ALIASES = {
    Path("/var"),
    Path("/tmp"),
    Path("/private/var"),
    Path("/private/tmp"),
}
_BANNED_PREFIXES = (
    "/Users/mac/.local/bin/notify-me",
    "/Users/mac/Library/Application Support/notify-me",
)


class AppPaths(object):
    def __init__(self, config_dir, launcher):
        self.config_dir = Path(config_dir) if config_dir is not None else None
        self.launcher = Path(launcher) if launcher is not None else None
        if self.config_dir is not None:
            self.state_db = self.config_dir / "state.sqlite3"
            self.dotenv = self.config_dir / ".env"
            self.marker = self.config_dir / "install-recovery.json"
            self.backup = self.config_dir / "state.sqlite3.pre-schema9"
            self.lock = self.config_dir / "install.lock"
        else:
            self.state_db = None
            self.dotenv = None
            self.marker = None
            self.backup = None
            self.lock = None


def forbid_host_path(path, env):
    """Refuse the real host install when the test guard is on. No stat."""

    if env is None or env.get("NOTIFY_ME_FORBID_HOST_PATHS") != "1":
        return
    text = os.path.abspath(os.path.expanduser(str(path)))
    for banned in _BANNED_PREFIXES:
        if text == banned or text.startswith(banned + os.sep):
            raise NotifyMeError("host_path_forbidden")


def reject_symlink_components(path, code):
    for component in (path,) + tuple(path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise NotifyMeError(code)
        if stat.S_ISLNK(info.st_mode) and component not in _PLATFORM_LINK_ALIASES:
            raise NotifyMeError(code)


def default_config_dir(env):
    configured = env.get("NOTIFY_ME_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    if env.get("NOTIFY_ME_FORBID_HOST_PATHS") == "1":
        raise NotifyMeError("host_path_forbidden")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "notify-me"
    if env.get("APPDATA") and os.name == "nt":
        return Path(env["APPDATA"]) / "notify-me"
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "notify-me"


def default_launcher(env, config_dir):
    configured = env.get("NOTIFY_ME_LAUNCHER_PATH")
    if configured:
        return Path(configured).expanduser()
    if env.get("NOTIFY_ME_CONFIG_DIR"):
        return Path(config_dir) / "bin" / "notify-me"
    if env.get("NOTIFY_ME_FORBID_HOST_PATHS") == "1":
        raise NotifyMeError("host_path_forbidden")
    return Path.home() / ".local" / "bin" / "notify-me"


def resolve_paths(env, config_dir=None, launcher=None, require_config=True, require_launcher=False):
    """Resolve paths for a command that is allowed to look at the filesystem."""

    if config_dir:
        config = Path(config_dir).expanduser()
    elif require_config:
        config = default_config_dir(env)
    else:
        config = None
    if config is not None:
        forbid_host_path(config, env)
    if launcher:
        launch = Path(launcher).expanduser()
    elif require_launcher:
        launch = default_launcher(env, config if config is not None else Path("."))
    else:
        launch = None
    if launch is not None:
        forbid_host_path(launch, env)
    return AppPaths(config, launch)


def path_exists(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise NotifyMeError("unsafe_config_path")
    return True


def ensure_private_dir(path, env):
    """Create a 0700 directory. Callers must not use this from status or version."""

    forbid_host_path(path, env)
    reject_symlink_components(path, "unsafe_config_path")
    if path.is_symlink():
        raise NotifyMeError("unsafe_config_path")
    if path.exists() and not path.is_dir():
        raise NotifyMeError("unsafe_config_path")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        raise NotifyMeError("config_permissions")
    mode = stat.S_IMODE(path.lstat().st_mode)
    if mode & 0o077:
        raise NotifyMeError("config_permissions")


def directory_fact(path):
    """Read-only description. Does not create the directory."""

    if path is None:
        return {"status": "missing", "mode": None, "private": None}
    try:
        reject_symlink_components(path, "unsafe_config_path")
    except NotifyMeError:
        return {"status": "unsafe", "mode": None, "private": False}
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"status": "missing", "mode": None, "private": None}
    except OSError:
        return {"status": "unsafe", "mode": None, "private": False}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return {"status": "unsafe", "mode": None, "private": False}
    mode = stat.S_IMODE(info.st_mode)
    private = (mode & 0o077) == 0
    return {
        "status": "ready" if private else "unsafe",
        "mode": "{:04o}".format(mode),
        "private": private,
    }


def file_fact(path):
    if path is None:
        return {"status": "missing", "mode": None, "private": None}
    try:
        reject_symlink_components(path, "unsafe_state_path")
    except NotifyMeError:
        return {"status": "unsafe", "mode": None, "private": False}
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"status": "missing", "mode": None, "private": None}
    except OSError:
        return {"status": "unsafe", "mode": None, "private": False}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return {"status": "unsafe", "mode": None, "private": False}
    mode = stat.S_IMODE(info.st_mode)
    private = (mode & 0o077) == 0
    return {
        "status": "ready" if private else "unsafe",
        "mode": "{:04o}".format(mode),
        "private": private,
    }
