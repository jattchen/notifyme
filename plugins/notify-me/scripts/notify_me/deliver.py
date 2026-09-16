import json
import os
import tempfile
from pathlib import Path

from .bark import BarkTransport
from .binding import Binding
from .errors import NotifyMeError
from .paths import chmod_private_file, ensure_private_dir


TOOL_NAME = "notify_me"
CURRENT_TOOL_NAME = "notifyme"
TOOL_NAMES = (TOOL_NAME, CURRENT_TOOL_NAME)
TOOL_DESCRIPTION = (
    "Main agent: send answer|auth|action|severe-risk|done; test verifies Bark. "
    "Never pass Bark URLs."
)
OPS = ("send", "test")
SENDABLE = ("answer", "auth", "action", "severe-risk", "done")
WAITING_EFFECT = {"level": "timeSensitive", "sound": "telegraph"}
QUIET_EFFECT = {"level": "active", "sound": "glass"}
TITLE_MARKS = {
    "answer": "💬 待回答",
    "auth": "🔐 待授权",
    "action": "🖐️ 待操作",
    "severe-risk": "🛑 先停下",
    "done": "✅ 已完成",
}
TEST_TITLE = "🔔 已接通"
EFFECTS = {
    "answer": WAITING_EFFECT,
    "auth": WAITING_EFFECT,
    "action": WAITING_EFFECT,
    "severe-risk": {"level": "critical", "sound": "alarm", "volume": 8},
    "done": QUIET_EFFECT,
    "test": QUIET_EFFECT,
}
DEFAULT_BARK_ICON_URL = (
    "https://cdn.jsdelivr.net/gh/jattchen/grok-build-bark-icon@main/grok-build-icon.png"
)
ACCEPTED_FILENAME = "accepted.json"
TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "inputSchema": {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": list(OPS),
                "description": "send delivers a notification; test verifies Bark binding.",
            },
            "condition": {
                "type": "string",
                "enum": list(SENDABLE),
                "description": (
                    "Required for send. answer: need a reply or choice. "
                    "auth: need permission or token. "
                    "action: user must act outside chat. "
                    "severe-risk: continuing is irreversible. "
                    "done: the user's full request is finished, not a substep."
                ),
            },
            "item_id": {
                "type": "string",
                "description": "Stable id for this incident. Required for send.",
            },
            "state": {
                "type": "string",
                "description": "Stable semantic state. Required for send.",
            },
            "message": {
                "type": "string",
                "description": (
                    "Short user-facing sentence in the user's language. Required for send."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, do not POST to Bark and do not record dedup.",
            },
        },
        "required": ["op"],
        "additionalProperties": False,
    },
}


def _required(params, name):
    value = (params or {}).get(name)
    if not isinstance(value, str) or not value.strip():
        raise NotifyMeError("invalid_arguments", "缺少 {}".format(name))
    return value.strip()


def _resolved_path(start):
    if not start:
        return None
    try:
        return Path(start).expanduser().resolve()
    except OSError:
        return None


def _git_root_name(start, home):
    path = _resolved_path(start)
    if path is None or path == home:
        return None
    current = path
    while True:
        if (current / ".git").exists() and current != home:
            return current.name
        if current.parent == current:
            return None
        current = current.parent


def _directory_name(start, home):
    path = _resolved_path(start)
    if path is None or path == home:
        return None
    try:
        if not path.is_dir():
            return None
    except OSError:
        return None
    return path.name or None


def _project_from(start, home):
    return _git_root_name(start, home) or _directory_name(start, home)


def project_name(env=None):
    env = env or os.environ
    home = Path.home().resolve()
    explicit = env.get("GROK_WORKSPACE_ROOT") or env.get("CLAUDE_PROJECT_DIR")
    if explicit:
        return _project_from(explicit, home)
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = None
    from_cwd = _project_from(cwd, home)
    if from_cwd:
        return from_cwd
    return _project_from(env.get("PWD"), home)


def _compose_title(condition, project=None):
    mark = TITLE_MARKS[condition]
    if project:
        return "{} · {}".format(mark, project)
    return mark


def _build_payload(endpoint, title, body, effect, group="Grok"):
    payload = {
        "device_key": endpoint.key,
        "title": title,
        "body": body,
        "group": group,
        "icon": DEFAULT_BARK_ICON_URL,
    }
    if effect.get("level"):
        payload["level"] = effect["level"]
    if effect.get("sound"):
        payload["sound"] = effect["sound"]
    if effect.get("volume") is not None:
        payload["volume"] = str(effect["volume"])
    return payload


class Deliverer:
    def __init__(self, binding=None, transport=None):
        self.binding = binding or Binding()
        self.transport = transport or BarkTransport()
        self._accepted = set()

    def _accepted_path(self):
        return self.binding.home / ACCEPTED_FILENAME

    def _accepted_persist_paths(self):
        paths = [self._accepted_path()]
        home = self.binding.home
        try:
            for path in home.iterdir():
                if path.name.startswith(".accepted.") and path.is_file():
                    paths.append(path)
        except OSError:
            pass
        return paths

    def _read_accepted_keys(self, path):
        keys = set()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return keys
        try:
            data = json.loads(raw)
        except ValueError:
            return keys
        if not isinstance(data, list):
            return keys
        for item in data:
            if (
                isinstance(item, list)
                and len(item) == 3
                and all(isinstance(part, str) for part in item)
            ):
                keys.add((item[0], item[1], item[2]))
        return keys

    def _load_accepted(self):
        keys = set(self._accepted)
        for path in self._accepted_persist_paths():
            keys.update(self._read_accepted_keys(path))
        return keys

    def _record_accepted(self, key):
        keys = self._load_accepted()
        keys.add(key)
        self._accepted = keys
        home = self.binding.home
        ensure_private_dir(home)
        payload = json.dumps([list(item) for item in sorted(keys)], ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(home), prefix=".accepted.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            chmod_private_file(Path(tmp))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return
        try:
            os.replace(tmp, self._accepted_path())
        except OSError:
            return
        try:
            chmod_private_file(self._accepted_path())
        except OSError:
            return

    def dispatch(self, params, env=None):
        params = params or {}
        op = params.get("op")
        if op == "send":
            return self.send(params, env)
        if op == "test":
            return self.test(params, env)
        raise NotifyMeError("unsupported_command", "不支持的 op")

    def send(self, params, env=None):
        condition = (params or {}).get("condition")
        if condition not in SENDABLE:
            raise NotifyMeError(
                "unsupported_condition",
                "send 只接受 answer、auth、action、severe-risk 或 done",
            )
        item_id = _required(params, "item_id")
        state = _required(params, "state")
        message = _required(params, "message")
        dry_run = bool((params or {}).get("dry_run"))
        key = (item_id, state, condition)
        if key in self._load_accepted():
            return {
                "ok": True,
                "status": "deduplicated",
                "item_id": item_id,
                "state": state,
            }
        project = project_name(env)
        title = _compose_title(condition, project)
        body = message
        effect = EFFECTS[condition]
        group = project or "Grok"
        if dry_run:
            return {
                "ok": True,
                "status": "dry_run",
                "condition": condition,
                "item_id": item_id,
                "state": state,
                "title": title,
                "body": body,
            }
        endpoint = self.binding.load()
        payload = _build_payload(endpoint, title, body, effect, group=group)
        result = self.transport.send_with_retry(endpoint, payload)
        if result.accepted:
            self._accepted.add(key)
            try:
                self._record_accepted(key)
            except Exception:
                pass
            return {
                "ok": True,
                "status": "accepted",
                "item_id": item_id,
                "state": state,
                "attempts": result.attempts,
            }
        return {
            "ok": True,
            "status": "failed",
            "item_id": item_id,
            "state": state,
            "category": result.category,
            "http_status": result.http_status,
            "attempts": result.attempts,
        }

    def test(self, params, env=None):
        dry_run = bool((params or {}).get("dry_run"))
        message = (params or {}).get("message")
        if message is None or (isinstance(message, str) and not message.strip()):
            message = "这是 Grok Notify Me 的测试通知"
        elif not isinstance(message, str):
            raise NotifyMeError("invalid_arguments", "message 必须是字符串")
        else:
            message = message.strip()
        title = TEST_TITLE
        effect = EFFECTS["test"]
        if dry_run:
            return {
                "ok": True,
                "status": "dry_run",
                "title": title,
                "body": message,
            }
        endpoint = self.binding.load()
        payload = _build_payload(endpoint, title, message, effect)
        result = self.transport.send_with_retry(endpoint, payload)
        if result.accepted:
            return {"ok": True, "status": "accepted", "attempts": result.attempts}
        return {
            "ok": True,
            "status": "failed",
            "category": result.category,
            "http_status": result.http_status,
            "attempts": result.attempts,
        }
