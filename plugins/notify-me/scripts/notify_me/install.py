import getpass
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .agents_rule import commit as commit_agents
from .bark import BarkEndpoint
from .binding import Binding
from .deliver import Deliverer
from .errors import NotifyMeError
from .paths import (
    PLUGIN_DIR_PREFIX,
    installed_plugin_root,
    stable_entry_path,
    write_stable_entry,
)


REPO = "jattchen/notifyme"
GITHUB_PLUGIN = "jattchen/notifyme#plugins/notify-me"


def plugin_root():
    return Path(__file__).resolve().parents[2]


def _say(message):
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def _require_tty():
    # Piped stdin is not a TTY; getpass can still read Bark URL from /dev/tty.
    isatty = getattr(sys.stdout, "isatty", None)
    if not callable(isatty) or not isatty():
        raise NotifyMeError(
            "tty_required",
            "请在 macOS「终端」里运行安装，以便输入 Bark 地址。Agent 应弹出终端窗口执行安装命令，不要在无 TTY 的工具调用里跑。",
        )


def _run(argv, check=True):
    result = subprocess.run(argv, capture_output=True, text=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise NotifyMeError(
            "install_command_failed",
            "命令失败：{}".format(" ".join(argv)),
            detail=detail[-500:] if detail else "",
        )
    return result


def _is_installed_hash_dir(path):
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return (
        resolved.name.startswith(PLUGIN_DIR_PREFIX)
        and resolved.parent.name == "installed-plugins"
        and (resolved / "plugin.json").is_file()
    )


def _ensure_plugin():
    source = plugin_root()
    already_installed = _is_installed_hash_dir(source)
    if (source / "plugin.json").is_file() and not already_installed:
        local = _run(
            ["grok", "plugin", "install", str(source), "--trust"],
            check=False,
        )
        if local.returncode != 0:
            detail = (local.stderr or local.stdout or "").strip()
            raise NotifyMeError(
                "plugin_install_failed",
                "无法从本地源码目录安装插件。",
                detail=detail[-500:] if detail else "",
            )
    dest = installed_plugin_root()
    if dest is None and not already_installed:
        remote = _run(
            ["grok", "plugin", "install", GITHUB_PLUGIN, "--trust"],
            check=False,
        )
        if remote.returncode != 0:
            if shutil.which("gh") is None:
                raise NotifyMeError(
                    "plugin_install_failed",
                    "无法安装插件。请确认已登录 GitHub CLI（gh），或仓库已公开。",
                )
            tmp = Path(tempfile.mkdtemp(prefix="notify-me-install-"))
            try:
                _run(["gh", "repo", "clone", REPO, str(tmp / "src")])
                _run(
                    ["grok", "plugin", "install", str(tmp / "src" / "plugins" / "notify-me"), "--trust"]
                )
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    _run(["grok", "plugin", "enable", "notify-me"], check=False)
    dest = installed_plugin_root()
    if dest is None and already_installed:
        dest = source.resolve()
    if dest is None:
        raise NotifyMeError(
            "plugin_install_failed",
            "插件安装后未找到 ~/.grok/installed-plugins/notify-me-*",
        )
    write_stable_entry()
    return dest


def _mcp_points_at(text, name, server):
    prefix = name + ":"
    needle = str(server)
    require_alias_name = name == "notifyme"
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix) and needle in stripped:
            if require_alias_name and "--name notifyme" not in stripped:
                continue
            return True
    return False


def _ensure_mcp(plugin_dir):
    listed = _run(["grok", "mcp", "list"], check=False)
    text = (listed.stdout or "") + (listed.stderr or "")
    server = plugin_dir / "scripts" / "mcp_server.py"
    if not _mcp_points_at(text, "notify_me", server):
        _run(["grok", "mcp", "add", "notify_me", "--", "python3", "-u", str(server)])
    if not _mcp_points_at(text, "notifyme", server):
        _run(
            [
                "grok",
                "mcp",
                "add",
                "notifyme",
                "--",
                "python3",
                "-u",
                str(server),
                "--name", "notifyme",
            ]
        )


def _load_previous_binding(binding):
    try:
        return binding.load()
    except NotifyMeError:
        return None


def _restore_previous_binding(binding, previous):
    if previous is not None:
        binding.save(previous)
        return
    try:
        binding.path.unlink()
    except OSError:
        pass


def run_install():
    _require_tty()
    _say("正在安装 Notify Me…")
    plugin_dir = _ensure_plugin()
    _ensure_mcp(plugin_dir)
    _say("请粘贴 Bark 推送 URL（输入不可见，不会出现在 Grok 对话里）。")
    raw = getpass.getpass("Bark URL: ")
    endpoint = BarkEndpoint.parse(raw)
    binding = Binding()
    previous = _load_previous_binding(binding)
    view = binding.save(endpoint)
    _say("已绑定 {}。正在发送测试通知…".format(view["host"]))
    tested = Deliverer().test({})
    if tested.get("status") != "accepted":
        if tested.get("category") in ("timeout", "network_error"):
            return {
                "ok": False,
                "error": {
                    "code": "test_unconfirmed",
                    "message": "测试通知超时或未能连接，绑定已保留",
                    "result": tested,
                },
            }
        _restore_previous_binding(binding, previous)
        return {
            "ok": False,
            "error": {
                "code": "test_not_accepted",
                "message": "测试通知未被 Bark 接受",
                "result": tested,
            },
        }
    written = commit_agents()
    _say("测试通知已发送。已写入 {}。请新开一局 Grok。".format(written["target"]))
    entry = write_stable_entry() or stable_entry_path()
    script = plugin_dir / "scripts" / "notify_me.py"
    _say("稳定入口：python3 {} doctor".format(entry))
    _say("当前脚本：python3 {} doctor".format(script))
    return {
        "ok": True,
        "status": "installed",
        "host": view["host"],
        "test": "accepted",
        "agents": written,
    }
