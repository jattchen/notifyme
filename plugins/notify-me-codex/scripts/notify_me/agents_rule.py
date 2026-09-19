"""Manage the small Notify Me policy block in Codex's global AGENTS.md."""

import os
import re
import tempfile

from .paths import agents_path


MANAGED_VERSION = "3"
MANAGED_START = "<!-- notify-me-codex:managed:start version={} -->".format(
    MANAGED_VERSION
)
MANAGED_END = "<!-- notify-me-codex:managed:end -->"
MANAGED_BODY = (
    "仅当前顶层主 Agent 直接调用 MCP 工具 "
    "mcp__notifyme_codex__notifyme（不是 Skill）；"
    "调用时必须传入扁平参数 condition、item_id、state、message、workspace；"
    "有可点开的网页时另传 url（http/https，不是 Bark 设备地址）；"
    "workspace 必须是当前项目根路径的绝对路径；"
    "调用格式固定为 {\"condition\":\"...\",\"item_id\":\"...\","
    "\"state\":\"...\",\"message\":\"...\","
    "\"workspace\":\"<绝对项目根路径>\"}，不得传 op；"
    "等待用户且主线停住 → condition=answer；"
    "等待用户授权 → condition=auth；"
    "等待用户在对话外操作 → condition=action；"
    "继续可能灾难性或大范围不可逆 → condition=severe-risk；"
    "用户这一次的整件事做完 → condition=done。\n"
    "未命中不发。中间步骤不发 done。仅 status=accepted 可称已推送。"
)
MANAGED_BLOCK_RE = re.compile(
    r"<!-- notify-me-codex:managed:start version=.*?-->"
    r"(?:"
    r"(?:(?!\n#{1,6} )(?!\n<!-- notify-me-codex:managed:start).)*?"
    r"<!-- notify-me-codex:managed:end -->"
    r"|"
    r".*?(?=\n#{1,6} |\n<!-- notify-me-codex:managed:start|\Z)"
    r")",
    re.DOTALL,
)
_COMPLETE_CURRENT_BLOCK_RE = re.compile(
    re.escape(MANAGED_START) + r".+?" + re.escape(MANAGED_END),
    re.DOTALL,
)


def managed_block():
    return "{}\n{}\n{}".format(MANAGED_START, MANAGED_BODY, MANAGED_END)


def _apply(text):
    block = managed_block()
    if MANAGED_BLOCK_RE.search(text or ""):
        seen = {"n": 0}

        def _replace(_match):
            seen["n"] += 1
            return block if seen["n"] == 1 else ""

        return MANAGED_BLOCK_RE.sub(_replace, text), "replaced"

    body = text or ""
    if body and not body.endswith("\n"):
        body += "\n"
    if "## 托管插件规则（Notify Me Codex）" not in body:
        body += "\n## 托管插件规则（Notify Me Codex）\n\n"
    elif not body.endswith("\n\n"):
        body += "\n"
    return body + block + "\n", "appended"


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
    except OSError as exc:
        raise OSError("AGENTS.md 是符号链接，但无法解析目标：{}".format(exc)) from exc
    if dest.exists() and dest.is_dir():
        raise OSError("AGENTS.md 是符号链接，不能指向目录")
    if not dest.parent.is_dir():
        raise OSError("AGENTS.md 是符号链接，目标目录不存在")
    return dest


def commit():
    path = agents_path()
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    updated, action = _apply(current)
    dest = _write_destination(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".agents.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
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
