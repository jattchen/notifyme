import errno
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import threading
from pathlib import Path

from .bark import BarkTransport
from .binding import Binding
from .errors import NotifyMeError
from .paths import chmod_private_file

_THREAD_SEND_LOCK = threading.RLock()
_IN_FLIGHT = set()
_IN_FLIGHT_COND = threading.Condition(_THREAD_SEND_LOCK)
ACCEPTED_LOCK_FILENAME = "accepted.lock"
IN_FLIGHT_PREFIX = ".inflight."


def _is_private_regular_file(path):
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not (stat.S_IMODE(info.st_mode) & 0o077)


class _AcceptedSendLock:
    def __init__(self, home):
        self._home = home
        self._fd = None

    def __enter__(self):
        _THREAD_SEND_LOCK.acquire()
        try:
            home = self._home
            if home.exists():
                if stat.S_IMODE(home.stat().st_mode) & 0o077:
                    raise NotifyMeError("insecure_binding", "Bark 状态目录权限过宽")
                path = home / ACCEPTED_LOCK_FILENAME
                if path.is_symlink():
                    raise NotifyMeError("insecure_binding", "accepted.lock 不能是符号链接")
                flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                try:
                    self._fd = os.open(str(path), flags, 0o600)
                except OSError as exc:
                    if exc.errno == errno.ELOOP or path.is_symlink():
                        raise NotifyMeError(
                            "insecure_binding",
                            "accepted.lock 不能是符号链接",
                        )
                    raise
                try:
                    opened = os.fstat(self._fd)
                    if not stat.S_ISREG(opened.st_mode):
                        raise NotifyMeError(
                            "insecure_binding",
                            "accepted.lock 不是普通文件",
                        )
                    os.fchmod(self._fd, stat.S_IRUSR | stat.S_IWUSR)
                except NotifyMeError:
                    raise
                except OSError:
                    pass
                fcntl.flock(self._fd, fcntl.LOCK_EX)
        except Exception:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            _THREAD_SEND_LOCK.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        _THREAD_SEND_LOCK.release()
        return False


def _in_flight_filename(key):
    raw = json.dumps(list(key), ensure_ascii=False, separators=(",", ":"))
    return IN_FLIGHT_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _open_inflight_path(path, create):
    if path.is_symlink():
        raise NotifyMeError("insecure_binding", ".inflight 不能是符号链接")
    flags = os.O_RDWR | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(str(path), flags, 0o600)
    except OSError as exc:
        if exc.errno in (errno.ENOENT,) and not create:
            return None
        if exc.errno == errno.ELOOP or path.is_symlink():
            raise NotifyMeError("insecure_binding", ".inflight 不能是符号链接")
        raise
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise NotifyMeError("insecure_binding", ".inflight 不是普通文件")
        if create:
            try:
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


class _InFlightReservation:
    def __init__(self, home, key):
        self._path = home / _in_flight_filename(key)
        self._fd = None

    def try_acquire(self):
        fd = _open_inflight_path(self._path, create=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            if isinstance(exc, BlockingIOError) or exc.errno in (
                errno.EAGAIN,
                errno.EACCES,
                errno.EWOULDBLOCK,
            ):
                return False
            raise
        self._fd = fd
        return True

    def wait(self):
        fd = _open_inflight_path(self._path, create=False)
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def release(self):
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            self._path.unlink()
        except OSError:
            pass


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
_SKIP_ACCEPTED = object()


def _accepted_key(item):
    if isinstance(item, dict):
        if "item_id" not in item or "state" not in item or "condition" not in item:
            return None
        workspace = item.get("workspace", "")
        item_id = item["item_id"]
        state = item["state"]
        condition = item["condition"]
        if not all(
            isinstance(part, str)
            for part in (workspace, item_id, state, condition)
        ):
            return None
        return (workspace, item_id, state, condition)
    if not (isinstance(item, list) and all(isinstance(part, str) for part in item)):
        return None
    if len(item) == 4:
        return (item[0], item[1], item[2], item[3])
    if len(item) == 3:
        return ("", item[0], item[1], item[2])
    return _SKIP_ACCEPTED


def _accepted_record(key):
    workspace, item_id, state, condition = key
    return {
        "workspace": workspace,
        "item_id": item_id,
        "state": state,
        "condition": condition,
    }


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
            "workspace": {
                "type": "string",
                "description": (
                    "Absolute project root for this send. Use it when the MCP "
                    "process has no GROK_WORKSPACE_ROOT or CLAUDE_PROJECT_DIR, "
                    "so two projects with the same item stay distinct."
                ),
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


def _dry_run(params):
    value = (params or {}).get("dry_run")
    if value is None or value is False or value == 0:
        return False
    if value is True:
        return True
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("false", "0"):
            return False
        if normalized == "true":
            return True
    raise NotifyMeError("invalid_arguments", "dry_run 必须是布尔值")


def _resolved_path(start):
    if not start:
        return None
    try:
        return Path(start).expanduser().resolve()
    except OSError:
        return None


def _workspace_root(start, home):
    path = _resolved_path(start)
    if path is None or path == home:
        return None
    current = path
    while True:
        if (current / ".git").exists() and current != home:
            return current
        if current.parent == current:
            break
        current = current.parent
    try:
        if path.is_dir():
            return path
    except OSError:
        return None
    return None


def _env_with_call_workspace(params, env):
    value = (params or {}).get("workspace")
    if not isinstance(value, str):
        return env
    stripped = value.strip()
    if not stripped:
        return env
    merged = dict(os.environ if env is None else env)
    merged["GROK_WORKSPACE_ROOT"] = stripped
    return merged


def _workspace_from_env(env):
    env = env or os.environ
    home = Path.home().resolve()
    explicit = env.get("GROK_WORKSPACE_ROOT") or env.get("CLAUDE_PROJECT_DIR")
    if explicit:
        return _workspace_root(explicit, home)
    return None


def project_name(env=None):
    root = _workspace_from_env(env)
    if root is None:
        return None
    return root.name or None


def workspace_identity(env=None):
    root = _workspace_from_env(env)
    if root is None:
        return ""
    return str(root)


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
                if path.name.startswith(".accepted.") and _is_private_regular_file(path):
                    paths.append(path)
        except OSError:
            pass
        return paths

    def _read_accepted_keys(self, path, fail_closed=False):
        keys = set()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return keys
        except (OSError, UnicodeError):
            if fail_closed:
                raise NotifyMeError(
                    "invalid_accepted",
                    "accepted.json 无法作为记录列表读取",
                )
            return keys
        try:
            data = json.loads(raw)
        except ValueError:
            if fail_closed:
                raise NotifyMeError(
                    "invalid_accepted",
                    "accepted.json 无法作为记录列表读取",
                )
            return keys
        if not isinstance(data, list):
            if fail_closed:
                raise NotifyMeError(
                    "invalid_accepted",
                    "accepted.json 无法作为记录列表读取",
                )
            return keys
        for item in data:
            key = _accepted_key(item)
            if key is _SKIP_ACCEPTED:
                continue
            if key is not None:
                keys.add(key)
                continue
            if fail_closed:
                raise NotifyMeError(
                    "invalid_accepted",
                    "accepted.json 无法作为记录列表读取",
                )
        return keys

    def _load_accepted(self):
        keys = set(self._accepted)
        accepted_path = self._accepted_path()
        corrupt = False
        for path in self._accepted_persist_paths():
            if path == accepted_path:
                try:
                    keys.update(self._read_accepted_keys(path, fail_closed=True))
                except NotifyMeError:
                    corrupt = True
            else:
                keys.update(self._read_accepted_keys(path))
        return keys, corrupt

    def _record_accepted(self, key):
        keys, _corrupt = self._load_accepted()
        keys.add(key)
        self._accepted = keys
        home = self.binding.home
        payload = json.dumps(
            [_accepted_record(item) for item in sorted(keys)],
            ensure_ascii=False,
        )
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
            return False
        try:
            os.replace(tmp, self._accepted_path())
        except OSError:
            return False
        try:
            chmod_private_file(self._accepted_path())
        except OSError:
            pass
        return True

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
        dry_run = _dry_run(params)
        env = _env_with_call_workspace(params, env)
        key = (workspace_identity(env), item_id, state, condition)
        project = project_name(env)
        title = _compose_title(condition, project)
        body = message
        effect = EFFECTS[condition]
        group = project or "Grok"
        reserved = False
        endpoint = None if dry_run else self.binding.load()
        reservation = _InFlightReservation(self.binding.home, key)
        try:
            while True:
                with _AcceptedSendLock(self.binding.home):
                    accepted_keys, accepted_corrupt = self._load_accepted()
                    if key in accepted_keys:
                        return {
                            "ok": True,
                            "status": "deduplicated",
                            "item_id": item_id,
                            "state": state,
                        }
                    if accepted_corrupt:
                        raise NotifyMeError(
                            "invalid_accepted",
                            "accepted.json 无法作为记录列表读取",
                        )
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
                    if key not in _IN_FLIGHT and reservation.try_acquire():
                        _IN_FLIGHT.add(key)
                        reserved = True
                if reserved:
                    break
                if key in _IN_FLIGHT:
                    with _IN_FLIGHT_COND:
                        if key in _IN_FLIGHT:
                            _IN_FLIGHT_COND.wait()
                else:
                    reservation.wait()
            payload = _build_payload(endpoint, title, body, effect, group=group)
            result = self.transport.send_with_retry(endpoint, payload)
            if result.accepted:
                persisted = False
                with _AcceptedSendLock(self.binding.home):
                    self._accepted.add(key)
                    try:
                        persisted = bool(self._record_accepted(key))
                    except Exception:
                        persisted = False
                accepted = {
                    "ok": True,
                    "status": "accepted",
                    "item_id": item_id,
                    "state": state,
                    "attempts": result.attempts,
                }
                if not persisted:
                    accepted["persist"] = "degraded"
                return accepted
            return {
                "ok": False,
                "status": "failed",
                "item_id": item_id,
                "state": state,
                "category": result.category,
                "http_status": result.http_status,
                "attempts": result.attempts,
            }
        finally:
            if reserved:
                with _IN_FLIGHT_COND:
                    _IN_FLIGHT.discard(key)
                    _IN_FLIGHT_COND.notify_all()
                reservation.release()

    def test(self, params, env=None):
        dry_run = _dry_run(params)
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
            "ok": False,
            "status": "failed",
            "category": result.category,
            "http_status": result.http_status,
            "attempts": result.attempts,
        }
