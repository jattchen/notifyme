#!/usr/bin/env bash
set -euo pipefail

main() {
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
import sys
from pathlib import Path

home = Path(sys.argv[1]).expanduser()
installed = home / "installed-plugins"
try:
    candidates = list(installed.glob("notify-me-*"))
except OSError:
    candidates = []
for path in candidates:
    scripts = path / "scripts"
    if not (scripts / "notify_me" / "paths.py").is_file():
        continue
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from notify_me.paths import installed_plugin_root

    root = installed_plugin_root(home)
    if root is None:
        raise SystemExit(1)
    print(root)
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
}

main "$@"
