# -*- coding: utf-8 -*-
"""Protocol v1 envelope. Every stdout body is one bounded JSON object."""

import json

from .build_info import PROTOCOL_VERSION
from .errors import NotifyMeError
from .schema import EVENT_STATES


STDOUT_LIMIT = 64 * 1024
PREVIOUS_STATES = EVENT_STATES + ("not_found",)

# Event last_error values this program is willing to show. Anything else,
# including lowercase text that merely looks like a code, is redacted.
EVENT_ERROR_CODES = frozenset((
    "bark_rejected",
    "bark_retryable",
    "cancelled",
    "delivery_uncertain",
    "expired",
    "invalid_payload",
    "invalid_response",
    "legacy_error_redacted",
    "network_error",
    "permanent_failure",
    "permanent_http",
    "redirect_rejected",
    "retryable_http",
))

COMMAND_ERROR_CODES = frozenset((
    "binding_exists",
    "binding_invalid",
    "binding_permissions",
    "binding_unavailable",
    "binding_write_failed",
    "config_permissions",
    "configuration_missing",
    "created_at_invalid",
    "created_at_too_old",
    "effect_required",
    "event_conflict",
    "event_expired",
    "host_path_forbidden",
    "insecure_bark_url",
    "delivery_interrupted",
    "install_interrupted",
    "internal_error",
    "invalid_arguments",
    "invalid_bark_key",
    "invalid_bark_url",
    "invalid_binding",
    "invalid_body",
    "invalid_effect",
    "invalid_event_id",
    "invalid_payload",
    "invalid_priority",
    "invalid_source",
    "invalid_title",
    "launcher_invalid",
    "launcher_required",
    "lock_busy",
    "not_initialized",
    "protocol_mismatch",
    "python_unsupported",
    "queue_full",
    "recovery_required",
    "recovery_unavailable",
    "response_too_large",
    "schema_upgrade_required",
    "source_required",
    "state_backup_failed",
    "state_conflict",
    "state_corrupt",
    "state_database_exists",
    "state_database_unavailable",
    "state_permissions",
    "state_schema_error",
    "state_schema_unsupported",
    "unknown_status",
    "unsafe_config_path",
    "unsafe_state_path",
    "unsupported_command",
))

CAPABILITIES = {
    "commands": [
        "version",
        "status",
        "push",
        "push-status",
        "push-cancel",
        "push-drain",
        "install",
        "upgrade",
        "migrate-binding",
        "recover",
    ],
    "states": list(EVENT_STATES),
    "enqueue_only": True,
    "source_required_drain": True,
    "force_drain": False,
    "exactly_once": False,
    "delivery": "at_least_once_same_id_within_ttl",
    "phone_duplicate_possible": True,
    "outcome_uncertain_on_accepted": "prior_attempt",
    "unknown_event_query": "not_found",
    "unknown_event_cancel": "cancelled",
    "unknown_event_cancel_previous_status": "not_found",
    "current_lease_fields": ["lease_until", "active_lease"],
    "remote_withdrawn": False,
    "readable_state_schemas": [8, 9],
    "writable_state_schema": 9,
}


def require_state(value):
    if value not in EVENT_STATES:
        raise NotifyMeError("unknown_status")
    return value


def require_previous(value):
    if value not in PREVIOUS_STATES:
        raise NotifyMeError("unknown_status")
    return value


def error_payload(code):
    if code not in COMMAND_ERROR_CODES:
        code = "internal_error"
    return {
        "ok": False,
        "protocol_version": PROTOCOL_VERSION,
        "status": "error",
        "error": {"code": code},
    }


def _compact(payload):
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def dumps(payload):
    """Serialize one object. The trailing newline counts toward 64KiB."""

    if not isinstance(payload, dict):
        payload = error_payload("internal_error")
    text = _compact(payload)
    if len(text.encode("utf-8")) + 1 > STDOUT_LIMIT:
        text = _compact(error_payload("response_too_large"))
    body = text + "\n"
    if len(body.encode("utf-8")) > STDOUT_LIMIT:
        body = _compact(error_payload("response_too_large")) + "\n"
    return body


def publish_event_error(value):
    """Keep a known event code. Unknown text becomes legacy_error_redacted."""

    if value is None or value == "":
        return None
    if isinstance(value, str) and value in EVENT_ERROR_CODES:
        return value
    return "legacy_error_redacted"


def normalize_legacy(status, last_error, outbox, now):
    """Map a schema 8 row to one public state.

    Historical SQL stored retryable failure, cancellation, and expiry all as
    failed. The outbox and last_error decide the public state. Legacy failed
    is not automatically the public failed state.
    """

    if status == "accepted":
        return "accepted", False
    if last_error == "cancelled":
        return "cancelled", False
    if last_error == "expired":
        return "expired", False
    if outbox is not None:
        lease_until = outbox.get("lease_until")
        lease_token = outbox.get("lease_token")
        if lease_token and lease_until is not None and float(lease_until) > float(now):
            return "sending", False
        uncertain = bool(lease_token)
        return "queued", uncertain
    if status == "sending":
        return "sending", True
    if status == "failed":
        return "failed", False
    raise NotifyMeError("unknown_status")


def metadata(
    status,
    attempts,
    next_attempt_at,
    expires_at,
    accepted_at,
    cancel_requested,
    error_code,
    outcome_uncertain,
    notification_id,
    priority,
    lease_until=None,
    now=None,
):
    """Safe event fields shared by push-status and deduplicated.

    lease_until and active_lease describe the current lease only. A past
    outcome_uncertain flag on an accepted row is historical and does not mean
    a lease is still held. This view never claims the phone notification was
    withdrawn.
    """

    state = require_state(status)
    active = state in ("queued", "sending")
    uncertain = bool(outcome_uncertain)
    held_until = _whole(lease_until)
    active_lease = False
    ttl_elapsed = False
    if now is not None:
        current = int(now)
        if held_until is not None and held_until > current:
            active_lease = True
        if (
            active
            and expires_at is not None
            and int(expires_at) <= current
            and not active_lease
        ):
            ttl_elapsed = True
        if active and not active_lease and (state == "sending" or held_until is not None):
            uncertain = True
    if not uncertain:
        uncertainty_applies_to = None
    elif state == "accepted":
        uncertainty_applies_to = "prior_attempt"
    else:
        uncertainty_applies_to = "current_delivery"
    retryable = active and not active_lease and not bool(cancel_requested) and not ttl_elapsed
    return {
        "attempts": int(attempts or 0),
        "next_attempt_at": _whole(next_attempt_at) if active else None,
        "expires_at": _whole(expires_at),
        "accepted_at": _whole(accepted_at),
        "retryable": retryable,
        "cancel_requested": bool(cancel_requested),
        "error_code": publish_event_error(error_code),
        "outcome_uncertain": uncertain,
        "uncertainty_applies_to": uncertainty_applies_to,
        "service_confirmed": state == "accepted",
        "notification_id": notification_id,
        "priority": priority,
        "lease_until": held_until,
        "active_lease": active_lease,
        "ttl_elapsed": ttl_elapsed,
        "remote_withdrawn": False,
    }


def _whole(value):
    if value is None:
        return None
    return int(value)


def operation(status, meta, previous_status=None, extra=None):
    body = {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "status": status,
    }
    if previous_status is not None:
        body["previous_status"] = require_previous(previous_status)
    if meta:
        body.update(meta)
    if extra:
        body.update(extra)
    if body.get("status") == "deduplicated":
        require_previous(body.get("previous_status"))
    elif body.get("status") in EVENT_STATES:
        require_state(body["status"])
    elif body.get("status") == "not_found":
        pass
    elif body.get("status") in ("not_pending", "completed", "dry_run", "installed", "recovered", "bound"):
        pass
    else:
        raise NotifyMeError("unknown_status")
    return body
