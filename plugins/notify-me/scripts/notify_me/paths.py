import importlib.util
import json
import os
import stat
import tempfile
from pathlib import Path

PLUGIN_DIR_PREFIX = "notify-me-"
PLUGIN_SCRIPT = Path("scripts") / "notify_me.py"
PLUGIN_MCP_SERVER = Path("scripts") / "mcp_server.py"
PLUGIN_PATHS_MODULE = Path("scripts") / "notify_me" / "paths.py"
STABLE_ENTRY_NAME = "notify-me"

_STABLE_ENTRY_SOURCE = """\
#!/usr/bin/env python3
import json
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

    def usable(path):
        try:
            return (
                path.is_dir()
                and path.name.startswith("notify-me-")
                and (path / "scripts" / "notify_me.py").is_file()
                and (path / "scripts" / "mcp_server.py").is_file()
                and (path / "scripts" / "notify_me" / "paths.py").is_file()
            )
        except OSError:
            return False

    def forget(scripts):
        for name in list(sys.modules):
            if name == "notify_me" or name.startswith("notify_me."):
                del sys.modules[name]
        try:
            sys.path.remove(str(scripts))
        except ValueError:
            pass

    matches = []
    registry = installed / "registry.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        data = None
    repos = data.get("repos") if isinstance(data, dict) else None
    if isinstance(repos, dict):
        for key, repo in repos.items():
            if not isinstance(repo, dict):
                continue
            plugins = repo.get("plugins") or {}
            if not isinstance(plugins, dict) or "notify-me" not in plugins:
                continue
            raw = repo.get("path") or str(installed / key)
            path = Path(raw).expanduser()
            if not usable(path):
                continue
            stamp = str(repo.get("updated_at") or repo.get("installed_at") or "")
            matches.append((stamp, path.resolve()))
    if not matches:
        try:
            candidates = list(installed.glob("notify-me-*"))
        except OSError:
            candidates = []
        for path in candidates:
            if not usable(path):
                continue
            try:
                stamp = path.stat().st_mtime_ns
            except OSError:
                continue
            matches.append((stamp, path.resolve()))
    if not matches:
        return None
    matches.sort()
    for _, chosen in reversed(matches):
        scripts = chosen / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        try:
            from notify_me.paths import installed_plugin_root
        except ImportError:
            forget(scripts)
            continue
        if installed_plugin_root() is None:
            forget(scripts)
            continue
        return lambda: chosen
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
            and (path / PLUGIN_MCP_SERVER).is_file()
            and (path / PLUGIN_PATHS_MODULE).is_file()
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


def _plugin_dir_exports_current_api(path):
    paths_file = Path(path) / PLUGIN_PATHS_MODULE
    try:
        spec = importlib.util.spec_from_file_location(
            "_notify_me_paths_api_probe",
            paths_file,
        )
    except (OSError, ValueError):
        return False
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return False
    return hasattr(module, "installed_plugin_root")


def _first_importable_plugin_dir(hits):
    for _, raw in reversed(hits):
        path = Path(raw)
        if _plugin_dir_exports_current_api(path):
            return path
    return None


def _resolve_installed_plugin_root(grok_dir=None):
    home = Path(grok_dir).expanduser() if grok_dir is not None else grok_home()
    installed = home / "installed-plugins"
    registry_hits = _registry_plugin_dirs(installed)
    if registry_hits:
        registry_hits.sort()
        chosen = _first_importable_plugin_dir(registry_hits)
        if chosen is not None:
            return chosen
        return Path(registry_hits[-1][1])
    mtime_hits = _mtime_plugin_dirs(installed)
    if mtime_hits:
        mtime_hits.sort()
        chosen = _first_importable_plugin_dir(mtime_hits)
        if chosen is not None:
            return chosen
        return Path(mtime_hits[-1][1])
    return None


def installed_plugin_root(grok_dir=None):
    root = _resolve_installed_plugin_root(grok_dir)
    if root is not None:
        try:
            write_stable_entry(grok_dir)
        except OSError:
            pass
    return root


def stable_entry_path(grok_dir=None):
    home = Path(grok_dir).expanduser() if grok_dir is not None else grok_home()
    return home / STABLE_ENTRY_NAME


def write_stable_entry(grok_dir=None):
    root = _resolve_installed_plugin_root(grok_dir)
    if root is None:
        return None
    dest = stable_entry_path(grok_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".notify-me.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_STABLE_ENTRY_SOURCE)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return dest.resolve()


def ensure_private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, stat.S_IRWXU)


def chmod_private_file(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
