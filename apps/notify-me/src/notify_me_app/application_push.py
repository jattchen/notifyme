# -*- coding: utf-8 -*-
"""Application push, query, cancel, and drain. version never calls this module."""

import copy
import json

from .configuration import parse_unix_seconds, safe_text, validate_event_id, validate_priority, validate_source
from .errors import NotifyMeError
from .paths import directory_fact, file_fact, path_exists, resolve_paths
from .protocol import CAPABILITIES, PROTOCOL_VERSION, operation
from .schema import SCHEMA_V8, SCHEMA_V9
from .storage import (
    StateStore,
    connect_readonly,
    create_fresh,
    inspect_database,
    limits_from_env,
    load_endpoint,
)
from .transport import TransportResult


def command_status(env, clock, source=None):
    """Read health. Does not create directories, databases, or migrations."""

    paths = resolve_paths(env)
    directory = directory_fact(paths.config_dir)
    if directory["status"] == "unsafe":
        raise NotifyMeError("config_permissions")
    if directory["status"] == "missing":
        binding = {"status": "missing", "mode": None, "private": None}
        database = _missing_database()
    else:
        binding = file_fact(paths.dotenv)
        database = inspect_database(paths.state_db)
    if database.get("error_code") in (
        "state_schema_unsupported",
        "state_permissions",
        "unsafe_state_path",
        "state_database_unavailable",
    ):
        raise NotifyMeError(database["error_code"])
    if binding["status"] == "unsafe":
        raise NotifyMeError("binding_permissions")
    endpoint = None
    bound = False
    if directory["status"] == "ready" and binding["status"] == "ready":
        endpoint = load_endpoint(paths)
        bound = True
    configuration = None
    outbox = None
    if database.get("status") == "ready":
        store = StateStore(paths, clock, limits_from_env(env))
        configuration = store.configuration_summary()
        source_key = _source_key(paths, source) if source else None
        outbox = store.outbox_summary(source_key)
    if not bound:
        status_name = "configuration_missing"
    elif database.get("status") != "ready":
        status_name = "not_initialized"
    else:
        status_name = "ready"
    public_db = {
        "status": database.get("status"),
        "schema_version": database.get("schema_version"),
        "writable": database.get("schema_version") == SCHEMA_V9,
        "private": database.get("private"),
        "integrity": database.get("integrity"),
        "migration_required": database.get("schema_version") == SCHEMA_V8,
    }
    body = {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "status": status_name,
        "config_dir": str(paths.config_dir),
        "bound": bound,
        "private_directory": directory,
        "binding": binding,
        "state_database": public_db,
        "configuration": configuration,
        "capabilities": CAPABILITIES,
        "migration_required": public_db["migration_required"],
    }
    if outbox is not None:
        body["application_outbox"] = outbox
    if bound and endpoint is not None:
        body["host"] = endpoint.host
    return body


def command_push(env, clock, transport, source, event_id, priority, title, body, created_at, expires_at, enqueue_only):
    source = validate_source(source)
    event_id = validate_event_id(event_id)
    priority = validate_priority(priority)
    title = safe_text(title, 80, "invalid_title")
    body_text = safe_text(body, 500, "invalid_body")
    now = int(clock.now())
    business_created = now if created_at is None else parse_unix_seconds(created_at, "created_at_invalid")
    business_expires = None if expires_at is None else parse_unix_seconds(expires_at, "invalid_arguments")
    paths = resolve_paths(env)
    if not path_exists(paths.config_dir):
        raise NotifyMeError("configuration_missing")
    endpoint = load_endpoint(paths)
    _ensure_writable(paths, clock)
    store = StateStore(paths, clock, limits_from_env(env))
    admitted = store.admit(
        source, event_id, priority, title, body_text, business_created, business_expires, enqueue_only
    )
    if admitted["outcome"] == "deduplicated":
        return operation("deduplicated", admitted["meta"], previous_status=admitted["previous_status"])
    if enqueue_only or admitted["outcome"] != "sending":
        return operation(admitted["outcome"], admitted["meta"])
    return _deliver(store, endpoint, transport, admitted["claim"], env)


def command_push_status(env, clock, source, event_id):
    source = validate_source(source)
    event_id = validate_event_id(event_id)
    paths = resolve_paths(env)
    if not path_exists(paths.config_dir):
        raise NotifyMeError("configuration_missing")
    probe = inspect_database(paths.state_db)
    if probe["status"] == "missing":
        raise NotifyMeError("not_initialized")
    if probe["status"] != "ready":
        raise NotifyMeError(probe["error_code"] or "state_schema_unsupported")
    store = StateStore(paths, clock, limits_from_env(env))
    view = store.read_event(source, event_id)
    if view is None:
        return operation("not_found", None)
    return operation(view["status"], view["meta"])


def command_cancel(env, clock, source, event_id):
    source = validate_source(source)
    event_id = validate_event_id(event_id)
    paths = resolve_paths(env)
    if not path_exists(paths.config_dir):
        raise NotifyMeError("configuration_missing")
    _ensure_writable(paths, clock)
    store = StateStore(paths, clock, limits_from_env(env))
    result = store.cancel(source, event_id)
    extra = {"reason": result["reason"]} if result.get("reason") else None
    return operation(result["outcome"], result.get("meta"), previous_status=result.get("previous_status"), extra=extra)


def command_drain(env, clock, transport, source, event_id, max_items, budget_ms):
    source = validate_source(source)
    if event_id is not None:
        event_id = validate_event_id(event_id)
    paths = resolve_paths(env)
    if not path_exists(paths.config_dir):
        raise NotifyMeError("configuration_missing")
    endpoint = load_endpoint(paths)
    probe = inspect_database(paths.state_db)
    if probe["status"] == "missing":
        raise NotifyMeError("not_initialized")
    if probe["schema_version"] == SCHEMA_V8:
        raise NotifyMeError("schema_upgrade_required")
    if probe["schema_version"] != SCHEMA_V9 or probe["status"] != "ready":
        raise NotifyMeError(probe["error_code"] or "state_schema_unsupported")
    store = StateStore(paths, clock, limits_from_env(env))
    if event_id is not None:
        return _drain_one(store, endpoint, transport, env, source, event_id, budget_ms)
    started = clock.monotonic()
    results = []
    while len(results) < max_items:
        if (clock.monotonic() - started) * 1000 >= budget_ms:
            break
        claimed = store.claim_due(source, None)
        if claimed["outcome"] == "empty":
            break
        if claimed["outcome"] == "claimed":
            results.append(_deliver(store, endpoint, transport, claimed["claim"], env))
            continue
        if claimed["outcome"] == "terminal":
            results.append(operation(claimed["status"], claimed.get("meta")))
            continue
        break
    return {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "status": "completed",
        "processed": len(results),
        "results": results,
    }


def _drain_one(store, endpoint, transport, env, source, event_id, budget_ms):
    if budget_ms <= 0:
        view = store.read_event(source, event_id)
        if view is None:
            return operation("not_found", None)
        return operation(view["status"], view["meta"])
    claimed = store.claim_due(source, event_id)
    if claimed["outcome"] == "not_found":
        return operation("not_found", None)
    if claimed["outcome"] == "terminal":
        return operation(claimed["status"], claimed.get("meta"))
    if claimed["outcome"] == "waiting":
        view = store.read_event(source, event_id)
        return operation(view["status"], view["meta"])
    if claimed["outcome"] == "empty":
        view = store.read_event(source, event_id)
        if view is None:
            return operation("not_found", None)
        return operation(view["status"], view["meta"])
    return _deliver(store, endpoint, transport, claimed["claim"], env)


def _deliver(store, endpoint, transport, claim, env):
    now = int(store.clock.now())
    if now >= int(claim["expires_at"]):
        finalized = store.finalize(claim, False, 0, None, "expired", False)
        return _finish(store, claim, finalized)
    payload = copy.deepcopy(claim["payload"])
    payload["device_key"] = endpoint.key
    try:
        result = transport.send_with_retry(endpoint, payload, sleep=_sleeper(env), max_attempts=2)
    except NotifyMeError:
        raise
    except Exception:
        result = TransportResult(False, True, "network_error", None, 1)
    if (
        env.get("NOTIFY_ME_TEST_MODE") == "1"
        and env.get("NOTIFY_ME_TEST_FAULT") == "after-accept"
        and result.accepted
    ):
        raise NotifyMeError("delivery_interrupted")
    finalized = store.finalize(
        claim,
        bool(result.accepted),
        result.attempts,
        result.http_status,
        None if result.accepted else result.category,
        bool(result.retryable) and not result.accepted,
    )
    return _finish(store, claim, finalized)


def _finish(store, claim, finalized):
    if not finalized.get("applied"):
        view = store.reread_notification(claim["notification_id"])
        meta = dict(view["meta"]) if view else {}
        status_name = view["status"] if view else "sending"
        # A lost compare-and-swap must not invent acceptance from the transport
        # result, and must not downgrade an accept another commit already won.
        meta["outcome_uncertain"] = True
        if status_name == "accepted":
            meta["service_confirmed"] = True
            meta["uncertainty_applies_to"] = "prior_attempt"
        else:
            meta["error_code"] = "delivery_uncertain"
            meta["service_confirmed"] = False
            meta["uncertainty_applies_to"] = "current_delivery"
        return operation(status_name, meta, extra={"finalize_applied": False, "delivery_uncertain": True})
    extra = None
    if finalized.get("cancel_late"):
        extra = {"cancel_late": True, "reason": "accepted"}
    return operation(finalized["status"], finalized.get("meta"), extra=extra)


def _ensure_writable(paths, clock):
    probe = inspect_database(paths.state_db)
    if probe["status"] == "missing":
        create_fresh(paths.state_db, clock)
        return
    if probe["schema_version"] == SCHEMA_V8:
        raise NotifyMeError("schema_upgrade_required")
    if probe["schema_version"] != SCHEMA_V9 or probe["status"] != "ready":
        raise NotifyMeError(probe["error_code"] or "state_schema_unsupported")


def _source_key(paths, source):
    source = validate_source(source)
    connection = connect_readonly(paths.state_db)
    try:
        row = connection.execute("SELECT value_json FROM settings WHERE key='scope_salt'").fetchone()
        if row is None:
            raise NotifyMeError("state_corrupt")
        salt = json.loads(row[0])
    finally:
        connection.close()
    from .configuration import application_identity

    return application_identity(salt, source, "placeholder")[0]


def _missing_database():
    return {
        "status": "missing",
        "schema_version": None,
        "writable": False,
        "private": None,
        "integrity": None,
        "error_code": None,
    }


def _sleeper(env):
    if env.get("NOTIFY_ME_TEST_MODE") == "1":
        return lambda _seconds: None
    return None
