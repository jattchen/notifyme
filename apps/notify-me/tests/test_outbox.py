# -*- coding: utf-8 -*-
"""Groups 1-4: protocol entry, source isolation, six states, priority and quota."""

import os
import sqlite3
import sys
from pathlib import Path

from notify_me_app.configuration import ICON_URL, backoff_seconds
from notify_me_app.storage import ManualClock
from notify_me_app.transport import FakeBarkTransport, TransportResult

from tests.support import DEVICE_KEY, IsolatedCase
from tests.test_migration import MIGRATION_NOW, _load_vectors, _write_schema8


def _push(event, priority="P0", title="Hello", body="World", source="aiusage", extra=None):
    argv = [
        "push",
        "--source",
        source,
        "--event-id",
        event,
        "--priority",
        priority,
        "--title",
        title,
        "--body",
        body,
    ]
    if extra:
        argv.extend(extra)
    return argv


class VersionAndStatusTests(IsolatedCase):
    def test_version_does_not_touch_files_in_any_state(self):
        missing = os.path.join(self.root, "missing-config")
        before = self.names(self.root)
        env = self.env()
        env["NOTIFY_ME_CONFIG_DIR"] = missing
        code, body, text, _err = self.cli(["version", "--json"], env=env)
        self.assertEqual(code, 0)
        self.assertTrue(body["ok"])
        self.assertEqual(body["protocol_version"], 1)
        self.assertEqual(body["source_commit"], "dev")
        self.assertEqual(body["build_digest"], "dev")
        self.assertEqual(body["artifact"], "source")
        self.assertEqual(body["python_requires"], ">=3.9")
        self.assertEqual(
            body["python_version"],
            "{}.{}.{}".format(sys.version_info[0], sys.version_info[1], sys.version_info[2]),
        )
        self.assertEqual(body["source_files"], [])
        self.assertFalse(body["capabilities"]["exactly_once"])
        self.assertEqual(body["capabilities"]["delivery"], "at_least_once_same_id_within_ttl")
        self.assertTrue(body["capabilities"]["phone_duplicate_possible"])
        self.assertEqual(body["capabilities"]["outcome_uncertain_on_accepted"], "prior_attempt")
        self.assertEqual(body["capabilities"]["unknown_event_query"], "not_found")
        self.assertEqual(body["capabilities"]["unknown_event_cancel"], "cancelled")
        self.assertEqual(body["capabilities"]["unknown_event_cancel_previous_status"], "not_found")
        self.assertFalse(body["capabilities"]["force_drain"])
        self.assertTrue(body["capabilities"]["source_required_drain"])
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(self.names(self.root), before)
        self.assertLessEqual(len(text.encode("utf-8")), 65536)

    def test_protocol_mismatch_and_force_fail_before_filesystem(self):
        missing = os.path.join(self.root, "missing-config")
        env = self.env()
        env["NOTIFY_ME_CONFIG_DIR"] = missing
        code, body, _text, _err = self.cli(["version", "--protocol-version", "2"], env=env)
        self.assertEqual(code, 1)
        self.assert_error("protocol_mismatch", body)
        code, body, _text, _err = self.cli(
            ["push", "--protocol-version", "nope", "--source", "aiusage"], env=env
        )
        self.assert_error("invalid_arguments", body)
        code, body, _text, _err = self.cli(
            ["push-drain", "--force", "--protocol-version", "2"], env=env
        )
        self.assert_error("protocol_mismatch", body)
        code, body, _text, _err = self.cli(["push-drain", "--force"], env=env)
        self.assert_error("source_required", body)
        code, body, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--force"], env=env
        )
        self.assert_error("invalid_arguments", body)
        self.assertFalse(os.path.exists(missing))

    def test_status_without_config_creates_nothing(self):
        missing = os.path.join(self.root, "missing-config")
        env = self.env()
        env["NOTIFY_ME_CONFIG_DIR"] = missing
        code, body, _text, _err = self.cli(["status"], env=env)
        self.assertEqual(code, 0)
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "configuration_missing")
        self.assertFalse(body["bound"])
        self.assertIn("capabilities", body)
        self.assertFalse(os.path.exists(missing))

    def test_bound_without_database_stays_not_initialized(self):
        self.write_env()
        before = self.names(self.config)
        code, body, _text, _err = self.cli(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(body["status"], "not_initialized")
        self.assertTrue(body["bound"])
        self.assertEqual(body["host"], "127.0.0.1")
        self.assertIsNone(body["state_database"]["schema_version"])
        self.assertFalse(body["state_database"]["writable"])
        self.assertFalse(body["migration_required"])
        self.assertEqual(self.names(self.config), before)

    def test_schema8_status_is_read_only(self):
        self.make_config()
        self.write_env()
        _write_schema8(Path(self.config) / "state.sqlite3", _load_vectors())
        before = self.database_bytes()
        code, body, _text, _err = self.cli(["status"], clock=self._legacy_clock())
        self.assertEqual(code, 0)
        self.assertEqual(body["status"], "ready")
        self.assertTrue(body["migration_required"])
        self.assertFalse(body["state_database"]["writable"])
        self.assertEqual(body["state_database"]["schema_version"], 8)
        self.assertEqual(self.database_bytes(), before)
        self.assertFalse(os.path.exists(os.path.join(self.config, "state.sqlite3-wal")))
        self.assertFalse(os.path.exists(os.path.join(self.config, "state.sqlite3-shm")))
        self.assertFalse(os.path.exists(os.path.join(self.config, "state.sqlite3-journal")))

    def test_unknown_schema_is_a_read_only_error(self):
        self.make_config()
        path = os.path.join(self.config, "state.sqlite3")
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at REAL NOT NULL)"
        )
        connection.execute("INSERT INTO schema_migrations VALUES (4, 'nope', 1)")
        connection.commit()
        connection.close()
        os.chmod(path, 0o600)
        before = self.database_bytes()
        code, body, _text, _err = self.cli(["status"])
        self.assertEqual(code, 1)
        self.assert_error("state_schema_unsupported", body)
        self.assertEqual(self.database_bytes(), before)

    def _legacy_clock(self):
        return ManualClock(MIGRATION_NOW)


class PushContractTests(IsolatedCase):
    def test_original_five_arguments_enqueue_and_missing_config_does_not_mkdir(self):
        missing = os.path.join(self.root, "missing-config")
        env = self.env()
        env["NOTIFY_ME_CONFIG_DIR"] = missing
        code, body, _text, _err = self.cli(_push("evt-1"), env=env)
        self.assert_error("configuration_missing", body)
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(self.transport.calls, 0)

        self.write_env()
        code, body, _text, _err = self.cli(_push("evt-1")[:-2])
        self.assert_error("invalid_arguments", body)

        code, body, _text, _err = self.cli(_push("evt-1", extra=["--enqueue-only"]))
        self.assertEqual(code, 0)
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["expires_at"], 1700000900)
        self.assertTrue(body["notification_id"].startswith("nm_"))
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.event_count(), 1)

    def test_schema8_push_does_not_write_or_send(self):
        self.make_config()
        self.write_env()
        _write_schema8(Path(self.config) / "state.sqlite3", _load_vectors())
        before = self.database_bytes()
        code, body, _text, _err = self.cli(_push("fresh-event"))
        self.assert_error("schema_upgrade_required", body)
        self.assertEqual(self.database_bytes(), before)
        self.assertEqual(self.transport.calls, 0)

    def test_schema8_query_normalizes_legacy_failed(self):
        self.make_config()
        _write_schema8(Path(self.config) / "state.sqlite3", _load_vectors())
        clock = self._legacy_clock()
        vectors = _load_vectors()
        accepted = self._status(vectors[0], clock)
        queued = self._status(vectors[1], clock)
        sending = self._status(vectors[2], clock)
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["notification_id"], vectors[0]["notification_id"])
        self.assertTrue(accepted["service_confirmed"])
        self.assertEqual(queued["status"], "queued")
        self.assertNotEqual(queued["status"], "failed")
        self.assertEqual(queued["notification_id"], vectors[1]["notification_id"])
        self.assertEqual(sending["status"], "sending")
        self.assertEqual(sending["notification_id"], vectors[2]["notification_id"])
        self.assertNotEqual(vectors[0]["notification_id"], vectors[2]["notification_id"])
        cancelled, _body = self._one("aiusage", "cancel-legacy-1", clock)
        expired, _body = self._one("aiusage", "expire-legacy-1", clock)
        retry, retry_body = self._one("aiusage", "retry-legacy-1", clock)
        secret, secret_body = self._one("aiusage", "secret-legacy-1", clock)
        self.assertEqual(cancelled, "cancelled")
        self.assertEqual(expired, "expired")
        self.assertEqual(retry, "queued")
        self.assertTrue(retry_body["outcome_uncertain"])
        self.assertEqual(secret, "failed")
        self.assertEqual(secret_body["error_code"], "legacy_error_redacted")

    def test_direct_delivery_uses_same_notification_id(self):
        self.write_env()
        code, body, _text, _err = self.cli(_push("evt-1"))
        self.assertEqual(code, 0)
        self.assertEqual(body["status"], "accepted")
        self.assertTrue(body["service_confirmed"])
        self.assertFalse(body["outcome_uncertain"])
        self.assertEqual(self.transport.calls, 1)
        payload = self.transport.payloads[0]
        self.assertEqual(payload["id"], body["notification_id"])
        self.assertEqual(payload["device_key"], DEVICE_KEY)
        self.assertEqual(payload["group"], "notify-me")
        self.assertEqual(payload["level"], "critical")
        self.assertEqual(payload["sound"], "alarm")
        self.assertEqual(payload["volume"], 8)
        self.assertEqual(payload["icon"], ICON_URL)
        self.assertNotIn("device_key", body)

    def _legacy_clock(self):
        return ManualClock(MIGRATION_NOW)

    def _status(self, vector, clock):
        _code, body = self._one(vector["source"], vector["event_id"], clock)
        return body

    def _one(self, source, event_id, clock):
        code, body, _text, _err = self.cli(
            ["push-status", "--source", source, "--event-id", event_id], clock=clock
        )
        self.assertEqual(code, 0)
        return body["status"], body


class IsolationAndStateTests(IsolatedCase):
    def test_sources_and_exact_event_stay_isolated(self):
        self.write_env()
        first, _ = self._enqueue("aiusage", "shared-id", "P1")
        second, _ = self._enqueue("otherapp", "shared-id", "P2")
        self.assertNotEqual(first["notification_id"], second["notification_id"])
        code, body, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "shared-id"]
        )
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(body["previous_status"], "queued")
        other_code, other = self.row_status("otherapp", "shared-id")
        self.assertEqual(other_code, 0)
        self.assertEqual(other["status"], "queued")
        code, body, _text, _err = self.cli(
            ["push-drain", "--source", "otherapp", "--event-id", "shared-id"]
        )
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(self.transport.payloads[0]["id"], second["notification_id"])
        self.assertEqual(len(self.transport.payloads), 1)
        cancelled_code, cancelled = self.row_status("aiusage", "shared-id")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled_code, 0)

    def test_exact_drain_does_not_touch_the_neighbor(self):
        self.write_env()
        self._enqueue("aiusage", "keep-me", "P0")
        taken, _body = self._enqueue("aiusage", "send-me", "P0")
        code, body, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--event-id", "send-me"]
        )
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(body["notification_id"], taken["notification_id"])
        self.assertNotIn("results", body)
        kept_code, kept = self.row_status("aiusage", "keep-me")
        self.assertEqual(kept_code, 0)
        self.assertEqual(kept["status"], "queued")
        self.assertEqual(self.transport.calls, 1)

    def test_query_not_found_is_not_a_cancel_ack(self):
        self.write_env()
        self._enqueue("aiusage", "real-id", "P0")
        code, body, _text, _err = self.cli(
            ["push-status", "--source", "aiusage", "--event-id", "missing-id"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(set(body), {"ok", "protocol_version", "status"})
        self.assertEqual(body["status"], "not_found")
        self.assertNotIn("previous_status", body)
        code, body, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "missing-id"]
        )
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(body["previous_status"], "not_found")
        self.assertEqual(body["reason"], "tombstone")
        code, again, _text, _err = self.cli(
            ["push-status", "--source", "aiusage", "--event-id", "missing-id"]
        )
        self.assertEqual(again["status"], "cancelled")
        code, still, _text, _err = self.cli(
            ["push-status", "--source", "aiusage", "--event-id", "real-id"]
        )
        self.assertEqual(still["status"], "queued")

    def test_six_states_match_dedup_previous_status(self):
        self.write_env()
        queued, _body = self._enqueue("aiusage", "state-queued", "P0")
        again, _text = self._push_result(_push("state-queued", extra=["--enqueue-only"]))
        self.assertEqual(again["status"], "deduplicated")
        self.assertEqual(again["previous_status"], "queued")
        self.assertEqual(again["notification_id"], queued["notification_id"])
        self.assertEqual(again["expires_at"], queued["expires_at"])

        self.transport = FakeBarkTransport()
        sending_env = self.env(NOTIFY_ME_TEST_FAULT="after-accept")
        code, broken, _text, _err = self.cli(_push("state-sending"), env=sending_env)
        self.assert_error("delivery_interrupted", broken)
        _code, sending = self.row_status("aiusage", "state-sending")
        self.assertEqual(sending["status"], "sending")
        code, replay, _text, _err = self.cli(_push("state-sending"), env=sending_env)
        self.assertEqual(replay["status"], "deduplicated")
        self.assertEqual(replay["previous_status"], "sending")
        self.assertEqual(self.transport.calls, 1)

        self.transport = FakeBarkTransport()
        accepted, _text = self._push_result(_push("state-accepted"))
        self.assertEqual(accepted["status"], "accepted")
        calls = self.transport.calls
        replay, _text = self._push_result(_push("state-accepted"))
        self.assertEqual(replay["previous_status"], "accepted")
        self.assertEqual(self.transport.calls, calls)

        self.transport = FakeBarkTransport(
            result=TransportResult(False, False, "bark_rejected", 400)
        )
        failed, _text = self._push_result(_push("state-failed"))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "bark_rejected")
        calls = self.transport.calls
        replay, _text = self._push_result(_push("state-failed"))
        self.assertEqual(replay["previous_status"], "failed")
        self.assertEqual(self.transport.calls, calls)

        self.transport = FakeBarkTransport()
        expiring, _text = self._push_result(
            _push("state-expired", extra=["--enqueue-only", "--expires-at", "1700000010"])
        )
        self.assertEqual(expiring["expires_at"], 1700000010)
        self.clock.advance(11)
        code, drained, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--event-id", "state-expired"]
        )
        self.assertEqual(drained["status"], "expired")
        self.assertEqual(self.transport.calls, 0)
        replay, _text = self._push_result(_push("state-expired"))
        self.assertEqual(replay["previous_status"], "expired")
        self.assertNotEqual(replay["status"], "accepted")

        self.transport = FakeBarkTransport()
        self._enqueue("aiusage", "state-cancelled", "P1")
        code, cancelled, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "state-cancelled"]
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["previous_status"], "queued")
        replay, _text = self._push_result(_push("state-cancelled", priority="P1"))
        self.assertEqual(replay["status"], "deduplicated")
        self.assertEqual(replay["previous_status"], "cancelled")
        self.assertEqual(self.transport.calls, 0)
        code, conflict, _text, _err = self.cli(
            _push("state-cancelled", priority="P1", title="Other", body="Body")
        )
        self.assert_error("event_conflict", conflict)
        _code, still_cancelled = self.row_status("aiusage", "state-cancelled")
        self.assertEqual(still_cancelled["status"], "cancelled")
        self.assertEqual(self.transport.calls, 0)

    def test_content_conflict_does_not_change_state(self):
        self.write_env()
        queued, _body = self._enqueue("aiusage", "evt-1", "P0")
        code, body, _text, _err = self.cli(_push("evt-1", title="Changed"))
        self.assert_error("event_conflict", body)
        _code, again = self.row_status("aiusage", "evt-1")
        self.assertEqual(again["status"], "queued")
        self.assertEqual(again["expires_at"], queued["expires_at"])
        self.assertEqual(self.transport.calls, 0)

    def test_ttl_is_frozen_at_the_first_business_time(self):
        self.write_env()
        code, body, _text, _err = self.cli(
            _push(
                "evt-1",
                extra=["--enqueue-only", "--created-at", "1700000000", "--expires-at", "1999999999"],
            )
        )
        self.assertEqual(body["expires_at"], 1700000900)
        code, late, _text, _err = self.cli(
            _push("evt-skew", extra=["--enqueue-only", "--created-at", "1700000121"])
        )
        self.assert_error("created_at_invalid", late)
        code, edge, _text, _err = self.cli(
            _push("evt-edge", extra=["--enqueue-only", "--created-at", "1700000120"])
        )
        self.assertEqual(edge["status"], "queued")
        self.assertEqual(edge["expires_at"], 1700000120 + 900)
        self.clock.advance(10)
        code, again, _text, _err = self.cli(_push("evt-1", extra=["--enqueue-only"]))
        self.assertEqual(again["status"], "deduplicated")
        self.assertEqual(again["expires_at"], 1700000900)

    def _enqueue(self, source, event_id, priority):
        code, body, text, _err = self.cli(
            _push(event_id, priority=priority, source=source, extra=["--enqueue-only"])
        )
        self.assertEqual(code, 0, text)
        self.assertEqual(body["status"], "queued")
        return body, text

    def _push_result(self, argv):
        code, body, text, _err = self.cli(argv)
        self.assertEqual(code, 0, text)
        return body, text


class PriorityBudgetTests(IsolatedCase):
    def test_backoff_and_future_next_attempt_send_nothing(self):
        self.assertEqual(backoff_seconds("critical", 0), 30)
        self.assertEqual(backoff_seconds("timeSensitive", 0), 120)
        self.assertEqual(backoff_seconds("active", 0), 300)
        self.write_env()
        self.transport = FakeBarkTransport(
            result=TransportResult(False, True, "network_error", None)
        )
        code, body, _text, _err = self.cli(_push("evt-1"))
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["next_attempt_at"], 1700000030)
        self.assertEqual(body["error_code"], "network_error")
        self.assertEqual(self.transport.calls, 2)
        self.transport = FakeBarkTransport()
        code, waiting, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--event-id", "evt-1"]
        )
        self.assertEqual(waiting["status"], "queued")
        self.assertEqual(self.transport.calls, 0)
        self.clock.advance(30)
        code, sent, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--event-id", "evt-1"]
        )
        self.assertEqual(sent["status"], "accepted")
        self.assertEqual(sent["notification_id"], body["notification_id"])
        self.assertEqual(sent["expires_at"], body["expires_at"])
        self.assertEqual(self.transport.calls, 1)

    def test_p0_wins_and_batch_respects_count_and_time(self):
        self.write_env()
        p2, _text = self._quiet("p2-item", "P2")
        p0, _text = self._quiet("p0-item", "P0")
        code, body, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--max-items", "1"]
        )
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["processed"], 1)
        self.assertEqual(body["results"][0]["notification_id"], p0["notification_id"])
        self.assertNotEqual(body["results"][0]["notification_id"], p2["notification_id"])
        _code, still = self.row_status("aiusage", "p2-item")
        self.assertEqual(still["status"], "queued")

        self.transport = FakeBarkTransport()
        for name in ("a-item", "b-item", "c-item"):
            self._quiet(name, "P1")
            self.clock.advance(1)
        code, batch, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--max-items", "2"]
        )
        self.assertEqual(batch["processed"], 2)
        self.assertEqual(self.transport.calls, 2)
        _code, waiting = self.row_status("aiusage", "c-item")
        self.assertEqual(waiting["status"], "queued")

        self.transport = _JumpTransport(self.clock)
        self._quiet("late-1", "P0")
        self.clock.advance(1)
        self._quiet("late-2", "P0")
        code, limited, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--max-items", "5", "--budget-ms", "1000"]
        )
        self.assertEqual(limited["processed"], 1)
        self.assertEqual(self.transport.calls, 1)
        _code, held = self.row_status("aiusage", "late-2")
        self.assertEqual(held["status"], "queued")

    def test_zero_budget_and_missing_effect_and_capacity(self):
        self.write_env()
        queued, _text = self._quiet("evt-1", "P0")
        code, body, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--event-id", "evt-1", "--budget-ms", "0"]
        )
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["expires_at"], queued["expires_at"])
        self.assertEqual(self.transport.calls, 0)
        code, missing, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--event-id", "no-such", "--budget-ms", "0"]
        )
        self.assertEqual(missing["status"], "not_found")
        self.assertNotIn("previous_status", missing)
        code, body, _text, _err = self.cli(_push("needs-effect", priority="P3", extra=["--enqueue-only"]))
        self.assert_error("effect_required", body)
        self.assertEqual(self.event_count(), 1)

        limited = self.env(NOTIFY_ME_TEST_MAX_ACTIVE_SOURCE="1")
        code, full, _text, _err = self.cli(
            _push("evt-2", extra=["--enqueue-only"]), env=limited
        )
        self.assert_error("queue_full", full)
        _code, still = self.row_status("aiusage", "evt-1")
        self.assertEqual(still["status"], "queued")
        self.assertEqual(still["notification_id"], queued["notification_id"])

        rows = self.env(NOTIFY_ME_TEST_MAX_ROWS="1")
        code, blocked, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "tomb-me"], env=rows
        )
        self.assert_error("queue_full", blocked)
        self.assertNotIn("previous_status", blocked)
        self.assertNotEqual(blocked["status"], "cancelled")
        code, absent, _text, _err = self.cli(
            ["push-status", "--source", "aiusage", "--event-id", "tomb-me"]
        )
        self.assertEqual(absent["status"], "not_found")
        _code, alive = self.row_status("aiusage", "evt-1")
        self.assertEqual(alive["status"], "queued")

    def test_tombstone_ignores_active_cap_until_rows_are_full(self):
        self.write_env()
        code, queued, _text, _err = self.cli(_push("evt-1", extra=["--enqueue-only"]))
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(self.event_count(), 1)
        per_source = self.env(NOTIFY_ME_TEST_MAX_ROWS_SOURCE="1")
        code, blocked, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "unknown-source-cap"],
            env=per_source,
        )
        self.assert_error("queue_full", blocked)
        self.assertNotEqual(blocked["status"], "cancelled")
        self.assertEqual(self.row_status("aiusage", "unknown-source-cap")[1]["status"], "not_found")
        self.assertEqual(self.event_count(), 1)

        active = self.env(NOTIFY_ME_TEST_MAX_ACTIVE="1", NOTIFY_ME_TEST_MAX_ACTIVE_SOURCE="1")
        code, tomb, text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "unknown-1"],
            env=active,
        )
        self.assertEqual(code, 0, text)
        self.assertEqual(tomb["status"], "cancelled")
        self.assertEqual(tomb["previous_status"], "not_found")
        self.assertEqual(tomb["reason"], "tombstone")
        self.assertFalse(tomb["retryable"])
        self.assertEqual(self.event_count(), 2)
        self.assertEqual(self.transport.calls, 0)
        code, full, _text, _err = self.cli(
            _push("evt-2", extra=["--enqueue-only"]), env=active
        )
        self.assert_error("queue_full", full)
        self.assertEqual(self.event_count(), 2)
        _code, still = self.row_status("aiusage", "evt-1")
        self.assertEqual(still["status"], "queued")

        capped = self.env(NOTIFY_ME_TEST_MAX_ROWS="2")
        code, blocked, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "unknown-2"],
            env=capped,
        )
        self.assert_error("queue_full", blocked)
        self.assertNotIn("previous_status", blocked)
        self.assertEqual(self.row_status("aiusage", "unknown-2")[1]["status"], "not_found")
        self.assertEqual(self.event_count(), 2)
        self.assertEqual(self.row_status("aiusage", "unknown-1")[1]["status"], "cancelled")

    def test_prune_drops_old_terminals_and_keeps_live_work(self):
        self.clock = ManualClock(1000)
        self.write_env()
        env = self.env(NOTIFY_ME_TEST_RETENTION_SECONDS="100")
        code, live, _text, _err = self.cli(
            _push("live-p0", extra=["--enqueue-only", "--created-at", "1000"]), env=env
        )
        self.assertEqual(live["status"], "queued")
        code, tomb, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "old-tomb"], env=env
        )
        self.assertEqual(tomb["previous_status"], "not_found")
        self.clock.advance(105)
        code, _body, _text, _err = self.cli(
            _push("newer", extra=["--enqueue-only"]), env=env
        )
        _code, still = self.cli(
            ["push-status", "--source", "aiusage", "--event-id", "live-p0"], env=env
        )[:2]
        self.assertEqual(still["status"], "queued")
        _code, gone, _text, _err = self.cli(
            ["push-status", "--source", "aiusage", "--event-id", "old-tomb"], env=env
        )
        self.assertEqual(gone["status"], "not_found")
        code, revived, _text, _err = self.cli(
            _push("old-tomb", extra=["--created-at", "1000"]), env=env
        )
        self.assert_error("created_at_too_old", revived)
        _code, still_gone, _text, _err = self.cli(
            ["push-status", "--source", "aiusage", "--event-id", "old-tomb"], env=env
        )
        self.assertEqual(still_gone["status"], "not_found")

    def _quiet(self, event_id, priority):
        code, body, text, _err = self.cli(
            _push(event_id, priority=priority, extra=["--enqueue-only"])
        )
        self.assertEqual(code, 0, text)
        return body, text


class _JumpTransport(FakeBarkTransport):
    def __init__(self, clock):
        FakeBarkTransport.__init__(self)
        self.clock = clock

    def send(self, endpoint, payload):
        result = FakeBarkTransport.send(self, endpoint, payload)
        self.clock._mono += 30
        return result
