"""Small local CLI for first-run setup and diagnostics."""

import getpass
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .agents_rule import check_target as check_rule_target
from .agents_rule import commit as commit_agents
from .agents_rule import has_managed_block, plan as plan_agents
from .bark import BarkEndpoint
from .binding import Binding
from .deliver import Deliverer
from .errors import NotifyMeError
from .host_install import (
    PLUGIN_ID,
    check_settings,
    commit_permission,
    permission_allowed,
    permission_blocked,
    plugin_enabled,
    plugin_installs,
)
from .paths import agents_path, claude_home, plugin_root, settings_path, state_home


def _emit(payload, exit_code):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    return exit_code


_FLAG_OPTIONS = {"dry-run"}
_VALUE_OPTIONS = {"message", "group"}


def _options(tokens):
    parsed = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            raise NotifyMeError("invalid_arguments", "命令参数格式无效")
        raw_name = token[2:]
        if raw_name in _FLAG_OPTIONS:
            parsed[raw_name.replace("-", "_")] = True
            index += 1
        elif raw_name in _VALUE_OPTIONS:
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                raise NotifyMeError("invalid_arguments", "命令参数格式无效")
            parsed[raw_name.replace("-", "_")] = tokens[index + 1]
            index += 2
        else:
            raise NotifyMeError("invalid_arguments", "命令参数格式无效")
    return parsed


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


def _commit_host():
    # Pre-approve the tool before the rule goes live, so a failure never
    # leaves a rule that sends every session into a permission prompt.
    written = {}
    for key, code, step, commit in (
        ("permission", "settings_write_failed", "settings.json 授权", commit_permission),
        ("agents", "rule_write_failed", "托管规则", commit_agents),
    ):
        try:
            written[key] = commit()
        except Exception as exc:
            raise _HostWriteError(code, step, exc, written) from exc
    return written


class _HostWriteError(Exception):
    def __init__(self, code, step, cause, written):
        super().__init__(str(cause))
        self.code = code
        self.step = step
        self.written = written


def _plugin_state():
    installed = bool(plugin_installs())
    enabled = plugin_enabled()
    warnings = []
    if not installed:
        warnings.append("plugin_not_installed")
    if not enabled:
        warnings.append("plugin_not_enabled")
    state = {"plugin_installed": installed, "plugin_enabled": enabled, "warnings": warnings}
    if warnings:
        state["hint"] = _PLUGIN_HINT
    return state


_PLUGIN_HINT = (
    "未检测到已安装并启用的 {}；先运行 claude plugin marketplace add jattchen/notifyme "
    "和 claude plugin install {}，否则规则指向的工具不存在".format(PLUGIN_ID, PLUGIN_ID)
)


def _setup(options):
    if not sys.stdin.isatty():
        raise NotifyMeError(
            "tty_required",
            "setup 必须在真实终端里输入 Bark 地址，不要把 URL 发到对话、命令参数或日志",
        )
    if options.get("dry_run"):
        return {"ok": True, "status": "dry_run"}

    check_settings()
    try:
        check_rule_target()
    except OSError as exc:
        raise NotifyMeError("rule_unwritable", str(exc)) from exc
    plugin = _plugin_state()
    sys.stderr.write("配置目录：{}\n".format(claude_home()))
    if plugin["warnings"]:
        sys.stderr.write("警告：{}\n".format(_PLUGIN_HINT))
    raw = getpass.getpass("请粘贴 Bark 推送 URL（输入不可见）：")
    endpoint = BarkEndpoint.parse(raw)
    binding = Binding()
    previous = _load_previous_binding(binding)
    view = binding.save(endpoint)
    tested = Deliverer().test({})
    if tested.get("status") != "accepted":
        if tested.get("category") in ("timeout", "network_error"):
            if previous is not None:
                _restore_previous_binding(binding, previous)
            return {
                "ok": False,
                "error": {
                    "code": "test_unconfirmed",
                    "message": (
                        "测试通知超时或未能连接，新地址未确认，仍使用原来的绑定"
                        if previous is not None
                        else "测试通知超时或未能连接，绑定已保留"
                    ),
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

    try:
        written = _commit_host()
    except _HostWriteError as exc:
        return {
            "ok": False,
            "status": "bound",
            "host": view["host"],
            "test": "accepted",
            "config_dir": str(claude_home()),
            "permission": exc.written.get("permission"),
            "error": {
                "code": exc.code,
                "message": "测试通知已被接受，卡在写入{}".format(exc.step),
                "detail": str(exc),
            },
        }
    result = {
        "ok": True,
        "status": "bound",
        "host": view["host"],
        "test": "accepted",
        "config_dir": str(claude_home()),
        "agents": written.get("agents"),
        "permission": written.get("permission"),
    }
    result.update(plugin)
    return result


_FORWARDED_ENV = ("CLAUDE_CONFIG_DIR", "CLAUDE_NOTIFY_ME_HOME")


def _applescript_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def setup_terminal_script(env=None, python=None):
    """Build the AppleScript that runs setup in a new Terminal window.

    The new shell doesn't inherit this process's environment, so the config
    and state overrides are passed explicitly and every path is shell-quoted.
    """
    env = env if env is not None else os.environ
    parts = ["env"]
    assignments = []
    for key in _FORWARDED_ENV:
        value = env.get(key)
        if value:
            assignments.append("{}={}".format(key, shlex.quote(value)))
        else:
            # Don't let the login shell's own export point setup elsewhere.
            parts.extend(["-u", key])
    parts.extend(assignments)
    parts.append(shlex.quote(python or sys.executable))
    parts.append(shlex.quote(str(Path(__file__).resolve().parents[1] / "notify_me.py")))
    parts.append("setup")
    command = "bash -lc {}".format(shlex.quote(" ".join(parts)))
    return (
        'tell application "Terminal"\n'
        "  activate\n"
        "  do script {}\n"
        "end tell\n"
    ).format(_applescript_string(command))


_OPEN_SETUP_NEXT = (
    "setup 在新终端里运行，结果只显示在那个窗口；等用户确认终端里完成后，"
    "再运行 doctor 核对，不要提前声称已绑定或已推送"
)


def _open_setup(options):
    script = setup_terminal_script()
    base = {"config_dir": str(claude_home()), "next": _OPEN_SETUP_NEXT}
    base.update(_plugin_state())
    if options.get("dry_run"):
        return dict(base, ok=True, status="dry_run", script=script)
    completed = subprocess.run(
        ["osascript"], input=script, text=True, capture_output=True
    )
    if completed.returncode != 0:
        raise NotifyMeError("terminal_failed", "无法打开终端：{}".format(completed.stderr.strip()))
    return dict(base, ok=True, status="opened")


def _doctor(deliverer):
    binding_store = deliverer.binding
    try:
        binding = binding_store.load().public_view()
    except NotifyMeError as exc:
        if exc.code in ("insecure_binding", "invalid_binding"):
            raise
        binding = binding_store.public_view()
    deliverer._read_accepted_keys(deliverer._accepted_path(), fail_closed=True)
    agents = agents_path()
    agents_text = agents.read_text(encoding="utf-8") if agents.is_file() else ""
    server = plugin_root() / "scripts" / "mcp_server.py"
    return {
        "ok": True,
        "status": "ok",
        "host": "claude",
        "host_detected": claude_home().is_dir(),
        "bound": bool(binding.get("bound")),
        "host_endpoint": binding.get("host"),
        "state_home": str(state_home()),
        "rule": str(agents),
        "rule_managed": has_managed_block(agents_text),
        "settings_json": str(settings_path()),
        "permission_allowed": permission_allowed(),
        "permission_blocked_by": permission_blocked(),
        "plugin_id": PLUGIN_ID,
        "plugin_installed": bool(plugin_installs()),
        "plugin_enabled": plugin_enabled(),
        "mcp_server": str(server),
        "mcp_server_present": server.is_file(),
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _emit(
            NotifyMeError(
                "invalid_arguments",
                "请指定 open-setup、setup、test、refresh-icons、doctor 或 agents-rule 命令",
            ).as_dict(),
            1,
        )
    command = argv[0]
    try:
        deliverer = Deliverer()
        if command == "agents-rule":
            if len(argv) < 2 or argv[1].startswith("--"):
                raise NotifyMeError("invalid_arguments", "agents-rule 只支持 plan 或 commit")
            sub = argv[1]
            options = _options(argv[2:])
            if sub == "plan":
                result = plan_agents()
            elif sub == "commit":
                result = {"ok": True, "status": "dry_run"} if options.get("dry_run") else commit_agents()
            else:
                raise NotifyMeError("invalid_arguments", "agents-rule 只支持 plan 或 commit")
        elif command == "setup":
            result = _setup(_options(argv[1:]))
        elif command == "open-setup":
            result = _open_setup(_options(argv[1:]))
        elif command == "test":
            result = deliverer.test(_options(argv[1:]))
        elif command == "refresh-icons":
            result = deliverer.refresh_icons(_options(argv[1:]))
        elif command == "doctor":
            result = _doctor(deliverer)
        else:
            raise NotifyMeError("unsupported_command", "不支持的命令")
        return _emit(result, 0 if result.get("ok") else 1)
    except NotifyMeError as exc:
        return _emit(exc.as_dict(), 1)
    except Exception as exc:
        return _emit(
            {"ok": False, "error": {"code": "internal_error", "message": str(exc)}},
            1,
        )
