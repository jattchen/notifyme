# -*- coding: utf-8 -*-
"""SQLite state for the application CLI.

status and push-status open the database read-only. They do not create the
directory, the database, sidecars, or a schema migration. Writes happen only
on push, cancel, drain, and explicit upgrade.
"""

import json
import os
import secrets
import sqlite3
import stat
import urllib.parse
from contextlib import contextmanager

from .configuration import (
    DEFAULT_PRIORITY_EFFECTS,
    ICON_URL,
    LEASE_SECONDS,
    application_identity,
    backoff_seconds,
    bark_fields,
    content_fingerprint,
    fingerprint,
    validate_effect,
    validate_priority,
)
from .errors import NotifyMeError
from .paths import directory_fact, file_fact, reject_symlink_components
from .protocol import metadata, normalize_legacy, publish_event_error
from .schema import (
    APPLICATION_EVENTS_STATUS_SQL,
    APPLICATION_EVENTS_V9_SQL,
    APPLICATION_OUTBOX_DUE_SQL,
    APPLICATION_OUTBOX_V9_SQL,
    LEGACY_SCHEMA_V8_CHECKSUM,
    LEGACY_SCHEMA_V8_SQL,
    MIGRATIONS_SQL,
    PRIORITY_EFFECTS_SQL,
    SCHEMA_V8,
    SCHEMA_V9,
    SCHEMA_V9_CHECKSUM,
    SETTINGS_SQL,
)
from .transport import BarkEndpoint


_ACTIVE = ("queued", "sending")
_TERMINAL = ("accepted", "failed", "expired", "cancelled")


class Limits(object):
    def __init__(self, active_source, active_total, rows_source, rows_total, retention, lease):
        self.active_source = active_source
        self.active_total = active_total
        self.rows_source = rows_source
        self.rows_total = rows_total
        self.retention = retention
        self.lease = lease

    @classmethod
    def defaults(cls):
        from .configuration import (
            MAX_ACTIVE_PER_SOURCE,
            MAX_ACTIVE_TOTAL,
            MAX_ROWS_PER_SOURCE,
            MAX_ROWS_TOTAL,
            RETENTION_SECONDS,
        )

        return cls(
            MAX_ACTIVE_PER_SOURCE,
            MAX_ACTIVE_TOTAL,
            MAX_ROWS_PER_SOURCE,
            MAX_ROWS_TOTAL,
            RETENTION_SECONDS,
            LEASE_SECONDS,
        )


def limits_from_env(env):
    base = Limits.defaults()
    if env is None or env.get("NOTIFY_ME_TEST_MODE") != "1":
        return base

    def pick(name, default):
        raw = env.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        if value < 1:
            return default
        return value

    return Limits(
        pick("NOTIFY_ME_TEST_MAX_ACTIVE_SOURCE", base.active_source),
        pick("NOTIFY_ME_TEST_MAX_ACTIVE", base.active_total),
        pick("NOTIFY_ME_TEST_MAX_ROWS_SOURCE", base.rows_source),
        pick("NOTIFY_ME_TEST_MAX_ROWS", base.rows_total),
        pick("NOTIFY_ME_TEST_RETENTION_SECONDS", base.retention),
        pick("NOTIFY_ME_TEST_LEASE_SECONDS", base.lease),
    )


class SystemClock(object):
    def now(self):
        import time

        return time.time()

    def monotonic(self):
        import time

        return time.monotonic()


class ManualClock(object):
    def __init__(self, now):
        self._now = float(now)
        self._mono = 0.0

    def now(self):
        return self._now

    def monotonic(self):
        return self._mono

    def advance(self, seconds):
        self._now += float(seconds)
        self._mono += float(seconds)


def _whole(clock):
    return int(clock.now())


def _ro_uri(path):
    return "file:{}?mode=ro".format(urllib.parse.quote(str(path)))


def _safe_error(value):
    return publish_event_error(value)


def connect_readonly(path):
    reject_symlink_components(path, "unsafe_state_path")
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise NotifyMeError("not_initialized")
    except OSError:
        raise NotifyMeError("unsafe_state_path")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise NotifyMeError("unsafe_state_path")
    try:
        connection = sqlite3.connect(_ro_uri(path), uri=True, timeout=2.0)
    except sqlite3.Error:
        raise NotifyMeError("state_database_unavailable")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=2000")
    return connection


def connect_write(path):
    reject_symlink_components(path, "unsafe_state_path")
    parent = directory_fact(path.parent)
    if parent["status"] != "ready":
        raise NotifyMeError("config_permissions" if parent["status"] == "unsafe" else "not_initialized")
    fact = file_fact(path)
    if fact["status"] == "missing":
        raise NotifyMeError("not_initialized")
    if fact["status"] != "ready":
        raise NotifyMeError("state_permissions")
    try:
        connection = sqlite3.connect(str(path), timeout=5.0)
    except sqlite3.Error:
        raise NotifyMeError("state_database_unavailable")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


class _InterprocessLock(object):
    """One non-blocking lock shared by application writes and database rollback.

    Closing any descriptor releases a POSIX flock for the whole process, so
    nested acquires share one descriptor and a depth count. Schema 8 agent
    programs do not take this lock; callers that see SQLite itself busy must
    keep the current database instead of copying an older file over it.
    """

    _held = {}

    def __init__(self, path):
        self.path = os.path.abspath(str(path))

    def __enter__(self):
        current = _InterprocessLock._held.get(self.path)
        if current is not None:
            current[1] += 1
            return self
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            os.close(descriptor)
            raise
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(descriptor)
            raise NotifyMeError("lock_busy")
        _InterprocessLock._held[self.path] = [descriptor, 1]
        return self

    def __exit__(self, exc_type, exc, tb):
        current = _InterprocessLock._held.get(self.path)
        if current is None:
            return False
        current[1] -= 1
        if current[1] <= 0:
            _InterprocessLock._held.pop(self.path, None)
            os.close(current[0])
        return False


def interprocess_lock(path):
    return _InterprocessLock(path)


def sqlite_immediate_available(path):
    """True when this process can start a write transaction immediately."""

    try:
        connection = sqlite3.connect(str(path), timeout=0.0)
    except sqlite3.Error:
        return False
    try:
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
        return True
    except sqlite3.Error:
        return False
    finally:
        connection.close()


@contextmanager
def _transaction(path):
    lock_path = os.path.join(os.path.dirname(os.path.abspath(str(path))), "install.lock")
    with interprocess_lock(lock_path):
        connection = connect_write(path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()


def _table_names(connection):
    return {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(connection, table):
    try:
        return {row[1] for row in connection.execute("PRAGMA table_info({})".format(table))}
    except sqlite3.Error:
        return set()


def _schema_probe(connection):
    names = _table_names(connection)
    if "schema_migrations" not in names:
        return {"status": "unsupported", "schema_version": None, "writable": False, "error_code": "state_schema_unsupported"}
    try:
        version = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
        checksum_row = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?", (version,)
        ).fetchone()
    except sqlite3.Error:
        return {"status": "unsupported", "schema_version": None, "writable": False, "error_code": "state_schema_unsupported"}
    checksum = checksum_row[0] if checksum_row else None
    if version == SCHEMA_V9 and checksum == SCHEMA_V9_CHECKSUM:
        required = {
            "notification_id",
            "source_key",
            "event_key",
            "payload_fingerprint",
            "status",
            "expires_at",
            "accepted_at",
            "cancel_requested",
            "outcome_uncertain",
        }
        if not required <= _columns(connection, "application_events"):
            return {"status": "unsupported", "schema_version": version, "writable": False, "error_code": "state_schema_unsupported"}
        return {"status": "ready", "schema_version": SCHEMA_V9, "writable": True, "error_code": None}
    if version == SCHEMA_V8 and checksum == LEGACY_SCHEMA_V8_CHECKSUM:
        return {"status": "ready", "schema_version": SCHEMA_V8, "writable": False, "error_code": None}
    return {
        "status": "unsupported",
        "schema_version": version,
        "writable": False,
        "error_code": "state_schema_unsupported",
    }


def inspect_database(path):
    """Read-only schema probe. Missing files stay missing."""

    fact = file_fact(path)
    if fact["status"] == "missing":
        return {
            "status": "missing",
            "schema_version": None,
            "writable": False,
            "private": None,
            "integrity": None,
            "error_code": None,
        }
    if fact["status"] != "ready":
        return {
            "status": "unsafe",
            "schema_version": None,
            "writable": False,
            "private": False,
            "integrity": None,
            "error_code": "state_permissions",
        }
    try:
        connection = connect_readonly(path)
    except NotifyMeError as exc:
        return {
            "status": "unsupported",
            "schema_version": None,
            "writable": False,
            "private": True,
            "integrity": None,
            "error_code": exc.code,
        }
    try:
        try:
            probe = _schema_probe(connection)
            integrity = None
            try:
                row = connection.execute("PRAGMA quick_check").fetchone()
                if row is not None and str(row[0]).lower() == "ok":
                    integrity = "ok"
                else:
                    integrity = "unavailable"
            except sqlite3.Error:
                integrity = "unavailable"
            probe["private"] = True
            probe["integrity"] = integrity
            return probe
        except sqlite3.Error:
            return {
                "status": "unsupported",
                "schema_version": None,
                "writable": False,
                "private": True,
                "integrity": None,
                "error_code": "state_database_unavailable",
            }
    finally:
        connection.close()


def _seed_effects(connection, now):
    for priority, effect in DEFAULT_PRIORITY_EFFECTS.items():
        connection.execute(
            "INSERT INTO priority_effects(priority, effect_json, updated_at) VALUES (?, ?, ?)",
            (
                priority,
                json.dumps(effect, ensure_ascii=False, sort_keys=True) if effect is not None else None,
                now,
            ),
        )


def create_fresh(path, clock):
    """Create schema 9. The parent directory must already exist. Never migrates."""

    if path.exists() or path.is_symlink():
        raise NotifyMeError("state_database_exists")
    parent = directory_fact(path.parent)
    if parent["status"] != "ready":
        raise NotifyMeError("config_permissions" if parent["status"] == "unsafe" else "not_initialized")
    reject_symlink_components(path, "unsafe_state_path")
    descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    now = _whole(clock)
    connection = sqlite3.connect(str(path), timeout=5.0)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in (
            MIGRATIONS_SQL,
            SETTINGS_SQL,
            PRIORITY_EFFECTS_SQL,
            APPLICATION_EVENTS_V9_SQL,
            APPLICATION_EVENTS_STATUS_SQL,
            APPLICATION_OUTBOX_V9_SQL,
            APPLICATION_OUTBOX_DUE_SQL,
        ):
            connection.execute(statement)
        _seed_effects(connection, now)
        connection.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES ('scope_salt', ?, ?)",
            (json.dumps(secrets.token_hex(32)), now),
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
            (SCHEMA_V9, SCHEMA_V9_CHECKSUM, now),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    connection.close()
    os.chmod(path, 0o600)


def backup_database(source, destination):
    """Consistent snapshot. Caller decides whether a later rollback may use it."""

    if destination.exists() or destination.is_symlink():
        raise NotifyMeError("state_backup_failed")
    reject_symlink_components(destination, "unsafe_state_path")
    probe = sqlite3.connect(str(source), timeout=5.0)
    temporary = str(destination) + ".partial"
    try:
        if os.path.lexists(temporary):
            raise NotifyMeError("state_backup_failed")
        copied = sqlite3.connect(temporary, timeout=5.0)
        try:
            probe.backup(copied)
            copied.commit()
        finally:
            copied.close()
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except NotifyMeError:
        raise
    except (sqlite3.Error, OSError):
        raise NotifyMeError("state_backup_failed")
    finally:
        probe.close()
        if os.path.lexists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass
    os.chmod(destination, 0o600)


def _mapping(row):
    """Copy a sqlite3.Row. Row supports keys but has no dict.get."""

    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _migration_fail_after_inserts():
    """Test-only interrupt after N event inserts. Ignored unless test mode is on."""

    if os.environ.get("NOTIFY_ME_TEST_MODE") != "1":
        return None
    raw = os.environ.get("NOTIFY_ME_TEST_MIGRATION_FAIL_AFTER")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 1:
        return None
    return value


def migrate_8_to_9(path, clock):
    """Rewrite only application delivery tables. Settings and agent rows stay."""

    now = _whole(clock)
    connection = sqlite3.connect(str(path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        probe = _schema_probe(connection)
        if probe["schema_version"] == SCHEMA_V9 and probe["status"] == "ready":
            connection.rollback()
            return {"migrated": False, "schema_version": SCHEMA_V9}
        if not (probe["schema_version"] == SCHEMA_V8 and probe["status"] == "ready"):
            connection.rollback()
            raise NotifyMeError(probe["error_code"] or "state_schema_unsupported")
        events = [
            _mapping(row)
            for row in connection.execute(
                "SELECT notification_id, source_key, event_key, priority, effect_fingerprint, "
                "status, created_at, updated_at, attempts, http_status, last_error "
                "FROM application_events"
            )
        ]
        outbox = {
            row["notification_id"]: _mapping(row)
            for row in connection.execute(
                "SELECT notification_id, payload_json, next_attempt_at, expires_at, attempts, "
                "lease_token, lease_until, created_at, updated_at FROM application_outbox"
            )
        }
        for event in events:
            normalize_legacy(event["status"], event["last_error"], outbox.get(event["notification_id"]), now)
        connection.execute("DROP TABLE application_outbox")
        connection.execute("DROP TABLE application_events")
        connection.execute(APPLICATION_EVENTS_V9_SQL)
        connection.execute(APPLICATION_EVENTS_STATUS_SQL)
        connection.execute(APPLICATION_OUTBOX_V9_SQL)
        connection.execute(APPLICATION_OUTBOX_DUE_SQL)
        inserted = 0
        fail_after = _migration_fail_after_inserts()
        for event in events:
            queued = outbox.get(event["notification_id"])
            state, uncertain = normalize_legacy(event["status"], event["last_error"], queued, now)
            payload_fp = None
            expires = None
            if queued is not None:
                expires = queued["expires_at"]
                try:
                    parsed = json.loads(queued["payload_json"])
                    if isinstance(parsed, dict) and isinstance(parsed.get("title"), str) and isinstance(parsed.get("body"), str):
                        payload_fp = content_fingerprint(parsed["title"], parsed["body"], event["priority"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload_fp = None
            accepted_at = event["updated_at"] if state == "accepted" else None
            connection.execute(
                "INSERT INTO application_events("
                "notification_id, source_key, event_key, priority, effect_fingerprint, payload_fingerprint, "
                "status, business_created_at, created_at, updated_at, expires_at, accepted_at, attempts, "
                "http_status, last_error, cancel_requested, outcome_uncertain"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    event["notification_id"],
                    event["source_key"],
                    event["event_key"],
                    event["priority"],
                    event["effect_fingerprint"],
                    payload_fp,
                    state,
                    event["created_at"],
                    event["created_at"],
                    event["updated_at"],
                    int(expires) if expires is not None else None,
                    int(accepted_at) if accepted_at is not None else None,
                    int(event["attempts"] or 0),
                    event["http_status"],
                    _safe_error(event["last_error"]),
                    1 if uncertain else 0,
                ),
            )
            inserted += 1
            if fail_after is not None and inserted == fail_after:
                raise sqlite3.OperationalError("forced rollback")
            if queued is not None and state in _ACTIVE:
                lease_token = queued["lease_token"] if state == "sending" else None
                lease_until = queued["lease_until"] if state == "sending" else None
                connection.execute(
                    "INSERT INTO application_outbox("
                    "notification_id, payload_json, next_attempt_at, expires_at, attempts, "
                    "lease_token, lease_until, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        queued["notification_id"],
                        queued["payload_json"],
                        queued["next_attempt_at"],
                        queued["expires_at"],
                        queued["attempts"],
                        lease_token,
                        lease_until,
                        queued["created_at"],
                        queued["updated_at"],
                    ),
                )
        connection.execute(
            "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
            (SCHEMA_V9, SCHEMA_V9_CHECKSUM, now),
        )
        connection.commit()
    except NotifyMeError:
        connection.rollback()
        raise
    except sqlite3.Error:
        connection.rollback()
        raise NotifyMeError("state_schema_error")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    os.chmod(path, 0o600)
    return {"migrated": True, "schema_version": SCHEMA_V9}


def delivery_committed(path):
    probe = inspect_database(path)
    if probe["schema_version"] != SCHEMA_V9:
        return False
    connection = connect_readonly(path)
    try:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key='delivery_since_upgrade'"
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        connection.close()
    if row is None:
        return False
    try:
        return json.loads(row[0]) is True
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def has_mutation_since(path, migrated_at):
    probe = inspect_database(path)
    if probe["schema_version"] != SCHEMA_V9:
        return False
    if delivery_committed(path):
        return True
    connection = connect_readonly(path)
    try:
        row = connection.execute(
            "SELECT 1 FROM application_events WHERE updated_at >= ? LIMIT 1",
            (int(migrated_at),),
        ).fetchone()
    except sqlite3.Error:
        return True
    finally:
        connection.close()
    return row is not None


class StateStore(object):
    def __init__(self, paths, clock, limits):
        self.paths = paths
        self.clock = clock
        self.limits = limits

    def _require_v9(self):
        probe = inspect_database(self.paths.state_db)
        if probe["status"] == "missing":
            raise NotifyMeError("not_initialized")
        if probe["schema_version"] == SCHEMA_V8:
            raise NotifyMeError("schema_upgrade_required")
        if probe["schema_version"] != SCHEMA_V9 or probe["status"] != "ready":
            raise NotifyMeError(probe["error_code"] or "state_schema_unsupported")
        return probe

    def salt(self, connection):
        row = connection.execute("SELECT value_json FROM settings WHERE key='scope_salt'").fetchone()
        if row is None:
            raise NotifyMeError("state_corrupt")
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise NotifyMeError("state_corrupt")
        return value

    def effect_for(self, connection, priority):
        validate_priority(priority)
        row = connection.execute(
            "SELECT effect_json FROM priority_effects WHERE priority=?", (priority,)
        ).fetchone()
        if row is None or row[0] is None:
            raise NotifyMeError("effect_required")
        try:
            parsed = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise NotifyMeError("invalid_effect")
        return validate_effect(parsed)

    def configuration_summary(self):
        probe = inspect_database(self.paths.state_db)
        if probe["status"] != "ready":
            return None
        readonly = probe["schema_version"] in (SCHEMA_V8, SCHEMA_V9)
        if not readonly:
            return None
        connection = connect_readonly(self.paths.state_db)
        try:
            rows = list(connection.execute("SELECT priority, effect_json FROM priority_effects"))
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        priorities = {}
        invalid = []
        for priority in ("P0", "P1", "P2", "P3"):
            priorities[priority] = None
        for row in rows:
            if row["effect_json"] is None:
                priorities[row["priority"]] = None
                continue
            try:
                priorities[row["priority"]] = validate_effect(json.loads(row["effect_json"]))
            except (NotifyMeError, TypeError, ValueError, json.JSONDecodeError):
                priorities[row["priority"]] = None
                invalid.append(row["priority"])
        summary = {"status": "ready", "priorities": priorities}
        if invalid:
            summary["invalid_priorities"] = invalid
        return summary

    def outbox_summary(self, source_key=None):
        probe = inspect_database(self.paths.state_db)
        if probe["status"] != "ready":
            return None
        now = _whole(self.clock)
        connection = connect_readonly(self.paths.state_db)
        try:
            if source_key is None:
                row = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(CASE WHEN next_attempt_at<=? THEN 1 ELSE 0 END), 0), MIN(expires_at) "
                    "FROM application_outbox",
                    (now,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(CASE WHEN o.next_attempt_at<=? THEN 1 ELSE 0 END), 0), MIN(o.expires_at) "
                    "FROM application_outbox o JOIN application_events e ON e.notification_id=o.notification_id "
                    "WHERE e.source_key=?",
                    (now, source_key),
                ).fetchone()
        except sqlite3.Error:
            return {"status": "unavailable"}
        finally:
            connection.close()
        return {
            "status": "ready",
            "queued": int(row[0] or 0),
            "due": int(row[1] or 0),
            "next_expiry_at": None if row[2] is None else int(row[2]),
        }

    def _identity(self, connection, source, event_id):
        return application_identity(self.salt(connection), source, event_id)

    def _load_v9(self, connection, source_key, event_key):
        event = connection.execute(
            "SELECT * FROM application_events WHERE source_key=? AND event_key=?",
            (source_key, event_key),
        ).fetchone()
        if event is None:
            return None, None
        outbox = connection.execute(
            "SELECT * FROM application_outbox WHERE notification_id=?",
            (event["notification_id"],),
        ).fetchone()
        return event, outbox

    def _meta(self, event, outbox):
        next_at = outbox["next_attempt_at"] if outbox is not None else None
        lease_until = outbox["lease_until"] if outbox is not None else None
        return metadata(
            event["status"],
            event["attempts"],
            next_at,
            event["expires_at"],
            event["accepted_at"],
            event["cancel_requested"],
            event["last_error"],
            event["outcome_uncertain"],
            event["notification_id"],
            event["priority"],
            lease_until,
            _whole(self.clock),
        )

    def read_event(self, source, event_id):
        """Normalized view for schema 8 or 9. Does not write."""

        probe = inspect_database(self.paths.state_db)
        if probe["status"] == "missing":
            raise NotifyMeError("not_initialized")
        if probe["status"] != "ready":
            raise NotifyMeError(probe["error_code"] or "state_schema_unsupported")
        connection = connect_readonly(self.paths.state_db)
        try:
            source_key, event_key, _notification_id = self._identity(connection, source, event_id)
            if probe["schema_version"] == SCHEMA_V9:
                event, outbox = self._load_v9(connection, source_key, event_key)
                if event is None:
                    return None
                return {"status": event["status"], "meta": self._meta(event, outbox)}
            if probe["schema_version"] != SCHEMA_V8:
                raise NotifyMeError("state_schema_unsupported")
            event = connection.execute(
                "SELECT notification_id, source_key, event_key, priority, status, created_at, "
                "updated_at, attempts, http_status, last_error FROM application_events "
                "WHERE source_key=? AND event_key=?",
                (source_key, event_key),
            ).fetchone()
            if event is None:
                return None
            outbox = connection.execute(
                "SELECT payload_json, next_attempt_at, expires_at, attempts, lease_token, lease_until "
                "FROM application_outbox WHERE notification_id=?",
                (event["notification_id"],),
            ).fetchone()
            outbox_view = None if outbox is None else {
                "lease_token": outbox["lease_token"],
                "lease_until": outbox["lease_until"],
            }
            state, uncertain = normalize_legacy(
                event["status"], event["last_error"], outbox_view, _whole(self.clock)
            )
            return {"status": state, "meta": metadata(
                state,
                event["attempts"],
                None if outbox is None else outbox["next_attempt_at"],
                None if outbox is None else outbox["expires_at"],
                event["updated_at"] if state == "accepted" else None,
                False,
                _safe_error(event["last_error"]),
                uncertain,
                event["notification_id"],
                event["priority"],
                None if outbox is None else outbox["lease_until"],
                _whole(self.clock),
            )}
        finally:
            connection.close()

    def _payload(self, title, body, notification_id, effect):
        payload = {
            "body": body,
            "group": "notify-me",
            "icon": ICON_URL,
            "id": notification_id,
            "title": title,
        }
        payload.update(bark_fields(effect))
        if "device_key" in payload:
            raise NotifyMeError("invalid_payload")
        return payload

    def _expire_and_prune(self, connection, source_key, now):
        rows = connection.execute(
            "SELECT e.notification_id FROM application_events e "
            "LEFT JOIN application_outbox o ON o.notification_id=e.notification_id "
            "WHERE e.source_key=? AND e.status IN ('queued','sending') "
            "AND e.expires_at IS NOT NULL AND e.expires_at<=? "
            "AND (o.lease_until IS NULL OR o.lease_until<=? OR o.notification_id IS NULL)",
            (source_key, now, now),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE application_events SET status='expired', last_error='expired', updated_at=? "
                "WHERE notification_id=? AND status IN ('queued','sending')",
                (now, row[0]),
            )
            connection.execute("DELETE FROM application_outbox WHERE notification_id=?", (row[0],))
        cutoff = now - int(self.limits.retention)
        connection.execute(
            "DELETE FROM application_outbox WHERE notification_id IN ("
            "SELECT notification_id FROM application_events "
            "WHERE status IN ('accepted','failed','expired','cancelled') AND updated_at<?)",
            (cutoff,),
        )
        connection.execute(
            "DELETE FROM application_events WHERE status IN ('accepted','failed','expired','cancelled') AND updated_at<?",
            (cutoff,),
        )

    def _assert_capacity(self, connection, source_key, count_active=True):
        total = connection.execute("SELECT COUNT(*) FROM application_events").fetchone()[0]
        per = connection.execute(
            "SELECT COUNT(*) FROM application_events WHERE source_key=?", (source_key,)
        ).fetchone()[0]
        if total + 1 > self.limits.rows_total or per + 1 > self.limits.rows_source:
            raise NotifyMeError("queue_full")
        if not count_active:
            return
        active = connection.execute(
            "SELECT COUNT(*) FROM application_events WHERE status IN ('queued','sending')"
        ).fetchone()[0]
        per_active = connection.execute(
            "SELECT COUNT(*) FROM application_events WHERE source_key=? AND status IN ('queued','sending')",
            (source_key,),
        ).fetchone()[0]
        if active + 1 > self.limits.active_total or per_active + 1 > self.limits.active_source:
            raise NotifyMeError("queue_full")

    def admit(self, source, event_id, priority, title, body, created_at, expires_at, enqueue_only):
        self._require_v9()
        now = _whole(self.clock)
        with _transaction(self.paths.state_db) as connection:
            source_key, event_key, notification_id = self._identity(connection, source, event_id)
            effect = self.effect_for(connection, priority)
            content = content_fingerprint(title, body, priority)
            self._expire_and_prune(connection, source_key, now)
            existing, outbox = self._load_v9(connection, source_key, event_key)
            if existing is not None:
                stored = existing["payload_fingerprint"]
                if stored and stored != content:
                    raise NotifyMeError("event_conflict")
                return {
                    "outcome": "deduplicated",
                    "previous_status": existing["status"],
                    "meta": self._meta(existing, outbox),
                }
            if created_at < now - int(self.limits.retention):
                raise NotifyMeError("created_at_too_old")
            if created_at > now + 120:
                raise NotifyMeError("created_at_invalid")
            service_expiry = created_at + int(effect["delivery_ttl_seconds"])
            frozen = service_expiry if expires_at is None else min(int(expires_at), service_expiry)
            if frozen <= now:
                raise NotifyMeError("event_expired")
            self._assert_capacity(connection, source_key)
            payload = self._payload(title, body, notification_id, effect)
            payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            effect_fp = fingerprint(effect)
            lease_token = None
            lease_until = None
            status = "queued"
            if not enqueue_only:
                status = "sending"
                lease_token = secrets.token_hex(16)
                lease_until = now + int(self.limits.lease)
            connection.execute(
                "INSERT INTO application_events("
                "notification_id, source_key, event_key, priority, effect_fingerprint, payload_fingerprint, "
                "status, business_created_at, created_at, updated_at, expires_at, accepted_at, attempts, "
                "http_status, last_error, cancel_requested, outcome_uncertain"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, 0, 0)",
                (
                    notification_id,
                    source_key,
                    event_key,
                    priority,
                    effect_fp,
                    content,
                    status,
                    created_at,
                    now,
                    now,
                    frozen,
                ),
            )
            connection.execute(
                "INSERT INTO application_outbox("
                "notification_id, payload_json, next_attempt_at, expires_at, attempts, "
                "lease_token, lease_until, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (notification_id, payload_json, now, frozen, lease_token, lease_until, now, now),
            )
            event, outbox = self._load_v9(connection, source_key, event_key)
            result = {"outcome": status, "meta": self._meta(event, outbox)}
            if status == "sending":
                result["claim"] = self._claim_dict(event, outbox, effect, payload)
            return result

    def _claim_dict(self, event, outbox, effect, payload):
        return {
            "notification_id": event["notification_id"],
            "source_key": event["source_key"],
            "lease_token": outbox["lease_token"],
            "payload": payload,
            "attempts": int(outbox["attempts"] or 0),
            "expires_at": int(outbox["expires_at"]),
            "priority": event["priority"],
            "level": effect["level"] if effect is not None else payload.get("level"),
            "cancel_requested": bool(event["cancel_requested"]),
        }

    def cancel(self, source, event_id):
        self._require_v9()
        now = _whole(self.clock)
        with _transaction(self.paths.state_db) as connection:
            source_key, event_key, notification_id = self._identity(connection, source, event_id)
            event, outbox = self._load_v9(connection, source_key, event_key)
            if event is None:
                self._expire_and_prune(connection, source_key, now)
                # A cancel tombstone is terminal. It uses the row caps only.
                self._assert_capacity(connection, source_key, count_active=False)
                connection.execute(
                    "INSERT INTO application_events("
                    "notification_id, source_key, event_key, priority, effect_fingerprint, payload_fingerprint, "
                    "status, business_created_at, created_at, updated_at, expires_at, accepted_at, attempts, "
                    "http_status, last_error, cancel_requested, outcome_uncertain"
                    ") VALUES (?, ?, ?, NULL, NULL, NULL, 'cancelled', ?, ?, ?, NULL, NULL, 0, NULL, NULL, 0, 0)",
                    (notification_id, source_key, event_key, now, now, now),
                )
                event, outbox = self._load_v9(connection, source_key, event_key)
                return {
                    "outcome": "cancelled",
                    "changed": True,
                    "previous_status": "not_found",
                    "reason": "tombstone",
                    "meta": self._meta(event, outbox),
                }
            if event["status"] == "cancelled":
                return {
                    "outcome": "cancelled",
                    "changed": False,
                    "previous_status": "cancelled",
                    "reason": None,
                    "meta": self._meta(event, outbox),
                }
            if event["status"] == "accepted":
                return {
                    "outcome": "not_pending",
                    "changed": False,
                    "previous_status": "accepted",
                    "reason": "accepted",
                    "meta": self._meta(event, outbox),
                }
            if event["status"] in ("failed", "expired"):
                return {
                    "outcome": "not_pending",
                    "changed": False,
                    "previous_status": event["status"],
                    "reason": event["status"],
                    "meta": self._meta(event, outbox),
                }
            lease_active = (
                outbox is not None
                and outbox["lease_token"]
                and outbox["lease_until"] is not None
                and int(outbox["lease_until"]) > now
            )
            if lease_active:
                connection.execute(
                    "UPDATE application_events SET cancel_requested=1, "
                    "status=CASE WHEN status='queued' THEN 'sending' ELSE status END, updated_at=? "
                    "WHERE notification_id=? AND source_key=? AND status IN ('queued','sending')",
                    (now, event["notification_id"], source_key),
                )
                event, outbox = self._load_v9(connection, source_key, event_key)
                return {
                    "outcome": "not_pending",
                    "changed": False,
                    "previous_status": "sending",
                    "reason": "in_flight",
                    "meta": self._meta(event, outbox),
                }
            if event["status"] == "sending":
                updated = connection.execute(
                    "UPDATE application_events SET status='cancelled', cancel_requested=1, "
                    "outcome_uncertain=1, last_error='delivery_uncertain', updated_at=? "
                    "WHERE notification_id=? AND source_key=? AND status='sending'",
                    (now, event["notification_id"], source_key),
                )
                if updated.rowcount != 1:
                    raise NotifyMeError("state_conflict")
                connection.execute(
                    "DELETE FROM application_outbox WHERE notification_id=?",
                    (event["notification_id"],),
                )
                event, outbox = self._load_v9(connection, source_key, event_key)
                return {
                    "outcome": "cancelled",
                    "changed": True,
                    "previous_status": "sending",
                    "reason": "lease_expired",
                    "meta": self._meta(event, outbox),
                }
            if event["status"] == "queued":
                deleted = connection.execute(
                    "DELETE FROM application_outbox WHERE notification_id=? "
                    "AND (lease_until IS NULL OR lease_until<=?)",
                    (event["notification_id"], now),
                )
                if deleted.rowcount != 1:
                    connection.execute(
                        "UPDATE application_events SET cancel_requested=1, status='sending', updated_at=? "
                        "WHERE notification_id=? AND source_key=?",
                        (now, event["notification_id"], source_key),
                    )
                    event, outbox = self._load_v9(connection, source_key, event_key)
                    return {
                        "outcome": "not_pending",
                        "changed": False,
                        "previous_status": "sending",
                        "reason": "in_flight",
                        "meta": self._meta(event, outbox),
                    }
                connection.execute(
                    "UPDATE application_events SET status='cancelled', last_error=NULL, updated_at=? "
                    "WHERE notification_id=? AND source_key=? AND status='queued'",
                    (now, event["notification_id"], source_key),
                )
                event, outbox = self._load_v9(connection, source_key, event_key)
                return {
                    "outcome": "cancelled",
                    "changed": True,
                    "previous_status": "queued",
                    "reason": None,
                    "meta": self._meta(event, outbox),
                }
            raise NotifyMeError("unknown_status")

    def claim_due(self, source, event_id=None):
        self._require_v9()
        now = _whole(self.clock)
        with _transaction(self.paths.state_db) as connection:
            source_key, event_key, _notification_id = self._identity(connection, source, event_id or "placeholder")
            if event_id is None:
                event_key = None
            else:
                source_key, event_key, _notification_id = self._identity(connection, source, event_id)
            self._expire_and_prune(connection, source_key, now)
            if event_id is not None:
                event, outbox = self._load_v9(connection, source_key, event_key)
                if event is None:
                    return {"outcome": "not_found"}
                if event["status"] not in _ACTIVE:
                    return {"outcome": "terminal", "status": event["status"], "meta": self._meta(event, outbox)}
                if outbox is None:
                    return {"outcome": "terminal", "status": event["status"], "meta": self._meta(event, outbox)}
                if int(outbox["next_attempt_at"]) > now:
                    return {"outcome": "waiting", "meta": self._meta(event, outbox)}
                if int(outbox["expires_at"]) <= now:
                    return {"outcome": "terminal", "status": event["status"], "meta": self._meta(event, outbox)}
                if outbox["lease_until"] is not None and int(outbox["lease_until"]) > now:
                    return {"outcome": "waiting", "meta": self._meta(event, outbox)}
            row = connection.execute(
                "SELECT e.notification_id FROM application_events e "
                "JOIN application_outbox o ON o.notification_id=e.notification_id "
                "WHERE e.source_key=? AND (? IS NULL OR e.event_key=?) "
                "AND e.status IN ('queued','sending') AND o.expires_at>? AND o.next_attempt_at<=? "
                "AND (o.lease_until IS NULL OR o.lease_until<=?) "
                "ORDER BY CASE e.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END, "
                "o.next_attempt_at, o.created_at LIMIT 1",
                (source_key, event_key, event_key, now, now, now),
            ).fetchone()
            if row is None:
                if event_id is not None:
                    event, outbox = self._load_v9(connection, source_key, event_key)
                    if event is None:
                        return {"outcome": "not_found"}
                    return {"outcome": "waiting", "meta": self._meta(event, outbox)}
                return {"outcome": "empty"}
            event = connection.execute(
                "SELECT * FROM application_events WHERE notification_id=? AND source_key=?",
                (row[0], source_key),
            ).fetchone()
            outbox = connection.execute(
                "SELECT * FROM application_outbox WHERE notification_id=?", (row[0],)
            ).fetchone()
            uncertain = bool(event["outcome_uncertain"])
            if outbox["lease_token"]:
                uncertain = True
            if event["cancel_requested"]:
                connection.execute(
                    "UPDATE application_events SET status='cancelled', outcome_uncertain=?, "
                    "last_error=?, updated_at=? WHERE notification_id=? AND source_key=?",
                    (
                        1 if uncertain else 0,
                        "delivery_uncertain" if uncertain else None,
                        now,
                        event["notification_id"],
                        source_key,
                    ),
                )
                connection.execute(
                    "DELETE FROM application_outbox WHERE notification_id=?", (event["notification_id"],)
                )
                event, outbox = self._load_v9(connection, event["source_key"], event["event_key"])
                return {"outcome": "terminal", "status": "cancelled", "meta": self._meta(event, outbox)}
            token = secrets.token_hex(16)
            lease_until = now + int(self.limits.lease)
            updated = connection.execute(
                "UPDATE application_outbox SET lease_token=?, lease_until=?, updated_at=? "
                "WHERE notification_id=? AND (lease_until IS NULL OR lease_until<=?)",
                (token, lease_until, now, event["notification_id"], now),
            )
            if updated.rowcount != 1:
                return {"outcome": "waiting", "meta": self._meta(event, outbox)}
            connection.execute(
                "UPDATE application_events SET status='sending', outcome_uncertain=?, updated_at=? "
                "WHERE notification_id=? AND source_key=? AND status IN ('queued','sending')",
                (1 if uncertain else int(event["outcome_uncertain"] or 0), now, event["notification_id"], source_key),
            )
            event = connection.execute(
                "SELECT * FROM application_events WHERE notification_id=?", (event["notification_id"],)
            ).fetchone()
            outbox = connection.execute(
                "SELECT * FROM application_outbox WHERE notification_id=?", (event["notification_id"],)
            ).fetchone()
            try:
                payload = json.loads(outbox["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                connection.execute(
                    "UPDATE application_events SET status='failed', last_error='invalid_payload', updated_at=? "
                    "WHERE notification_id=?",
                    (now, event["notification_id"]),
                )
                connection.execute(
                    "DELETE FROM application_outbox WHERE notification_id=?", (event["notification_id"],)
                )
                event = connection.execute(
                    "SELECT * FROM application_events WHERE notification_id=?", (event["notification_id"],)
                ).fetchone()
                return {"outcome": "terminal", "status": "failed", "meta": self._meta(event, None)}
            if not isinstance(payload, dict) or "device_key" in payload:
                connection.execute(
                    "UPDATE application_events SET status='failed', last_error='invalid_payload', updated_at=? "
                    "WHERE notification_id=?",
                    (now, event["notification_id"]),
                )
                connection.execute(
                    "DELETE FROM application_outbox WHERE notification_id=?", (event["notification_id"],)
                )
                event = connection.execute(
                    "SELECT * FROM application_events WHERE notification_id=?", (event["notification_id"],)
                ).fetchone()
                return {"outcome": "terminal", "status": "failed", "meta": self._meta(event, None)}
            effect_level = payload.get("level")
            return {
                "outcome": "claimed",
                "meta": self._meta(event, outbox),
                "claim": self._claim_dict(event, outbox, {"level": effect_level}, payload),
            }

    def finalize(self, claim, accepted, attempt_count, http_status, error_code, retryable):
        self._require_v9()
        now = _whole(self.clock)
        safe_error = _safe_error(error_code) if error_code else None
        with _transaction(self.paths.state_db) as connection:
            outbox = connection.execute(
                "SELECT o.expires_at, o.attempts, e.cancel_requested, e.source_key, e.event_key, e.outcome_uncertain "
                "FROM application_outbox o JOIN application_events e ON e.notification_id=o.notification_id "
                "WHERE o.notification_id=? AND o.lease_token=? AND o.lease_until>?",
                (claim["notification_id"], claim["lease_token"], now),
            ).fetchone()
            if outbox is None:
                # The lease is gone, so this finalize cannot change status.
                # If another commit already accepted, keep that accept and
                # record that an overlapping send may have duplicated it.
                connection.execute(
                    "UPDATE application_events SET outcome_uncertain=1 "
                    "WHERE notification_id=? AND status='accepted' AND outcome_uncertain=0",
                    (claim["notification_id"],),
                )
                return {"applied": False}
            attempts = int(outbox["attempts"] or 0) + int(attempt_count or 1)
            cancel_requested = bool(outbox["cancel_requested"])
            if accepted:
                updated = connection.execute(
                    "UPDATE application_events SET status='accepted', accepted_at=?, attempts=?, "
                    "http_status=?, last_error=NULL, updated_at=? "
                    "WHERE notification_id=? AND status='sending'",
                    (now, attempts, http_status, now, claim["notification_id"]),
                )
                if updated.rowcount != 1:
                    raise NotifyMeError("state_conflict")
                connection.execute(
                    "DELETE FROM application_outbox WHERE notification_id=? AND lease_token=?",
                    (claim["notification_id"], claim["lease_token"]),
                )
                connection.execute(
                    "INSERT INTO settings(key, value_json, updated_at) VALUES ('delivery_since_upgrade', 'true', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json='true', updated_at=excluded.updated_at",
                    (now,),
                )
                event, queued = self._load_v9(connection, outbox["source_key"], outbox["event_key"])
                return {
                    "applied": True,
                    "status": "accepted",
                    "cancel_late": cancel_requested,
                    "meta": self._meta(event, queued),
                }
            if cancel_requested:
                connection.execute(
                    "UPDATE application_events SET status='cancelled', attempts=?, http_status=?, "
                    "last_error=NULL, updated_at=? WHERE notification_id=? AND status='sending'",
                    (attempts, http_status, now, claim["notification_id"]),
                )
                connection.execute(
                    "DELETE FROM application_outbox WHERE notification_id=? AND lease_token=?",
                    (claim["notification_id"], claim["lease_token"]),
                )
                event, queued = self._load_v9(connection, outbox["source_key"], outbox["event_key"])
                return {"applied": True, "status": "cancelled", "cancel_late": False, "meta": self._meta(event, queued)}
            if int(outbox["expires_at"]) <= now:
                connection.execute(
                    "UPDATE application_events SET status='expired', attempts=?, last_error='expired', updated_at=? "
                    "WHERE notification_id=? AND status='sending'",
                    (attempts, now, claim["notification_id"]),
                )
                connection.execute(
                    "DELETE FROM application_outbox WHERE notification_id=? AND lease_token=?",
                    (claim["notification_id"], claim["lease_token"]),
                )
                event, queued = self._load_v9(connection, outbox["source_key"], outbox["event_key"])
                return {"applied": True, "status": "expired", "cancel_late": False, "meta": self._meta(event, queued)}
            if retryable:
                delay = backoff_seconds(claim.get("level"), claim.get("attempts", 0))
                connection.execute(
                    "UPDATE application_events SET status='queued', attempts=?, http_status=?, last_error=?, updated_at=? "
                    "WHERE notification_id=? AND status='sending'",
                    (attempts, http_status, safe_error, now, claim["notification_id"]),
                )
                connection.execute(
                    "UPDATE application_outbox SET attempts=?, next_attempt_at=?, lease_token=NULL, lease_until=NULL, updated_at=? "
                    "WHERE notification_id=? AND lease_token=?",
                    (attempts, now + delay, now, claim["notification_id"], claim["lease_token"]),
                )
                event, queued = self._load_v9(connection, outbox["source_key"], outbox["event_key"])
                return {"applied": True, "status": "queued", "cancel_late": False, "meta": self._meta(event, queued)}
            connection.execute(
                "UPDATE application_events SET status='failed', attempts=?, http_status=?, last_error=?, updated_at=? "
                "WHERE notification_id=? AND status='sending'",
                (attempts, http_status, safe_error or "permanent_failure", now, claim["notification_id"]),
            )
            connection.execute(
                "DELETE FROM application_outbox WHERE notification_id=? AND lease_token=?",
                (claim["notification_id"], claim["lease_token"]),
            )
            event, queued = self._load_v9(connection, outbox["source_key"], outbox["event_key"])
            return {"applied": True, "status": "failed", "cancel_late": False, "meta": self._meta(event, queued)}

    def reread(self, source, event_id):
        view = self.read_event(source, event_id)
        return view

    def reread_notification(self, notification_id):
        probe = inspect_database(self.paths.state_db)
        if probe["status"] != "ready" or probe["schema_version"] != SCHEMA_V9:
            return None
        connection = connect_readonly(self.paths.state_db)
        try:
            event = connection.execute(
                "SELECT * FROM application_events WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            if event is None:
                return None
            outbox = connection.execute(
                "SELECT * FROM application_outbox WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            return {"status": event["status"], "meta": self._meta(event, outbox)}
        finally:
            connection.close()


def load_endpoint(paths):
    """Read the application .env. Status may call this. It does not create files."""

    directory = directory_fact(paths.config_dir)
    if directory["status"] == "missing":
        raise NotifyMeError("configuration_missing")
    if directory["status"] != "ready":
        raise NotifyMeError("config_permissions")
    fact = file_fact(paths.dotenv)
    if fact["status"] == "missing":
        raise NotifyMeError("configuration_missing")
    if fact["status"] != "ready":
        raise NotifyMeError("binding_permissions")
    try:
        text = paths.dotenv.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise NotifyMeError("binding_unavailable")
    raw = None
    for line in text.splitlines():
        if line.startswith("BARK_URL="):
            raw = line[len("BARK_URL=") :]
            break
    if raw is None:
        raise NotifyMeError("configuration_missing")
    try:
        return BarkEndpoint.parse(raw)
    except NotifyMeError:
        raise NotifyMeError("binding_invalid")


def binding_bytes(path):
    try:
        return path.read_bytes()
    except OSError:
        return None


def write_binding(paths, endpoint, replace):
    directory = directory_fact(paths.config_dir)
    if directory["status"] == "missing":
        raise NotifyMeError("not_initialized")
    if directory["status"] != "ready":
        raise NotifyMeError("config_permissions")
    fact = file_fact(paths.dotenv)
    if fact["status"] == "ready" and not replace:
        raise NotifyMeError("binding_exists")
    if fact["status"] == "unsafe":
        raise NotifyMeError("binding_permissions")
    if paths.dotenv.is_symlink():
        raise NotifyMeError("binding_invalid")
    data = "BARK_URL={}/{}\n".format(endpoint.server, endpoint.key).encode("utf-8")
    temporary = None
    try:
        descriptor, temporary = __import__("tempfile").mkstemp(
            prefix=".notify-me-env-", dir=str(paths.config_dir)
        )
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, paths.dotenv)
        temporary = None
    except OSError:
        raise NotifyMeError("binding_write_failed")
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    os.chmod(paths.dotenv, 0o600)


def endpoint_from_binding_file(path):
    """Validate an explicitly chosen agent binding or application env file."""

    fact = file_fact(path)
    if fact["status"] != "ready":
        raise NotifyMeError("invalid_binding")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise NotifyMeError("invalid_binding")
    stripped = raw.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise NotifyMeError("invalid_binding")
        return BarkEndpoint.from_stored(data)
    line = None
    for item in raw.splitlines():
        if item.startswith("BARK_URL="):
            line = item[len("BARK_URL=") :]
            break
    if line is None:
        raise NotifyMeError("invalid_binding")
    return BarkEndpoint.parse(line)


# Imported for tests that rebuild a historical database with the exact DDL.
HISTORICAL_SCHEMA_V8_SQL = LEGACY_SCHEMA_V8_SQL
