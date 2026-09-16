#!/usr/bin/env bash
set -euo pipefail

# Piped stdin is not a TTY; a real Terminal still has stdout TTY.
if [[ ! -t 1 ]]; then
  echo "请在 macOS「终端」里运行，以便输入 Bark 地址。" >&2
  echo "Agent 应弹出一个新的终端窗口来执行本脚本，用户不必先自己开终端。" >&2
  exit 1
fi

if grok plugin install jattchen/notifyme#plugins/notify-me --trust; then
  true
else
  if ! command -v gh >/dev/null 2>&1; then
    echo "无法安装插件：grok plugin install 失败，且没有 gh。" >&2
    exit 1
  fi
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  gh repo clone jattchen/notifyme "$tmp/src"
  grok plugin install "$tmp/src/plugins/notify-me" --trust
  rm -rf "$tmp"
  trap - EXIT
fi

grok plugin enable notify-me >/dev/null 2>&1 || true

grok_home="${GROK_HOME:-$HOME/.grok}"
if ! plugin="$(python3 - "$grok_home" <<'NOTIFY_ME_RESOLVE_PLUGIN'
import json
import sys
from pathlib import Path

home = Path(sys.argv[1]).expanduser()
installed = home / "installed-plugins"
needle = Path("scripts") / "notify_me.py"


def usable(path):
    try:
        return (
            path.is_dir()
            and path.name.startswith("notify-me-")
            and (path / needle).is_file()
        )
    except OSError:
        return False


matches = []
registry = installed / "registry.json"
try:
    data = json.loads(registry.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError, UnicodeDecodeError):
    data = {}
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
        if usable(path):
            stamp = str(repo.get("updated_at") or repo.get("installed_at") or "")
            matches.append((stamp, str(path.resolve())))
if matches:
    matches.sort()
    print(matches[-1][1])
    raise SystemExit(0)

mtime_hits = []
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
    mtime_hits.append((stamp, str(path.resolve())))
if mtime_hits:
    mtime_hits.sort()
    print(mtime_hits[-1][1])
    raise SystemExit(0)

raise SystemExit(1)
NOTIFY_ME_RESOLVE_PLUGIN
)"; then
  echo "插件安装后未找到 Notify Me（~/.grok/installed-plugins/notify-me-*）。" >&2
  exit 1
fi

if [[ ! -f "$plugin/scripts/mcp_server.py" || ! -f "$plugin/scripts/notify_me.py" ]]; then
  echo "插件安装后未找到 $plugin/scripts" >&2
  exit 1
fi

mcp_list="$(grok mcp list 2>/dev/null || true)"
if ! printf '%s\n' "$mcp_list" | grep '^[[:space:]]*notify_me:' | grep -Fq -- "$plugin/scripts/mcp_server.py"; then
  grok mcp add notify_me -- python3 -u "$plugin/scripts/mcp_server.py"
fi
if ! printf '%s\n' "$mcp_list" | grep '^[[:space:]]*notifyme:' | grep -Fq -- "$plugin/scripts/mcp_server.py"; then
  grok mcp add notifyme -- python3 -u "$plugin/scripts/mcp_server.py" --name notifyme
fi

# Do not inherit the install script pipe; getpass needs a real terminal.
exec python3 "$plugin/scripts/notify_me.py" install </dev/tty
