# -*- coding: utf-8 -*-
"""Build the zipapp and replace only the application launcher."""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from .errors import NotifyMeError
from .paths import AppPaths, directory_fact, ensure_private_dir, file_fact, forbid_host_path, path_exists, reject_symlink_components
from .protocol import PROTOCOL_VERSION, operation
from .schema import SCHEMA_V8, SCHEMA_V9
from .storage import (
    ManualClock,
    StateStore,
    SystemClock,
    backup_database,
    create_fresh,
    endpoint_from_binding_file,
    has_mutation_since,
    inspect_database,
    interprocess_lock,
    limits_from_env,
    migrate_8_to_9,
    sqlite_immediate_available,
    write_binding,
)


_SOURCES = ("codex", "grok", "cursor")
_PACKAGE_DIR = ("apps", "notify-me", "src", "notify_me_app")


def repo_root_from_here():
    here = Path(__file__).resolve()
    if here.parents[0].name == "notify_me_app":
        candidate = here.parents[4]
        if (candidate / "apps" / "notify-me").is_dir():
            return candidate
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    if completed.returncode != 0:
        raise NotifyMeError("launcher_invalid")
    return Path(completed.stdout.strip())


def package_files(root):
    directory = root.joinpath(*_PACKAGE_DIR)
    rows = []
    for path in sorted(directory.glob("*.py")):
        if path.name == "build_info.py":
            continue
        data = path.read_bytes()
        rel = "notify_me_app/" + path.name
        rows.append((rel, data, hashlib.sha256(data).hexdigest()))
    return rows


def commit_label(root):
    commit = _git(root, ["rev-parse", "HEAD"])
    status = _git(root, ["status", "--porcelain", "--", "apps/notify-me/src/notify_me_app"])
    if status.strip():
        return commit + "-dirty"
    return commit


def build_digest(label, files):
    manifest = "".join("{} {}\n".format(rel, digest) for rel, _data, digest in files)
    return hashlib.sha256((label + "\n" + manifest).encode("utf-8")).hexdigest()


def render_build_info(label, digest, files):
    lines = [
        "# -*- coding: utf-8 -*-",
        'CLI_VERSION = "1.0.0"',
        "PROTOCOL_VERSION = 1",
        'SOURCE_COMMIT = "{}"'.format(label),
        'BUILD_DIGEST = "{}"'.format(digest),
        'PYTHON_REQUIRES = ">=3.9"',
        'ARTIFACT = "zipapp"',
        "SOURCE_FILES = (",
    ]
    for rel, _data, digest in files:
        lines.append('    ("{}", "{}"),'.format(rel, digest))
    lines.append(")")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def build_zipapp_bytes(root=None):
    root = Path(root) if root is not None else repo_root_from_here()
    files = package_files(root)
    label = commit_label(root)
    digest = build_digest(label, files)
    info = render_build_info(label, digest, files)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", "from notify_me_app.cli import main\nraise SystemExit(main())\n")
        for rel, data, _digest in files:
            archive.writestr(rel, data)
        archive.writestr("notify_me_app/build_info.py", info)
    blob = b"#!/usr/bin/env python3\n" + buffer.getvalue()
    return blob, label, digest


def current_archive_bytes():
    candidate = sys.argv[0]
    if candidate and os.path.isfile(candidate) and zipfile.is_zipfile(candidate):
        with open(candidate, "rb") as handle:
            return handle.read()
    return None


def install_application(env, launcher, config_dir=None, dry_run=False, clock=None):
    if not launcher:
        raise NotifyMeError("launcher_required")
    clock = clock or _clock(env)
    launch = Path(launcher).expanduser()
    forbid_host_path(launch, env)
    reject_symlink_components(launch, "launcher_invalid")
    if launch.exists() and launch.is_symlink():
        raise NotifyMeError("launcher_invalid")
    config = None
    if config_dir:
        config = Path(config_dir).expanduser()
        forbid_host_path(config, env)
        reject_symlink_components(config, "unsafe_config_path")
    paths = AppPaths(config, launch)
    marker = _marker_path(paths, launch)
    if path_exists(marker):
        raise NotifyMeError("recovery_required")
    blob = current_archive_bytes()
    label = None
    digest = None
    if blob is None:
        blob, label, digest = build_zipapp_bytes()
    else:
        label, digest = _embedded_identity(blob)
    self_check(blob)
    if dry_run:
        return operation(
            "dry_run",
            None,
            extra={
                "launcher": str(launch),
                "source_commit": label,
                "build_digest": digest,
                "config_dir": None if config is None else str(config),
            },
        )
    if not launch.parent.exists():
        raise NotifyMeError("launcher_invalid")
    with interprocess_lock(_lock_path(paths, launch)):
        return _install_locked(env, paths, launch, config, blob, label, digest, clock)


def recover_application(env, launcher, config_dir=None, clock=None):
    if not launcher:
        raise NotifyMeError("launcher_required")
    launch = Path(launcher).expanduser()
    forbid_host_path(launch, env)
    config = Path(config_dir).expanduser() if config_dir else None
    if config is not None:
        forbid_host_path(config, env)
    paths = AppPaths(config, launch)
    marker = _marker_path(paths, launch)
    if not path_exists(marker):
        raise NotifyMeError("recovery_unavailable")
    with interprocess_lock(_lock_path(paths, launch)):
        return _recover_locked(env, paths, launch, marker, clock or _clock(env))


def migrate_binding(env, source, binding_file, config_dir, replace=False):
    if source not in _SOURCES:
        raise NotifyMeError("invalid_arguments")
    if not binding_file or not config_dir:
        raise NotifyMeError("invalid_arguments")
    chosen = Path(binding_file).expanduser()
    config = Path(config_dir).expanduser()
    forbid_host_path(chosen, env)
    forbid_host_path(config, env)
    endpoint = endpoint_from_binding_file(chosen)
    ensure_private_dir(config, env)
    paths = AppPaths(config, None)
    existed = file_fact(paths.dotenv)["status"] == "ready"
    write_binding(paths, endpoint, replace)
    return operation(
        "bound",
        None,
        extra={"source": source, "replaced": bool(existed and replace)},
    )


def self_check(blob):
    directory = tempfile.mkdtemp(prefix="notify-me-selfcheck-")
    try:
        target = os.path.join(directory, "notify-me")
        canary = os.path.join(directory, "canary-config")
        home = os.path.join(directory, "home")
        os.mkdir(home, 0o700)
        _write_new(Path(target), blob, 0o700)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": home,
            "NOTIFY_ME_FORBID_HOST_PATHS": "1",
            "NOTIFY_ME_CONFIG_DIR": canary,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = subprocess.run(
            [sys.executable, target, "version", "--json"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0 or len(completed.stdout) > 65536:
            raise NotifyMeError("launcher_invalid")
        try:
            data = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise NotifyMeError("launcher_invalid")
        if data.get("ok") is not True or data.get("protocol_version") != PROTOCOL_VERSION:
            raise NotifyMeError("launcher_invalid")
        if not data.get("source_commit") or not data.get("build_digest"):
            raise NotifyMeError("launcher_invalid")
        if os.path.exists(canary):
            raise NotifyMeError("launcher_invalid")
        return data
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _install_locked(env, paths, launch, config, blob, label, digest, clock):
    migrated = False
    created_database = False
    schema_before = None
    migrated_at = None
    if config is not None and path_exists(config):
        directory = directory_fact(config)
        if directory["status"] != "ready":
            raise NotifyMeError("config_permissions")
        probe = inspect_database(paths.state_db)
        if probe.get("schema_version") == SCHEMA_V8 and probe.get("status") == "ready":
            schema_before = SCHEMA_V8
            backup_database(paths.state_db, paths.backup)
            migrated_at = int(clock.now())
            _write_marker(paths, launch, "backed-up", migrated_at, schema_before, False, False)
            _maybe_fault(env, "after-backup")
            migrate_8_to_9(paths.state_db, clock)
            migrated = True
            _write_marker(paths, launch, "migrated", migrated_at, schema_before, False, False)
            _maybe_fault(env, "after-migrate")
        elif probe.get("status") == "missing":
            create_fresh(paths.state_db, clock)
            created_database = True
            migrated_at = int(clock.now())
            _write_marker(paths, launch, "created-database", migrated_at, None, True, False)
        elif probe.get("error_code"):
            raise NotifyMeError(probe["error_code"])
    previous = _previous_path(launch)
    previous_bytes = None
    if launch.is_file() and not launch.is_symlink():
        previous_bytes = launch.read_bytes()
        _write_new(previous, previous_bytes, 0o700)
    identity = {
        "new_sha256": hashlib.sha256(blob).hexdigest(),
        "previous_sha256": None if previous_bytes is None else hashlib.sha256(previous_bytes).hexdigest(),
    }
    # The journal is durable before the launcher bytes change. A crash on
    # either side of that replace can still see what the old and new files are.
    _write_marker(paths, launch, "replacing", migrated_at, schema_before, created_database, False, identity)
    _maybe_fault(env, "before-replace")
    _write_new(launch, blob, 0o700)
    _maybe_fault(env, "after-commit")
    _write_marker(paths, launch, "replaced", migrated_at, schema_before, created_database, True, identity)
    _maybe_fault(env, "after-replace")
    if paths.backup is not None and path_exists(paths.backup):
        os.unlink(paths.backup)
    marker = _marker_path(paths, launch)
    if path_exists(marker):
        marker.unlink()
    if path_exists(previous):
        os.unlink(previous)
    return operation(
        "installed",
        None,
        extra={
            "launcher": str(launch),
            "source_commit": label,
            "build_digest": digest,
            "migrated": migrated,
            "created_database": created_database,
            "config_dir": None if config is None else str(config),
        },
    )


def _recover_locked(env, paths, launch, marker, clock):
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raise NotifyMeError("recovery_unavailable")
    if not isinstance(data, dict):
        raise NotifyMeError("recovery_unavailable")
    code_plan = _code_plan(data, launch)
    database_restored = False
    delivery_preserved = False
    rollback = "unchanged"
    mutated = _database_mutated(paths, data)
    _maybe_recover_barrier(env, paths, clock)
    if _database_mutated(paths, data):
        mutated = True
    if mutated:
        _discard_backup(paths)
        delivery_preserved = True
        rollback = "preserved"
    elif _should_replace_database(paths, data):
        if not sqlite_immediate_available(paths.state_db) or _database_mutated(paths, data):
            # An external writer, including a schema 8 agent that does not
            # take this lock, can still change the file. Keep schema 9.
            _discard_backup(paths)
            rollback = "blocked"
        else:
            raw = paths.backup.read_bytes()
            _overwrite(paths.state_db, raw)
            _unlink_sidecars(paths.state_db)
            os.unlink(paths.backup)
            database_restored = True
            rollback = "restored"
    elif data.get("created_database") and paths.state_db is not None and path_exists(paths.state_db):
        if _event_count(paths.state_db) == 0 and sqlite_immediate_available(paths.state_db):
            _unlink_database(paths.state_db)
            database_restored = True
            rollback = "restored"
        elif _event_count(paths.state_db) != 0:
            delivery_preserved = True
            rollback = "preserved"
        else:
            rollback = "blocked"
    elif paths.backup is not None and path_exists(paths.backup):
        probe = inspect_database(paths.state_db) if paths.state_db is not None and path_exists(paths.state_db) else {}
        if probe.get("schema_version") == SCHEMA_V8:
            os.unlink(paths.backup)
    code_restored = _apply_code_plan(code_plan, launch)
    marker.unlink()
    probe = inspect_database(paths.state_db) if paths.state_db is not None and path_exists(paths.state_db) else None
    return operation(
        "recovered",
        None,
        extra={
            "code_restored": code_restored,
            "database_restored": database_restored,
            "database_rollback": rollback,
            "delivery_preserved": delivery_preserved,
            "schema_version": None if probe is None else probe.get("schema_version"),
        },
    )


def _database_mutated(paths, data):
    if paths.state_db is None or not path_exists(paths.state_db) or data.get("migrated_at") is None:
        return False
    return has_mutation_since(paths.state_db, int(data["migrated_at"]))


def _should_replace_database(paths, data):
    if data.get("created_database"):
        return False
    if paths.backup is None or paths.state_db is None:
        return False
    if not path_exists(paths.backup) or not path_exists(paths.state_db):
        return False
    probe = inspect_database(paths.state_db)
    return probe.get("schema_version") == SCHEMA_V9


def _discard_backup(paths):
    if paths.backup is not None and path_exists(paths.backup):
        os.unlink(paths.backup)


def _maybe_recover_barrier(env, paths, clock):
    """Test-only write injected after the first mutation read and before restore."""

    if env.get("NOTIFY_ME_TEST_MODE") != "1":
        return
    if env.get("NOTIFY_ME_TEST_RECOVER_BARRIER") != "admit-accepted":
        return
    if paths.state_db is None or not path_exists(paths.state_db):
        return
    store = StateStore(paths, clock, limits_from_env(env))
    now = int(clock.now())
    admitted = store.admit(
        "aiusage",
        "barrier-accept",
        "P0",
        "Barrier",
        "Accepted",
        now,
        None,
        False,
    )
    if admitted.get("outcome") != "sending" or not admitted.get("claim"):
        raise NotifyMeError("state_conflict")
    store.finalize(admitted["claim"], True, 1, 200, None, False)


def _code_plan(data, launch):
    """Describe how to put the launcher back. Does not write."""

    previous = _previous_path(launch)
    new_sha = data.get("new_sha256")
    prev_sha = data.get("previous_sha256")
    if new_sha is None and not data.get("launcher_replaced"):
        return ("noop", None)
    current = _file_sha256(launch)
    if new_sha is not None:
        if current == prev_sha or (prev_sha is None and current is None):
            return ("cleanup-previous", None)
        if current != new_sha:
            raise NotifyMeError("recovery_unavailable")
        if prev_sha is None:
            if path_exists(previous):
                raise NotifyMeError("recovery_unavailable")
            return ("unlink", None)
        if _file_sha256(previous) != prev_sha:
            raise NotifyMeError("recovery_unavailable")
        return ("restore", previous.read_bytes())
    if data.get("launcher_replaced") and path_exists(previous):
        return ("restore", previous.read_bytes())
    return ("noop", None)


def _apply_code_plan(plan, launch):
    action, payload = plan
    previous = _previous_path(launch)
    if action == "cleanup-previous":
        if path_exists(previous):
            os.unlink(previous)
        return False
    if action == "unlink":
        os.chmod(launch, 0o700)
        os.unlink(launch)
        return True
    if action == "restore":
        _overwrite(launch, payload)
        os.chmod(launch, 0o700)
        if path_exists(previous):
            os.unlink(previous)
        return True
    return False


def _file_sha256(path):
    if not path_exists(path) or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_marker(paths, launch, phase, migrated_at, schema_before, created_database, launcher_replaced, identity=None):
    marker = _marker_path(paths, launch)
    payload = {
        "phase": phase,
        "migrated_at": migrated_at,
        "schema_before": schema_before,
        "created_database": bool(created_database),
        "launcher_replaced": bool(launcher_replaced),
    }
    if identity is not None:
        payload["new_sha256"] = identity.get("new_sha256")
        payload["previous_sha256"] = identity.get("previous_sha256")
    _write_new(marker, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"), 0o600)


def _marker_path(paths, launch):
    if paths.config_dir is not None and path_exists(paths.config_dir) and paths.marker is not None:
        return paths.marker
    return launch.parent / "install-recovery.json"


def _lock_path(paths, launch):
    if paths.config_dir is not None and path_exists(paths.config_dir) and paths.lock is not None:
        return paths.lock
    return launch.parent / ".notify-me-install.lock"


def _previous_path(launch):
    return Path(str(launch) + ".previous")


def _maybe_fault(env, name):
    if env.get("NOTIFY_ME_TEST_MODE") == "1" and env.get("NOTIFY_ME_TEST_FAULT") == name:
        raise NotifyMeError("install_interrupted")


def _clock(env):
    if env.get("NOTIFY_ME_TEST_MODE") == "1" and env.get("NOTIFY_ME_TEST_NOW"):
        return ManualClock(float(env["NOTIFY_ME_TEST_NOW"]))
    return SystemClock()


def _git(root, args):
    completed = subprocess.run(
        ["git"] + args,
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    if completed.returncode != 0:
        raise NotifyMeError("launcher_invalid")
    return completed.stdout.strip()


def _embedded_identity(blob):
    text = blob.split(b"\n", 1)[1]
    with zipfile.ZipFile(io.BytesIO(text)) as archive:
        raw = archive.read("notify_me_app/build_info.py").decode("utf-8")
    commit = None
    digest = None
    for line in raw.splitlines():
        if line.startswith("SOURCE_COMMIT = "):
            commit = line.split("=", 1)[1].strip().strip('"')
        if line.startswith("BUILD_DIGEST = "):
            digest = line.split("=", 1)[1].strip().strip('"')
    if not commit or not digest:
        raise NotifyMeError("launcher_invalid")
    return commit, digest


def _write_new(path, data, mode):
    parent = path.parent
    descriptor, temporary = tempfile.mkstemp(prefix=".notify-me-", dir=str(parent))
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, str(path))
        temporary = None
        os.chmod(str(path), mode)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _overwrite(path, data):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(str(path), stat.S_IMODE(os.lstat(str(path)).st_mode) & 0o700 or 0o600)


def _unlink_sidecars(path):
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and not sidecar.is_symlink():
            sidecar.unlink()


def _unlink_database(path):
    _unlink_sidecars(path)
    if path.exists() and not path.is_symlink():
        path.unlink()


def _event_count(path):
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute("SELECT COUNT(*) FROM application_events").fetchone()[0]
    except sqlite3.Error:
        return 1
    finally:
        connection.close()
