"""Manage the Notify Me policy rule in Claude Code's user rules directory."""

import os
import re
import tempfile

from .paths import agents_path, write_problem


TOOL_PERMISSION = "mcp__plugin_notify-me-claude_notifyme_claude__notifyme"
MANAGED_VERSION = "1"
MANAGED_START = "<!-- notify-me-claude:managed:start version={} -->".format(
    MANAGED_VERSION
)
MANAGED_END = "<!-- notify-me-claude:managed:end -->"
MANAGED_BODY = (
    "仅主 Agent 直接调 MCP 工具 {}（不是 Skill，不是 CLI）；"
    "子 Agent、委派 Agent 不发；"
    "调用时必须传入扁平参数 condition、item_id、state、message、workspace；"
    "有可点开的网页时另传 url（http/https，不是 Bark 设备地址）；"
    "workspace 必须是当前项目根路径的绝对路径；"
    "不得传 op；"
    "等待用户且主线停住 → condition=answer；"
    "等待用户授权 → condition=auth；"
    "等待用户在对话外操作 → condition=action；"
    "继续可能灾难性或大范围不可逆 → condition=severe-risk；"
    "用户这一次的整件事做完 → condition=done。\n"
    "未命中不发。中间步骤不发 done。仅 status=accepted 可称已推送。"
).format(TOOL_PERMISSION)
RULE_HEADER = (
    "# Notify Me Claude\n\n"
    "<!-- 本文件整体由 notify-me-claude setup 管理，重新运行 setup 会覆盖全部内容。 -->\n"
)
_COMPLETE_CURRENT_BLOCK_RE = re.compile(
    re.escape(MANAGED_START) + r".+?" + re.escape(MANAGED_END),
    re.DOTALL,
)


def managed_block():
    return "{}\n{}\n{}".format(MANAGED_START, MANAGED_BODY, MANAGED_END)


def managed_file():
    return RULE_HEADER + managed_block() + "\n"


def _apply(text):
    canonical = managed_file()
    if text:
        return canonical, "replaced"
    return canonical, "appended"


def check_target():
    """Fail before any write if the rule path can't hold a regular file."""
    problem = write_problem(_write_destination(agents_path()))
    if problem:
        raise OSError(problem)


def plan():
    path = agents_path()
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    _, action = _apply(current)
    return {
        "ok": True,
        "status": "plan",
        "target": str(path),
        "action": action,
        "block": managed_block(),
    }


def _write_destination(path):
    if not path.is_symlink():
        return path
    try:
        dest = path.resolve()
    except (OSError, RuntimeError) as exc:
        # Python < 3.13 raises RuntimeError for a symlink loop.
        raise OSError("notify-me-claude.md 是符号链接，但无法解析目标：{}".format(exc)) from exc
    if dest.exists() and dest.is_dir():
        raise OSError("notify-me-claude.md 是符号链接，不能指向目录")
    if not dest.parent.is_dir():
        raise OSError("notify-me-claude.md 是符号链接，目标目录不存在")
    return dest


def commit():
    path = agents_path()
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    updated, action = _apply(current)
    dest = _write_destination(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".notify-me.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {
        "ok": True,
        "status": "committed",
        "target": str(path),
        "action": action,
        "version": MANAGED_VERSION,
    }


def has_managed_block(text=None):
    if text is None:
        path = agents_path()
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
    return _COMPLETE_CURRENT_BLOCK_RE.search(text) is not None
