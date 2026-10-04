# -*- coding: utf-8 -*-
"""JSON command entry. version returns before any path or filesystem call."""

import sys
import traceback

from .application_push import command_cancel, command_drain, command_push, command_push_status, command_status
from .build_info import (
    ARTIFACT,
    BUILD_DIGEST,
    CLI_VERSION,
    PROTOCOL_VERSION,
    PYTHON_REQUIRES,
    SOURCE_COMMIT,
    SOURCE_FILES,
)
from .errors import NotifyMeError
from .installation import install_application, migrate_binding, recover_application
from .protocol import CAPABILITIES, dumps, error_payload
from .storage import ManualClock, SystemClock, limits_from_env
from .transport import BarkTransport


_FLAGS = {"json", "enqueue-only", "dry-run", "replace-binding", "force"}
_COMMANDS = {
    "version": {"json", "protocol-version"},
    "status": {"source", "json", "protocol-version"},
    "push": {
        "source",
        "event-id",
        "priority",
        "title",
        "body",
        "enqueue-only",
        "created-at",
        "expires-at",
        "json",
        "protocol-version",
    },
    "push-status": {"source", "event-id", "json", "protocol-version"},
    "push-cancel": {"source", "event-id", "json", "protocol-version"},
    "push-drain": {"source", "event-id", "max-items", "budget-ms", "force", "json", "protocol-version"},
    "install": {"launcher", "config-dir", "dry-run", "json", "protocol-version"},
    "upgrade": {"launcher", "config-dir", "dry-run", "json", "protocol-version"},
    "migrate-binding": {"source", "binding-file", "config-dir", "replace-binding", "json", "protocol-version"},
    "recover": {"launcher", "config-dir", "json", "protocol-version"},
}


def parse_args(argv):
    if not argv:
        raise NotifyMeError("invalid_arguments")
    command = argv[0]
    if command not in _COMMANDS:
        raise NotifyMeError("unsupported_command")
    options = {}
    index = 1
    while index < len(argv):
        token = argv[index]
        if not token.startswith("--") or len(token) < 3:
            raise NotifyMeError("invalid_arguments")
        name = token[2:]
        if name not in _COMMANDS[command]:
            raise NotifyMeError("invalid_arguments")
        if name in _FLAGS:
            options[name] = True
            index += 1
            continue
        if index + 1 >= len(argv):
            raise NotifyMeError("invalid_arguments")
        options[name] = argv[index + 1]
        index += 2
    return command, options


def version_payload(options):
    files = []
    for item in SOURCE_FILES:
        files.append({"path": item[0], "sha256": item[1]})
    return {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "status": "ready",
        "cli_version": CLI_VERSION,
        "source_commit": SOURCE_COMMIT,
        "build_digest": BUILD_DIGEST,
        "python_requires": PYTHON_REQUIRES,
        "python_version": "{}.{}.{}".format(sys.version_info[0], sys.version_info[1], sys.version_info[2]),
        "artifact": ARTIFACT,
        "supported_state_schemas": [8, 9],
        "writable_state_schema": 9,
        "capabilities": CAPABILITIES,
        "source_files": files,
    }


def main(argv=None, env=None, transport=None, clock=None):
    if sys.version_info[0] < 3 or (sys.version_info[0] == 3 and sys.version_info[1] < 9):
        sys.stdout.write(dumps(error_payload("python_unsupported")))
        return 1
    if argv is None:
        argv = sys.argv[1:]
    if env is None:
        env = os_environ()
    try:
        command, options = parse_args(list(argv))
        _check_protocol(options)
        if command == "version":
            payload = version_payload(options)
        elif command == "push-drain" and "source" not in options:
            raise NotifyMeError("source_required")
        elif command == "push-drain" and options.get("force"):
            raise NotifyMeError("invalid_arguments")
        else:
            payload = _dispatch(command, options, env, transport, clock)
    except NotifyMeError as exc:
        payload = error_payload(exc.code)
    except Exception:
        if env.get("NOTIFY_ME_TEST_MODE") == "1":
            traceback.print_exc(file=sys.stderr)
        payload = error_payload("internal_error")
    sys.stdout.write(dumps(payload))
    return 0 if payload.get("ok") else 1


def _dispatch(command, options, env, transport, clock):
    clock = _clock(env, clock)
    transport = transport or BarkTransport(timeout=5.0)
    if command == "status":
        return command_status(env, clock, options.get("source"))
    if command == "push":
        return command_push(
            env,
            clock,
            transport,
            _required(options, "source"),
            _required(options, "event-id"),
            _required(options, "priority"),
            _required(options, "title"),
            _required(options, "body"),
            options.get("created-at"),
            options.get("expires-at"),
            bool(options.get("enqueue-only")),
        )
    if command == "push-status":
        return command_push_status(env, clock, _required(options, "source"), _required(options, "event-id"))
    if command == "push-cancel":
        return command_cancel(env, clock, _required(options, "source"), _required(options, "event-id"))
    if command == "push-drain":
        return command_drain(
            env,
            clock,
            transport,
            _required(options, "source"),
            options.get("event-id"),
            _bounded_int(options, "max-items", 1, 1, 32),
            _bounded_int(options, "budget-ms", 15000, 0, 60000),
        )
    if command in ("install", "upgrade"):
        return install_application(
            env,
            options.get("launcher"),
            options.get("config-dir"),
            bool(options.get("dry-run")),
            clock,
        )
    if command == "migrate-binding":
        return migrate_binding(
            env,
            options.get("source"),
            options.get("binding-file"),
            options.get("config-dir"),
            bool(options.get("replace-binding")),
        )
    if command == "recover":
        return recover_application(env, options.get("launcher"), options.get("config-dir"), clock)
    raise NotifyMeError("unsupported_command")


def _check_protocol(options):
    if "protocol-version" not in options:
        return
    raw = options["protocol-version"]
    if isinstance(raw, bool) or not isinstance(raw, str) or not raw.isdigit():
        raise NotifyMeError("invalid_arguments")
    if int(raw) != PROTOCOL_VERSION:
        raise NotifyMeError("protocol_mismatch")


def _required(options, name):
    value = options.get(name)
    if not isinstance(value, str) or not value:
        raise NotifyMeError("invalid_arguments")
    return value


def _bounded_int(options, name, default, minimum, maximum):
    if name not in options:
        return default
    raw = options[name]
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise NotifyMeError("invalid_arguments")
    try:
        value = int(raw, 10)
    except ValueError:
        raise NotifyMeError("invalid_arguments")
    if value < minimum:
        raise NotifyMeError("invalid_arguments")
    if value > maximum:
        return maximum
    return value


def _clock(env, clock):
    if clock is not None:
        return clock
    if env.get("NOTIFY_ME_TEST_MODE") == "1" and env.get("NOTIFY_ME_TEST_NOW"):
        try:
            return ManualClock(float(env["NOTIFY_ME_TEST_NOW"]))
        except (TypeError, ValueError):
            return SystemClock()
    return SystemClock()


def os_environ():
    import os

    return os.environ


# Imported for tests that want the active limits helper beside the clock.
_LIMITS = limits_from_env
