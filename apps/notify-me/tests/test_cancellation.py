# -*- coding: utf-8 -*-
"""Group 5: cancel races, leases, and the uncertain same-id retry."""

import json
import os
import sqlite3

from notify_me_app.paths import resolve_paths
from notify_me_app.storage import StateStore, limits_from_env
from notify_me_app.transport import FakeBarkTransport, TransportResult

from tests.support import IsolatedCase
from tests.test_outbox import _push


class _CancelDuringSend(FakeBarkTransport):
    """The phone accepts while a cancel is persisted against the live lease."""

    def __init__(self, case, event_id):
        FakeBarkTransport.__init__(self)
        self.case = case
        self.event_id = event_id
        self.cancel_result = None

    def send(self, endpoint, payload):
        result = FakeBarkTransport.send(self, endpoint, payload)
        _code, body, _text, _err = self.case.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", self.event_id]
        )
        self.cancel_result = body
        return result


class _StealTransport(FakeBarkTransport):
    """Accept on the wire, then change the row before finalize runs."""

    def __init__(self, database, clock, mode):
        FakeBarkTransport.__init__(self)
        self.database = database
        self.clock = clock
        self.mode = mode

    def send(self, endpoint, payload):
        result = FakeBarkTransport.send(self, endpoint, payload)
        connection = sqlite3.connect(self.database)
        try:
            if self.mode == "keep-sending":
                connection.execute(
                    "UPDATE application_outbox SET lease_token='stolen' WHERE notification_id=?",
                    (payload["id"],),
                )
            elif self.mode == "already-accepted":
                now = int(self.clock.now())
                connection.execute(
                    "UPDATE application_events SET status='accepted', accepted_at=?, "
                    "last_error=NULL, updated_at=? WHERE notification_id=?",
                    (now, now, payload["id"]),
                )
                connection.execute(
                    "DELETE FROM application_outbox WHERE notification_id=?",
                    (payload["id"],),
                )
            connection.commit()
        finally:
            connection.close()
        return result


class CancellationTests(IsolatedCase):
    def test_cancel_before_push_tombstone_blocks_every_body(self):
        self.write_env()
        code, body, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "evt-1"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(body["previous_status"], "not_found")
        self.assertEqual(body["reason"], "tombstone")
        code, again, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "evt-1"]
        )
        self.assertEqual(again["status"], "cancelled")
        self.assertEqual(again["previous_status"], "cancelled")
        code, replay, _text, _err = self.cli(_push("evt-1", title="Different", body="Text"))
        self.assertEqual(replay["status"], "deduplicated")
        self.assertEqual(replay["previous_status"], "cancelled")
        self.assertEqual(self.transport.calls, 0)
        _code, view = self.row_status("aiusage", "evt-1")
        self.assertEqual(view["status"], "cancelled")

    def test_queued_cancel_is_atomic_and_accepted_is_too_late(self):
        self.write_env()
        self.cli(_push("evt-q", extra=["--enqueue-only"]))
        code, body, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "evt-q"]
        )
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(body["previous_status"], "queued")
        self.assertNotIn("reason", body)
        code, sent, _text, _err = self.cli(_push("evt-a"))
        self.assertEqual(sent["status"], "accepted")
        code, late, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "evt-a"]
        )
        self.assertEqual(late["status"], "not_pending")
        self.assertEqual(late["previous_status"], "accepted")
        self.assertEqual(late["reason"], "accepted")
        _code, view = self.row_status("aiusage", "evt-a")
        self.assertEqual(view["status"], "accepted")
        self.assertTrue(view["service_confirmed"])

    def test_expired_lease_cancel_finishes_without_drain(self):
        self.write_env()
        _code, queued, _text, _err = self.cli(_push("evt-1", extra=["--enqueue-only"]))
        self.assertEqual(queued["expires_at"], 1700000900)
        self._set_lease("evt-1", active=True)
        code, inflight, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "evt-1"]
        )
        self.assertEqual(inflight["status"], "not_pending")
        self.assertEqual(inflight["reason"], "in_flight")
        self.assertTrue(inflight["cancel_requested"])
        self.assertTrue(inflight["active_lease"])
        self.assertEqual(inflight["lease_until"], 1700000060)
        self.assertFalse(inflight["remote_withdrawn"])
        before = self._stored("evt-1")
        _code, query = self.row_status("aiusage", "evt-1")
        self.assertEqual(query["status"], "sending")
        self.assertTrue(query["active_lease"])
        self.assertEqual(query["lease_until"], 1700000060)
        self.assertFalse(query["outcome_uncertain"])
        self.assertFalse(query["retryable"])
        self.assertEqual(query["expires_at"], 1700000900)
        self.assertEqual(self._stored("evt-1"), before)

        self.clock.advance(61)
        before = self._stored("evt-1")
        _code, waiting = self.row_status("aiusage", "evt-1")
        self.assertEqual(waiting["status"], "sending")
        self.assertFalse(waiting["active_lease"])
        self.assertEqual(waiting["lease_until"], 1700000060)
        self.assertTrue(waiting["cancel_requested"])
        self.assertTrue(waiting["outcome_uncertain"])
        self.assertEqual(waiting["uncertainty_applies_to"], "current_delivery")
        self.assertFalse(waiting["retryable"])
        self.assertFalse(waiting["ttl_elapsed"])
        self.assertFalse(waiting["service_confirmed"])
        self.assertFalse(waiting["remote_withdrawn"])
        self.assertEqual(waiting["expires_at"], 1700000900)
        self.assertEqual(self._stored("evt-1"), before)
        self.assertEqual(self._stored("evt-1")["outcome_uncertain"], 0)

        code, done, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "evt-1"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(done["status"], "cancelled")
        self.assertEqual(done["previous_status"], "sending")
        self.assertEqual(done["reason"], "lease_expired")
        self.assertTrue(done["outcome_uncertain"])
        self.assertEqual(done["error_code"], "delivery_uncertain")
        self.assertFalse(done["active_lease"])
        self.assertFalse(done["remote_withdrawn"])
        self.assertEqual(done["expires_at"], 1700000900)
        self.assertEqual(self.transport.calls, 0)
        _code, view = self.row_status("aiusage", "evt-1")
        self.assertEqual(view["status"], "cancelled")
        self.assertTrue(view["outcome_uncertain"])
        self.assertFalse(view["retryable"])
        self.assertFalse(view["remote_withdrawn"])
        code, again, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "evt-1"]
        )
        self.assertEqual(again["status"], "cancelled")
        self.assertEqual(again["previous_status"], "cancelled")

    def test_query_past_ttl_does_not_write_or_extend_ttl(self):
        self.write_env()
        self.cli(_push("evt-1", extra=["--enqueue-only"]))
        self._set_lease("evt-1", active=True)
        self.clock.advance(100000)
        before = self._stored("evt-1")
        _code, view = self.row_status("aiusage", "evt-1")
        self.assertEqual(view["status"], "sending")
        self.assertTrue(view["ttl_elapsed"])
        self.assertFalse(view["active_lease"])
        self.assertEqual(view["lease_until"], 1700000060)
        self.assertEqual(view["expires_at"], 1700000900)
        self.assertTrue(view["outcome_uncertain"])
        self.assertFalse(view["retryable"])
        self.assertFalse(view["service_confirmed"])
        self.assertFalse(view["remote_withdrawn"])
        self.assertNotEqual(view["status"], "cancelled")
        self.assertEqual(self._stored("evt-1"), before)
        code, health, _text, _err = self.cli(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(health["status"], "ready")
        self.assertEqual(self._stored("evt-1"), before)
        _code, again = self.row_status("aiusage", "evt-1")
        self.assertEqual(again["expires_at"], 1700000900)
        self.assertEqual(self._stored("evt-1")["expires_at"], 1700000900)
        self.assertEqual(self._stored("evt-1")["status"], "sending")
        code, done, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "evt-1"]
        )
        self.assertEqual(done["status"], "cancelled")
        self.assertEqual(done["reason"], "lease_expired")
        self.assertEqual(done["expires_at"], 1700000900)
        self.assertTrue(done["outcome_uncertain"])
        self.assertFalse(done["remote_withdrawn"])
        self.assertEqual(self.transport.calls, 0)

    def test_active_lease_cancel_is_in_flight_and_failure_does_not_requeue(self):
        self.write_env()
        self.cli(_push("evt-1", extra=["--enqueue-only"]))
        self._set_lease("evt-1", active=True)
        code, body, _text, _err = self.cli(
            ["push-cancel", "--source", "aiusage", "--event-id", "evt-1"]
        )
        self.assertEqual(body["status"], "not_pending")
        self.assertEqual(body["previous_status"], "sending")
        self.assertEqual(body["reason"], "in_flight")
        self.assertTrue(body["cancel_requested"])
        _code, view = self.row_status("aiusage", "evt-1")
        self.assertEqual(view["status"], "sending")
        self.transport = FakeBarkTransport(
            result=TransportResult(False, True, "network_error", None)
        )
        self.clock.advance(60)
        code, done, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--event-id", "evt-1"]
        )
        self.assertEqual(done["status"], "cancelled")
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(done["error_code"], "delivery_uncertain")
        code, replay, _text, _err = self.cli(_push("evt-1"))
        self.assertEqual(replay["previous_status"], "cancelled")
        self.assertEqual(self.transport.calls, 0)

    def test_accepted_during_cancel_cannot_be_undone(self):
        self.write_env()
        self.transport = _CancelDuringSend(self, "evt-1")
        code, accepted, _text, _err = self.cli(_push("evt-1"))
        self.assertEqual(code, 0)
        self.assertEqual(accepted["status"], "accepted")
        self.assertTrue(accepted["service_confirmed"])
        self.assertEqual(accepted["reason"], "accepted")
        self.assertTrue(accepted["cancel_late"])
        self.assertEqual(self.transport.cancel_result["reason"], "in_flight")
        self.assertEqual(self.transport.cancel_result["previous_status"], "sending")
        _code, view = self.row_status("aiusage", "evt-1")
        self.assertEqual(view["status"], "accepted")
        self.assertFalse(view["retryable"])
        self.assertTrue(view["service_confirmed"])

    def test_expired_lease_retries_same_id_and_keeps_historical_uncertainty(self):
        self.write_env()
        env = self.env(NOTIFY_ME_TEST_FAULT="after-accept")
        code, broken, _text, _err = self.cli(_push("evt-1"), env=env)
        self.assert_error("delivery_interrupted", broken)
        first = self.row_status("aiusage", "evt-1")[1]
        self.assertEqual(first["status"], "sending")
        self.assertFalse(first["outcome_uncertain"])
        notification_id = first["notification_id"]
        self.transport = FakeBarkTransport()
        self.clock.advance(59)
        code, held, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--event-id", "evt-1"]
        )
        self.assertEqual(held["status"], "sending")
        self.assertEqual(self.transport.calls, 0)
        self.clock.advance(1)
        code, retried, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--event-id", "evt-1"]
        )
        self.assertEqual(retried["status"], "accepted")
        self.assertEqual(retried["notification_id"], notification_id)
        self.assertEqual(retried["expires_at"], first["expires_at"])
        self.assertTrue(retried["service_confirmed"])
        self.assertTrue(retried["outcome_uncertain"])
        self.assertEqual(retried["uncertainty_applies_to"], "prior_attempt")
        self.assertFalse(retried["retryable"])
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual(self.transport.payloads[0]["id"], notification_id)
        _code, view = self.row_status("aiusage", "evt-1")
        self.assertEqual(view["status"], "accepted")
        self.assertTrue(view["outcome_uncertain"])
        self.assertEqual(view["uncertainty_applies_to"], "prior_attempt")
        self.assertTrue(view["service_confirmed"])
        calls = self.transport.calls
        code, replay, _text, _err = self.cli(_push("evt-1"))
        self.assertEqual(replay["previous_status"], "accepted")
        self.assertEqual(replay["expires_at"], first["expires_at"])
        self.assertEqual(self.transport.calls, calls)

    def test_lost_finalize_does_not_invent_or_downgrade_acceptance(self):
        self.write_env()
        self.cli(_push("evt-send", extra=["--enqueue-only"]))
        database = os.path.join(self.config, "state.sqlite3")
        self.transport = _StealTransport(database, self.clock, "keep-sending")
        code, body, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--event-id", "evt-send"]
        )
        self.assertEqual(body["status"], "sending")
        self.assertFalse(body["service_confirmed"])
        self.assertEqual(body["uncertainty_applies_to"], "current_delivery")
        self.assertEqual(body["error_code"], "delivery_uncertain")
        self.assertFalse(body["finalize_applied"])
        self.assertTrue(body["delivery_uncertain"])
        _code, view = self.row_status("aiusage", "evt-send")
        self.assertEqual(view["status"], "sending")

        self.cli(_push("evt-won", extra=["--enqueue-only"]))
        self.transport = _StealTransport(database, self.clock, "already-accepted")
        code, won, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--event-id", "evt-won"]
        )
        self.assertEqual(won["status"], "accepted")
        self.assertTrue(won["service_confirmed"])
        self.assertEqual(won["uncertainty_applies_to"], "prior_attempt")
        self.assertTrue(won["outcome_uncertain"])
        self.assertFalse(won["finalize_applied"])
        _code, view = self.row_status("aiusage", "evt-won")
        self.assertEqual(view["status"], "accepted")
        self.assertTrue(view["service_confirmed"])
        self.assertTrue(view["outcome_uncertain"])
        self.assertEqual(view["uncertainty_applies_to"], "prior_attempt")

    def test_late_token_cannot_resurrect_and_conflict_rolls_back(self):
        self.write_env()
        _code, accepted, _text, _err = self.cli(_push("evt-ok"))
        self.assertEqual(accepted["status"], "accepted")
        store = self._store()
        missed = store.finalize(
            {
                "notification_id": accepted["notification_id"],
                "lease_token": "stale-token",
                "level": "critical",
                "attempts": 0,
            },
            True,
            1,
            200,
            None,
            False,
        )
        self.assertFalse(missed["applied"])
        view = self.row_status("aiusage", "evt-ok")[1]
        self.assertEqual(view["status"], "accepted")
        self.assertTrue(view["outcome_uncertain"])

        self.cli(_push("evt-no", extra=["--enqueue-only"]))
        token = self._set_lease("evt-no", active=True)
        connection = sqlite3.connect(os.path.join(self.config, "state.sqlite3"))
        connection.execute(
            "UPDATE application_events SET status='cancelled', updated_at=111 WHERE notification_id=?",
            (self._notification("evt-no"),),
        )
        connection.commit()
        connection.close()
        from notify_me_app.errors import NotifyMeError

        with self.assertRaises(NotifyMeError) as caught:
            store.finalize(
                {
                    "notification_id": self._notification("evt-no"),
                    "lease_token": token,
                    "level": "critical",
                    "attempts": 0,
                },
                True,
                1,
                200,
                None,
                False,
            )
        self.assertEqual(caught.exception.code, "state_conflict")
        view = self.row_status("aiusage", "evt-no")[1]
        self.assertEqual(view["status"], "cancelled")
        connection = sqlite3.connect(os.path.join(self.config, "state.sqlite3"))
        try:
            status, updated_at = connection.execute(
                "SELECT status, updated_at FROM application_events WHERE notification_id=?",
                (self._notification("evt-no"),),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(status, "cancelled")
        self.assertEqual(updated_at, 111)

    def _stored(self, event_id):
        from notify_me_app.configuration import application_identity

        connection = sqlite3.connect(os.path.join(self.config, "state.sqlite3"))
        try:
            salt = json.loads(
                connection.execute(
                    "SELECT value_json FROM settings WHERE key='scope_salt'"
                ).fetchone()[0]
            )
            _source, _event, notification_id = application_identity(salt, "aiusage", event_id)
            row = connection.execute(
                "SELECT status, outcome_uncertain, expires_at, updated_at, cancel_requested "
                "FROM application_events WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
        finally:
            connection.close()
        return {
            "status": row[0],
            "outcome_uncertain": row[1],
            "expires_at": row[2],
            "updated_at": row[3],
            "cancel_requested": row[4],
        }

    def _store(self):
        paths = resolve_paths(self.env())
        return StateStore(paths, self.clock, limits_from_env(self.env()))

    def _notification(self, event_id):
        return self.row_status("aiusage", event_id)[1]["notification_id"]

    def _set_lease(self, event_id, active):
        notification_id = self._notification(event_id)
        token = "lease-{}-token".format(event_id)
        until = int(self.clock.now()) + (60 if active else -1)
        connection = sqlite3.connect(os.path.join(self.config, "state.sqlite3"))
        try:
            if active:
                connection.execute(
                    "UPDATE application_events SET status='sending' WHERE notification_id=? AND status='queued'",
                    (notification_id,),
                )
            connection.execute(
                "UPDATE application_outbox SET lease_token=?, lease_until=? WHERE notification_id=?",
                (token, until, notification_id),
            )
            connection.commit()
        finally:
            connection.close()
        return token
