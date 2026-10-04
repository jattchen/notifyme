# -*- coding: utf-8 -*-
"""Protocol bounds that do not touch config or state."""

import json
import unittest

from notify_me_app.protocol import STDOUT_LIMIT, dumps, error_payload, metadata


def _payload_with_json_length(target):
    overhead = len(
        json.dumps(
            {"ok": True, "pad": ""},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    payload = {"ok": True, "pad": "a" * (target - overhead)}
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(raw) != target:
        raise AssertionError("json length {} != {}".format(len(raw), target))
    return payload


class ProtocolBoundTests(unittest.TestCase):
    def test_stdout_limit_includes_trailing_newline(self):
        at_limit = dumps(_payload_with_json_length(STDOUT_LIMIT - 1)).encode("utf-8")
        self.assertEqual(len(at_limit), STDOUT_LIMIT)
        self.assertTrue(json.loads(at_limit.decode("utf-8"))["ok"])

        over = dumps(_payload_with_json_length(STDOUT_LIMIT)).encode("utf-8")
        self.assertLessEqual(len(over), STDOUT_LIMIT)
        self.assertNotEqual(len(over), STDOUT_LIMIT + 1)
        body = json.loads(over.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "response_too_large")
        self.assertTrue(over.endswith(b"\n"))

    def test_unknown_lowercase_code_is_redacted(self):
        view = metadata(
            "failed", 1, None, 100, None, False, "fake_credential_lowercase", False, "nm_x", "P1"
        )
        self.assertEqual(view["error_code"], "legacy_error_redacted")
        self.assertNotIn("fake_credential_lowercase", json.dumps(view))

        known = metadata(
            "failed", 1, None, 100, None, False, "network_error", False, "nm_x", "P1"
        )
        self.assertEqual(known["error_code"], "network_error")

    def test_command_error_rejects_unknown_code(self):
        self.assertEqual(error_payload("fake_credential_lowercase")["error"]["code"], "internal_error")
        self.assertEqual(error_payload("configuration_missing")["error"]["code"], "configuration_missing")

    def test_accepted_uncertainty_is_historical_not_unconfirmed(self):
        view = metadata(
            "accepted", 2, None, 900, 800, False, None, True, "nm_same", "P0"
        )
        self.assertEqual(view["service_confirmed"], True)
        self.assertEqual(view["outcome_uncertain"], True)
        self.assertEqual(view["uncertainty_applies_to"], "prior_attempt")
        self.assertEqual(view["retryable"], False)
        self.assertFalse(view["active_lease"])
        self.assertIsNone(view["lease_until"])
        self.assertFalse(view["ttl_elapsed"])
        self.assertFalse(view["remote_withdrawn"])


if __name__ == "__main__":
    unittest.main()
