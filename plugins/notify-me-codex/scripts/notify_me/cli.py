"""Small local CLI for first-run setup and diagnostics."""

import getpass
import json
import sys

from .agents_rule import commit as commit_agents
from .agents_rule import has_managed_block, plan as plan_agents
from .bark import BarkEndpoint
from .binding import Binding
from .deliver import Deliverer
from .errors import NotifyMeError
from .paths import agents_path, codex_home, plugin_root, state_home


def _emit(payload, exit_code):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    return exit_code


_FLAG_OPTIONS = {"dry-run"}
_VALUE_OPTIONS = {"message"}


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


def _setup(options):
    if not sys.stdin.isatty():
        raise NotifyMeError(
            "tty_required",
            "setup 必须在真实终端里输入 Bark 地址，不要把 URL 发到对话、命令参数或日志",
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
    except Exception as exc:
        return {
            "ok": False,
            "status": "bound",
            "host": view["host"],
            "test": "accepted",
            "error": {
                "code": "agents_write_failed",
                "message": "测试通知已被接受，卡在写入 Codex 托管规则",
                "detail": str(exc),
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
    agents = agents_path()
    agents_text = agents.read_text(encoding="utf-8") if agents.is_file() else ""
    server = plugin_root() / "scripts" / "mcp_server.py"
    return {
        "ok": True,
        "status": "ok",
        "host": "codex",
        "host_detected": codex_home().is_dir(),
        "bound": bool(binding.get("bound")),
        "host_endpoint": binding.get("host"),
        "state_home": str(state_home()),
        "agents_md": str(agents),
        "agents_managed": has_managed_block(agents_text),
        "mcp_server": str(server),
        "mcp_server_present": server.is_file(),
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _emit(
            NotifyMeError("invalid_arguments", "请指定 setup、doctor 或 agents-rule 命令").as_dict(),
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
        elif command == "test":
            result = deliverer.test(_options(argv[1:]))
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
