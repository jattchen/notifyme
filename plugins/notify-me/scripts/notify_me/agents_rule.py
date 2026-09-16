import os
import re
import tempfile

from .paths import grok_home


MANAGED_VERSION = "7"
MANAGED_START = "<!-- notify-me:managed:start version={} -->".format(MANAGED_VERSION)
MANAGED_END = "<!-- notify-me:managed:end -->"
MANAGED_BODY = (
    "仅主 Agent 直接调 MCP 工具 notifyme__notifyme（不是 Skill）；"
    "没有该工具时用 notify_me__notify_me："
    "等用户且主线停住 → 缺信息/选择 condition=answer；"
    "等用户授权 condition=auth；等用户去操作 condition=action。"
    "继续可能灾难性或大范围不可逆 → condition=severe-risk。"
    "用户这一次要的事整件做完 → condition=done。\n"
    "未命中不发。中间步骤不发 done。仅 status=accepted 可称已推送。"
)
MANAGED_BLOCK_RE = re.compile(
    r"<!-- notify-me:managed:start version=.*?-->"
    r"(?:.*?<!-- notify-me:managed:end -->|.*\Z)",
    re.DOTALL,
)
_COMPLETE_CURRENT_BLOCK_RE = re.compile(
    re.escape(MANAGED_START) + r".+?" + re.escape(MANAGED_END),
    re.DOTALL,
)


def managed_block():
    return "{}\n{}\n{}".format(MANAGED_START, MANAGED_BODY, MANAGED_END)


def agents_path():
    return grok_home() / "AGENTS.md"


def _apply(text):
    block = managed_block()
    if MANAGED_BLOCK_RE.search(text or ""):
        return MANAGED_BLOCK_RE.sub(lambda _: block, text), "replaced"
    body = text or ""
    if body and not body.endswith("\n"):
        body += "\n"
    if "## 托管插件规则" not in body:
        body += "\n## 托管插件规则\n\n"
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


def commit():
    path = agents_path()
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    updated, action = _apply(current)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".agents.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
        os.replace(tmp, path)
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
