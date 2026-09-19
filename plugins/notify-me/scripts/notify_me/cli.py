import getpass
import json
import sys

from .agents_rule import commit as commit_agents
from .agents_rule import has_managed_block, plan as plan_agents
from .bark import BarkEndpoint
from .binding import Binding
from .deliver import Deliverer
from .errors import NotifyMeError
from .install import (
    _check_mcp_current,
    _load_previous_binding,
    _restore_previous_binding,
    run_install,
)
from .paths import grok_home, write_stable_entry


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


def _setup(options):
    if not sys.stdin.isatty():
        raise NotifyMeError(
            "tty_required",
            "setup 必须在终端里输入 Bark 地址，不要把 URL 发到对话里",
        )
    if options.get("dry_run"):
        return {"ok": True, "status": "dry_run"}
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
        written = commit_agents()
    except Exception:
        return {
            "ok": False,
            "status": "bound",
            "host": view["host"],
            "test": "accepted",
            "error": {
                "code": "agents_write_failed",
                "message": "测试通知已被接受，卡在写入托管规则",
            },
        }
    return {
        "ok": True,
        "status": "bound",
        "host": view["host"],
        "test": "accepted",
        "agents": written,
    }


def _doctor(deliverer):
    binding_store = deliverer.binding
    try:
        binding = binding_store.load().public_view()
    except NotifyMeError as exc:
        if exc.code in ("insecure_binding", "invalid_binding"):
            raise
        binding = binding_store.public_view()
    deliverer._read_accepted_keys(deliverer._accepted_path(), fail_closed=True)
    _check_mcp_current()
    agents = grok_home() / "AGENTS.md"
    agents_text = agents.read_text(encoding="utf-8") if agents.is_file() else ""
    return {
        "ok": True,
        "status": "ok",
        "host_detected": grok_home().is_dir(),
        "bound": bool(binding.get("bound")),
        "host": binding.get("host"),
        "agents_md": str(agents),
        "agents_managed": has_managed_block(agents_text),
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        write_stable_entry()
    except OSError:
        pass
    if not argv:
        return _emit(NotifyMeError("invalid_arguments", "请指定命令").as_dict(), 1)
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
                if options.get("dry_run"):
                    result = {"ok": True, "status": "dry_run"}
                else:
                    result = commit_agents()
            else:
                raise NotifyMeError("invalid_arguments", "agents-rule 只支持 plan 或 commit")
        elif command == "install":
            options = _options(argv[1:])
            if options.get("dry_run"):
                result = {"ok": True, "status": "dry_run"}
            else:
                result = run_install()
        else:
            options = _options(argv[1:])
            if command == "setup":
                result = _setup(options)
            elif command == "doctor":
                result = _doctor(deliverer)
            elif command == "test":
                params = {"op": "test", "dry_run": bool(options.get("dry_run"))}
                if "message" in options:
                    params["message"] = options["message"]
                if "group" in options:
                    params["group"] = options["group"]
                result = deliverer.dispatch(params)
            elif command == "refresh-icons":
                result = deliverer.refresh_icons(options)
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
