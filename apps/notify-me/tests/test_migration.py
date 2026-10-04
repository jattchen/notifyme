# -*- coding: utf-8 -*-
"""Schema 8 migration using a mixed in-memory-shaped fixture. No host database."""

import json
import os
import sqlite3
import stat
import tempfile
import unittest
import unittest.mock
import urllib.parse
from pathlib import Path

from notify_me_app.configuration import application_identity, content_fingerprint
from notify_me_app.errors import NotifyMeError
from notify_me_app.protocol import normalize_legacy
from notify_me_app.schema import LEGACY_SCHEMA_V8_CHECKSUM, LEGACY_SCHEMA_V8_SQL
from notify_me_app.storage import ManualClock, migrate_8_to_9


_EMBEDDED_VECTORS = os.path.join(os.path.dirname(__file__), "legacy_identity_vectors.json")
SALT = "a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5"
_HISTORICAL_VECTORS = [
    {
        "source": "aiusage",
        "event_id": "monitor-error-codex-1700000000",
        "source_key": "d18e95b3f13fc23be259f631d4ac64759e5baeb4fce21f0e267cfdd3d4f5b88e",
        "event_key": "f6bb0246608c309b06af95c708b11a949b8871a4b748e69a550c6afef138bd5d",
        "notification_id": "nm_5f6a91e450c39f824a76667b9819f87c99e1ebe3",
    },
    {
        "source": "aiusage",
        "event_id": "quota-codex-1700604800-remaining-0-1",
        "source_key": "d18e95b3f13fc23be259f631d4ac64759e5baeb4fce21f0e267cfdd3d4f5b88e",
        "event_key": "b74207af5e3a000acc12a460d0a18f10c71f61dc9697546c38a40b3f31331527",
        "notification_id": "nm_78babe257ed560218adf1d268ff8fade20f56e34",
    },
    {
        "source": "otherapp",
        "event_id": "monitor-error-codex-1700000000",
        "source_key": "536b8dd1fcf6b655a59098d854054cc2f9c3ea5a55fe0560566bbd374f9345e0",
        "event_key": "7a9b7e3b0be484b6feb4ebafb5538427f421f43ccf6ef583a4ef543418b28a72",
        "notification_id": "nm_d453828b52ba8c0cf1bc67c186e93e75201abbd7",
    },
]
MIGRATION_NOW = 500000
P2_EFFECT_JSON = json.dumps(
    {"call": False, "delivery_ttl_seconds": 15000, "level": "active", "sound": "bell"},
    sort_keys=True,
    separators=(",", ":"),
)


def _load_vectors():
    """Repository fixture only. The historical HMAC values are fixed below."""

    with open(_EMBEDDED_VECTORS, "r", encoding="utf-8") as handle:
        embedded = json.load(handle)
    if embedded.get("fake_scope_salt") != SALT:
        raise AssertionError("legacy identity vector salt does not match the fixture")
    if embedded.get("historical_source") != "556f114:notify_me/application_push.py:_identity":
        raise AssertionError("legacy identity vector source drifted")
    if embedded.get("vectors") != _HISTORICAL_VECTORS:
        raise AssertionError("legacy identity vectors drifted")
    for row in _HISTORICAL_VECTORS:
        got = application_identity(SALT, row["source"], row["event_id"])
        if got != (row["source_key"], row["event_key"], row["notification_id"]):
            raise AssertionError("legacy identity HMAC drifted for {}".format(row["event_id"]))
    return embedded["vectors"]


def _identity(source, event_id):
    return application_identity(SALT, source, event_id)


def _payload(title, body, notification_id):
    return json.dumps(
        {"body": body, "group": "notify-me", "id": notification_id, "title": title},
        sort_keys=True,
        separators=(",", ":"),
    )


def _snapshot(path):
    quoted = urllib.parse.quote(str(path))
    connection = sqlite3.connect("file:{}?mode=ro".format(quoted), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        def rows(sql):
            return [tuple(row) for row in connection.execute(sql)]

        return {
            "migrations": rows("SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version"),
            "events": rows("SELECT * FROM application_events ORDER BY notification_id"),
            "outbox": rows("SELECT * FROM application_outbox ORDER BY notification_id"),
            "settings": rows("SELECT key, value_json, updated_at FROM settings ORDER BY key"),
            "effects": rows("SELECT priority, effect_json, updated_at FROM priority_effects ORDER BY priority"),
            "notifications": rows("SELECT * FROM notifications ORDER BY notification_id"),
            "event_columns": [row[1] for row in connection.execute("PRAGMA table_info(application_events)")],
        }
    finally:
        connection.close()


def _write_schema8(path, vectors):
    os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in LEGACY_SCHEMA_V8_SQL.splitlines():
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
            (8, LEGACY_SCHEMA_V8_CHECKSUM, 1000),
        )
        connection.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES ('scope_salt', ?, 1000)",
            (json.dumps(SALT),),
        )
        connection.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES ('kept_setting', ?, 1000)",
            (json.dumps("keep-me"),),
        )
        effects = {
            "P0": {"call": False, "delivery_ttl_seconds": 900, "level": "critical", "sound": "alarm", "volume": 8},
            "P1": {"call": False, "delivery_ttl_seconds": 7200, "level": "timeSensitive", "sound": "telegraph"},
            "P2": json.loads(P2_EFFECT_JSON),
            "P3": None,
        }
        for priority in ("P0", "P1", "P2", "P3"):
            effect = effects[priority]
            connection.execute(
                "INSERT INTO priority_effects(priority, effect_json, updated_at) VALUES (?, ?, 1000)",
                (priority, None if effect is None else json.dumps(effect, sort_keys=True, separators=(",", ":"))),
            )
        connection.execute(
            "INSERT INTO notifications("
            "notification_id, scope_key, condition_key, item_key, event_state_key, effect_fingerprint, "
            "status, created_at, updated_at, attempts, http_status, last_error"
            ") VALUES ('agent-kept', 'scope', 'blocking', 'item', 'state', 'fp', 'accepted', 1000, 1000, 0, NULL, NULL)"
        )
        accepted, queued, other = vectors
        cases = [
            _case(accepted, "accepted", "P1", "effect-accepted", "accepted", None, 2, 200, None),
            _case(queued, "failed", "P0", "effect-queued", "failed", "network_error", 1, 503, _outbox(
                queued, "Queued", "Still waiting", 424242, 1300, 1, None, None, 1100, 1200
            )),
            _case(other, "sending", "P2", "effect-sending", "sending", None, 1, None, _outbox(
                other, "Send", "Now", 777777, 1000, 1, "lease-sending", 560000, 1000, 1400
            )),
            _manual(
                "aiusage", "cancel-legacy-1", "P1", "effect-cancel", "failed", "cancelled", 1, None, 1000, 1600,
                _manual_outbox("aiusage", "cancel-legacy-1", "Gone", "Cancel", 333333, 1000, 1, None, None, 1000, 1600),
            ),
            _manual(
                "aiusage", "expire-legacy-1", "P1", "effect-expire", "failed", "expired", 1, None, 1000, 1700,
                _manual_outbox("aiusage", "expire-legacy-1", "Old", "Expire", 222222, 1000, 1, None, None, 1000, 1700),
            ),
            _manual(
                "aiusage", "retry-legacy-1", "P2", "effect-retry", "failed", "retryable_http", 3, 500, 1000, 1800,
                _manual_outbox("aiusage", "retry-legacy-1", "Retry", "Later", 555555, 1800, 3, "old-lease", 50, 1000, 1800),
            ),
            _manual(
                "aiusage", "secret-legacy-1", "P1", "effect-secret", "failed", "fake_credential_lowercase", 1, None, 1000, 1900,
                None,
            ),
        ]
        for case in cases:
            _insert_event(connection, case)
        connection.commit()
    finally:
        connection.close()
    os.chmod(path, 0o600)


def _case(vector, raw_status, priority, effect, status, last_error, attempts, http_status, outbox):
    source_key, event_key, notification_id = _identity(vector["source"], vector["event_id"])
    if notification_id != vector["notification_id"] or source_key != vector["source_key"] or event_key != vector["event_key"]:
        raise AssertionError("identity vector mismatch for {}".format(vector["event_id"]))
    body = {
        "source": vector["source"],
        "event_id": vector["event_id"],
        "notification_id": notification_id,
        "source_key": source_key,
        "event_key": event_key,
        "priority": priority,
        "effect": effect,
        "status": status,
        "last_error": last_error,
        "attempts": attempts,
        "http_status": http_status,
        "created_at": 1000,
        "updated_at": 1500 if raw_status == "sending" else 1000,
        "outbox": None,
    }
    if raw_status == "accepted":
        body["updated_at"] = 1000
    if raw_status == "failed" and vector["event_id"].startswith("quota"):
        body["created_at"] = 1100
        body["updated_at"] = 1200
    if outbox is not None:
        outbox["notification_id"] = notification_id
        body["outbox"] = outbox
    return body


def _outbox(vector, title, body, expires_at, next_attempt_at, attempts, lease_token, lease_until, created_at, updated_at):
    _source_key, _event_key, notification_id = _identity(vector["source"], vector["event_id"])
    return {
        "notification_id": notification_id,
        "payload_json": _payload(title, body, notification_id),
        "title": title,
        "body": body,
        "next_attempt_at": next_attempt_at,
        "expires_at": expires_at,
        "attempts": attempts,
        "lease_token": lease_token,
        "lease_until": lease_until,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _manual(source, event_id, priority, effect, status, last_error, attempts, http_status, created_at, updated_at, outbox):
    source_key, event_key, notification_id = _identity(source, event_id)
    if outbox is not None:
        outbox["notification_id"] = notification_id
    return {
        "source": source,
        "event_id": event_id,
        "notification_id": notification_id,
        "source_key": source_key,
        "event_key": event_key,
        "priority": priority,
        "effect": effect,
        "status": status,
        "last_error": last_error,
        "attempts": attempts,
        "http_status": http_status,
        "created_at": created_at,
        "updated_at": updated_at,
        "outbox": outbox,
    }


def _manual_outbox(source, event_id, title, body, expires_at, next_attempt_at, attempts, lease_token, lease_until, created_at, updated_at):
    _source_key, _event_key, notification_id = _identity(source, event_id)
    return {
        "notification_id": notification_id,
        "payload_json": _payload(title, body, notification_id),
        "title": title,
        "body": body,
        "next_attempt_at": next_attempt_at,
        "expires_at": expires_at,
        "attempts": attempts,
        "lease_token": lease_token,
        "lease_until": lease_until,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _insert_event(connection, case):
    connection.execute(
        "INSERT INTO application_events("
        "notification_id, source_key, event_key, priority, effect_fingerprint, status, "
        "created_at, updated_at, attempts, http_status, last_error"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            case["notification_id"],
            case["source_key"],
            case["event_key"],
            case["priority"],
            case["effect"],
            case["status"],
            case["created_at"],
            case["updated_at"],
            case["attempts"],
            case["http_status"],
            case["last_error"],
        ),
    )
    outbox = case["outbox"]
    if outbox is None:
        return
    connection.execute(
        "INSERT INTO application_outbox("
        "notification_id, payload_json, next_attempt_at, expires_at, attempts, "
        "lease_token, lease_until, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            outbox["notification_id"],
            outbox["payload_json"],
            outbox["next_attempt_at"],
            outbox["expires_at"],
            outbox["attempts"],
            outbox["lease_token"],
            outbox["lease_until"],
            outbox["created_at"],
            outbox["updated_at"],
        ),
    )


class Schema8MigrationTests(unittest.TestCase):
    def setUp(self):
        self._vectors = _load_vectors()
        self._root = tempfile.mkdtemp(prefix="notify-me-migrate-")
        os.chmod(self._root, 0o700)

    def tearDown(self):
        for name in os.listdir(self._root):
            os.unlink(os.path.join(self._root, name))
        os.rmdir(self._root)

    def _database(self):
        path = Path(self._root) / "state.sqlite3"
        _write_schema8(path, self._vectors)
        return path

    def test_identity_fixture_does_not_read_outside_the_repository(self):
        self.assertTrue(os.path.isfile(_EMBEDDED_VECTORS))
        self.assertTrue(_EMBEDDED_VECTORS.endswith(os.path.join("tests", "legacy_identity_vectors.json")))

        real_open = open

        def guarded_open(path, *args, **kwargs):
            text = path if isinstance(path, str) else str(path)
            if text.startswith("/tmp/"):
                raise AssertionError("identity fixture read an external file")
            return real_open(path, *args, **kwargs)

        with unittest.mock.patch("builtins.open", guarded_open):
            vectors = _load_vectors()
        self.assertEqual(
            [row["notification_id"] for row in vectors],
            [
                "nm_5f6a91e450c39f824a76667b9819f87c99e1ebe3",
                "nm_78babe257ed560218adf1d268ff8fade20f56e34",
                "nm_d453828b52ba8c0cf1bc67c186e93e75201abbd7",
            ],
        )

    def test_sqlite_row_has_no_get_and_would_crash_normalize(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE application_outbox(lease_until REAL, lease_token TEXT)")
        connection.execute("INSERT INTO application_outbox(lease_until, lease_token) VALUES (1, 'x')")
        row = connection.execute("SELECT lease_until, lease_token FROM application_outbox").fetchone()
        self.assertFalse(hasattr(row, "get"))
        with self.assertRaises(AttributeError):
            normalize_legacy("failed", None, row, MIGRATION_NOW)

    def test_mixed_fixture_migrates_identity_state_ttl_and_salt(self):
        path = self._database()
        result = migrate_8_to_9(path, ManualClock(MIGRATION_NOW))
        self.assertEqual(result, {"migrated": True, "schema_version": 9})
        connection = sqlite3.connect(str(path))
        connection.row_factory = sqlite3.Row
        try:
            salt = connection.execute("SELECT value_json, updated_at FROM settings WHERE key='scope_salt'").fetchone()
            self.assertEqual(salt["value_json"], json.dumps(SALT))
            self.assertEqual(salt["updated_at"], 1000)
            kept = connection.execute("SELECT value_json FROM settings WHERE key='kept_setting'").fetchone()
            self.assertEqual(kept["value_json"], json.dumps("keep-me"))
            p2 = connection.execute("SELECT effect_json FROM priority_effects WHERE priority='P2'").fetchone()
            self.assertEqual(p2["effect_json"], P2_EFFECT_JSON)
            agent = connection.execute("SELECT status FROM notifications WHERE notification_id='agent-kept'").fetchone()
            self.assertEqual(agent["status"], "accepted")
            versions = [row["version"] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
            self.assertEqual(versions, [8, 9])
            events = {
                row["notification_id"]: row
                for row in connection.execute("SELECT * FROM application_events")
            }
            outbox = {
                row["notification_id"]: row
                for row in connection.execute("SELECT * FROM application_outbox")
            }
            self.assertEqual(len(events), 7)
            accepted = self._expect(events, self._vectors[0])
            queued = self._expect(events, self._vectors[1])
            sending = self._expect(events, self._vectors[2])
            self.assertNotEqual(accepted["notification_id"], sending["notification_id"])
            self.assertNotEqual(accepted["source_key"], sending["source_key"])
            self.assertEqual(accepted["status"], "accepted")
            self.assertEqual(accepted["accepted_at"], 1000)
            self.assertEqual(accepted["updated_at"], 1000)
            self.assertEqual(accepted["outcome_uncertain"], 0)
            self.assertIsNone(accepted["payload_fingerprint"])
            self.assertNotIn(accepted["notification_id"], outbox)
            self.assertEqual(queued["status"], "queued")
            self.assertEqual(queued["outcome_uncertain"], 0)
            self.assertEqual(queued["expires_at"], 424242)
            self.assertEqual(queued["updated_at"], 1200)
            self.assertEqual(queued["last_error"], "network_error")
            queued_box = outbox[queued["notification_id"]]
            self.assertEqual(queued_box["expires_at"], 424242)
            self.assertIsNone(queued_box["lease_token"])
            self.assertEqual(
                queued["payload_fingerprint"],
                content_fingerprint("Queued", "Still waiting", "P0"),
            )
            self.assertEqual(sending["status"], "sending")
            self.assertEqual(sending["outcome_uncertain"], 0)
            self.assertEqual(sending["expires_at"], 777777)
            self.assertEqual(sending["updated_at"], 1500)
            sending_box = outbox[sending["notification_id"]]
            self.assertEqual(sending_box["lease_token"], "lease-sending")
            self.assertEqual(sending_box["lease_until"], 560000)
            self.assertEqual(sending_box["expires_at"], 777777)
            cancelled = self._by_event(events, "aiusage", "cancel-legacy-1")
            expired = self._by_event(events, "aiusage", "expire-legacy-1")
            retry = self._by_event(events, "aiusage", "retry-legacy-1")
            secret = self._by_event(events, "aiusage", "secret-legacy-1")
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(cancelled["last_error"], "cancelled")
            self.assertNotIn(cancelled["notification_id"], outbox)
            self.assertEqual(expired["status"], "expired")
            self.assertEqual(expired["last_error"], "expired")
            self.assertNotIn(expired["notification_id"], outbox)
            self.assertEqual(retry["status"], "queued")
            self.assertEqual(retry["outcome_uncertain"], 1)
            self.assertEqual(retry["expires_at"], 555555)
            self.assertEqual(retry["updated_at"], 1800)
            retry_box = outbox[retry["notification_id"]]
            self.assertIsNone(retry_box["lease_token"])
            self.assertIsNone(retry_box["lease_until"])
            self.assertEqual(retry_box["expires_at"], 555555)
            self.assertEqual(secret["status"], "failed")
            self.assertEqual(secret["last_error"], "legacy_error_redacted")
            self.assertNotIn("fake_credential_lowercase", secret["last_error"])
            for row in events.values():
                self.assertNotEqual(row["updated_at"], MIGRATION_NOW)
                self.assertEqual(row["business_created_at"], row["created_at"])
        finally:
            connection.close()
        self.assertFalse((path.parent / (path.name + "-wal")).exists())
        self.assertFalse((path.parent / (path.name + "-shm")).exists())
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode & 0o077, 0)

        before_repeat = _snapshot(path)
        again = migrate_8_to_9(path, ManualClock(MIGRATION_NOW + 50))
        self.assertEqual(again, {"migrated": False, "schema_version": 9})
        self.assertEqual(_snapshot(path), before_repeat)

    def test_failed_rewrite_rolls_back_to_schema8(self):
        path = self._database()
        before = _snapshot(path)
        os.environ["NOTIFY_ME_TEST_MODE"] = "1"
        os.environ["NOTIFY_ME_TEST_MIGRATION_FAIL_AFTER"] = "1"
        try:
            with self.assertRaises(NotifyMeError) as caught:
                migrate_8_to_9(path, ManualClock(MIGRATION_NOW))
            self.assertEqual(caught.exception.code, "state_schema_error")
        finally:
            os.environ.pop("NOTIFY_ME_TEST_MODE", None)
            os.environ.pop("NOTIFY_ME_TEST_MIGRATION_FAIL_AFTER", None)
        self.assertEqual(_snapshot(path), before)
        self.assertNotIn("payload_fingerprint", before["event_columns"])
        self.assertEqual(before["migrations"], [(8, LEGACY_SCHEMA_V8_CHECKSUM, 1000)])

    def _expect(self, events, vector):
        row = events[vector["notification_id"]]
        self.assertEqual(row["source_key"], vector["source_key"])
        self.assertEqual(row["event_key"], vector["event_key"])
        return row

    def _by_event(self, events, source, event_id):
        _source_key, _event_key, notification_id = _identity(source, event_id)
        return events[notification_id]


if __name__ == "__main__":
    unittest.main()
