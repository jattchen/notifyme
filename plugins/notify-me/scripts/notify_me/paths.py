import json
import os
import stat
from pathlib import Path

PLUGIN_DIR_PREFIX = "notify-me-"
PLUGIN_SCRIPT = Path("scripts") / "notify_me.py"
STABLE_ENTRY_NAME = "notify-me"

_STABLE_ENTRY_SOURCE = """\
#!/usr/bin/env python3
import os
import sys
from pathlib import Path


def _resolver():
    override = os.environ.get("GROK_HOME")
    if override:
        grok = Path(override).expanduser()
    else:
        grok = Path.home() / ".grok"
    installed = grok / "installed-plugins"
    try:
        candidates = list(installed.glob("notify-me-*"))
    except OSError:
        candidates = []
    for path in candidates:
        scripts = path / "scripts"
        if (scripts / "notify_me" / "paths.py").is_file():
            if str(scripts) not in sys.path:
                sys.path.insert(0, str(scripts))
            from notify_me.paths import installed_plugin_root

            return installed_plugin_root
    return None


resolve = _resolver()
root = resolve() if resolve is not None else None
if root is None:
    sys.stderr.write("notify-me is not installed\\n")
    raise SystemExit(1)
script = str(root / "scripts" / "notify_me.py")
os.execv(sys.executable, [sys.executable, script, *sys.argv[1:]])
"""


def state_home():
    override = os.environ.get("GROK_NOTIFY_ME_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "grok-notify-me"


def grok_home():
    override = os.environ.get("GROK_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".grok"


def _usable_plugin_dir(path):
    try:
        return (
            path.is_dir()
            and path.name.startswith(PLUGIN_DIR_PREFIX)
            and (path / PLUGIN_SCRIPT).is_file()
        )
    except OSError:
        return False


def _registry_plugin_dirs(installed_root):
    registry = installed_root / "registry.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return []
    matches = []
    for key, repo in repos.items():
        if not isinstance(repo, dict):
            continue
        plugins = repo.get("plugins") or {}
        if not isinstance(plugins, dict) or "notify-me" not in plugins:
            continue
        raw = repo.get("path") or str(installed_root / key)
        path = Path(raw).expanduser()
        if not _usable_plugin_dir(path):
            continue
        stamp = str(repo.get("updated_at") or repo.get("installed_at") or "")
        matches.append((stamp, str(path.resolve())))
    return matches


def _mtime_plugin_dirs(installed_root):
    matches = []
    try:
        candidates = list(installed_root.glob("notify-me-*"))
    except OSError:
        return []
    for path in candidates:
        if not _usable_plugin_dir(path):
            continue
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            continue
        matches.append((stamp, str(path.resolve())))
    return matches


def installed_plugin_root(grok_dir=None):
    home = Path(grok_dir).expanduser() if grok_dir is not None else grok_home()
    installed = home / "installed-plugins"
    registry_hits = _registry_plugin_dirs(installed)
    if registry_hits:
        registry_hits.sort()
        return Path(registry_hits[-1][1])
    mtime_hits = _mtime_plugin_dirs(installed)
    if mtime_hits:
        mtime_hits.sort()
        return Path(mtime_hits[-1][1])
    return None


def stable_entry_path(grok_dir=None):
    home = Path(grok_dir).expanduser() if grok_dir is not None else grok_home()
    return home / STABLE_ENTRY_NAME


def write_stable_entry(grok_dir=None):
    root = installed_plugin_root(grok_dir)
    if root is None:
        return None
    dest = stable_entry_path(grok_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    dest.write_text(_STABLE_ENTRY_SOURCE, encoding="utf-8")
    os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return dest.resolve()


def ensure_private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, stat.S_IRWXU)


def chmod_private_file(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
