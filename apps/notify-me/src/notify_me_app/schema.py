# -*- coding: utf-8 -*-
"""Historical schema 8 text and the writable schema 9 definition.

Importing this module hashes the embedded SQL in memory. It does not open
a database or create a directory.
"""

import hashlib


SCHEMA_V8 = 8
SCHEMA_V9 = 9
LEGACY_SCHEMA_V8_CHECKSUM = "ccd1690e549465539e88af0737d0efac7b698fac4b5e3f931e33b738e9f72ac7"

LEGACY_SCHEMA_V8_SQL = "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at REAL NOT NULL)\nCREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)\nCREATE TABLE IF NOT EXISTS notifications (notification_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, condition_key TEXT NOT NULL, item_key TEXT NOT NULL, event_state_key TEXT NOT NULL, effect_fingerprint TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('sending', 'accepted', 'failed', 'deduplicated')), created_at REAL NOT NULL, updated_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, http_status INTEGER, last_error TEXT)\nCREATE UNIQUE INDEX IF NOT EXISTS notifications_item_identity ON notifications (scope_key, condition_key, item_key, event_state_key, effect_fingerprint)\nCREATE TABLE IF NOT EXISTS priority_effects (priority TEXT PRIMARY KEY CHECK (priority IN ('P0', 'P1', 'P2', 'P3')), effect_json TEXT, updated_at REAL NOT NULL)\nCREATE TABLE IF NOT EXISTS condition_configs (condition_key TEXT PRIMARY KEY CHECK (condition_key IN ('blocking', 'severe-risk')), priority TEXT NOT NULL REFERENCES priority_effects(priority), enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)), effect_override_json TEXT, updated_at REAL NOT NULL)\nCREATE TABLE IF NOT EXISTS subscriptions (subscription_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision >= 1), summary TEXT NOT NULL, mode TEXT NOT NULL CHECK (mode IN ('one-time', 'repeating')), priority TEXT NOT NULL REFERENCES priority_effects(priority), effect_override_json TEXT, status TEXT NOT NULL CHECK (status IN ('pending', 'triggered-pending-delivery', 'consumed', 'delivery-failed', 'cancelled')), replaces_subscription_id TEXT REFERENCES subscriptions(subscription_id), created_at REAL NOT NULL, updated_at REAL NOT NULL)\nCREATE INDEX IF NOT EXISTS subscriptions_scope_status ON subscriptions (scope_key, status, created_at)\nCREATE TABLE IF NOT EXISTS subscription_events (event_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id), scope_key TEXT NOT NULL, fulfillment_key TEXT NOT NULL, notification_id TEXT NOT NULL UNIQUE REFERENCES notifications(notification_id), status TEXT NOT NULL CHECK (status IN ('sending', 'accepted', 'failed')), created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(subscription_id, fulfillment_key))\nCREATE INDEX IF NOT EXISTS subscription_events_scope_status ON subscription_events (scope_key, status, created_at)\nCREATE TABLE IF NOT EXISTS outbox (notification_id TEXT PRIMARY KEY REFERENCES notifications(notification_id) ON DELETE CASCADE, subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id), payload_json TEXT NOT NULL, next_attempt_at REAL NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)\nCREATE INDEX IF NOT EXISTS outbox_due ON outbox (next_attempt_at, expires_at, lease_until)\nCREATE TABLE IF NOT EXISTS subscription_event_payloads (event_id TEXT PRIMARY KEY REFERENCES subscription_events(event_id) ON DELETE CASCADE, payload_json TEXT NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)\nCREATE INDEX IF NOT EXISTS subscription_event_payloads_expiry ON subscription_event_payloads (expires_at)\nCREATE TABLE IF NOT EXISTS application_events (notification_id TEXT PRIMARY KEY, source_key TEXT NOT NULL, event_key TEXT NOT NULL, priority TEXT NOT NULL REFERENCES priority_effects(priority), effect_fingerprint TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('sending', 'accepted', 'failed')), created_at REAL NOT NULL, updated_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, http_status INTEGER, last_error TEXT, UNIQUE(source_key, event_key))\nCREATE INDEX IF NOT EXISTS application_events_status ON application_events (status, updated_at)\nCREATE TABLE IF NOT EXISTS application_outbox (notification_id TEXT PRIMARY KEY REFERENCES application_events(notification_id) ON DELETE CASCADE, payload_json TEXT NOT NULL, next_attempt_at REAL NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)\nCREATE INDEX IF NOT EXISTS application_outbox_due ON application_outbox (next_attempt_at, expires_at, lease_until)"

if hashlib.sha256(LEGACY_SCHEMA_V8_SQL.encode("utf-8")).hexdigest() != LEGACY_SCHEMA_V8_CHECKSUM:
    raise RuntimeError("embedded schema 8 SQL does not match the historical checksum")


SETTINGS_SQL = (
    "CREATE TABLE IF NOT EXISTS settings ("
    "key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)"
)
MIGRATIONS_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at REAL NOT NULL)"
)
PRIORITY_EFFECTS_SQL = (
    "CREATE TABLE IF NOT EXISTS priority_effects ("
    "priority TEXT PRIMARY KEY CHECK (priority IN ('P0', 'P1', 'P2', 'P3')), "
    "effect_json TEXT, updated_at REAL NOT NULL)"
)

APPLICATION_EVENTS_V9_SQL = """CREATE TABLE application_events (
    notification_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    event_key TEXT NOT NULL,
    priority TEXT REFERENCES priority_effects(priority),
    effect_fingerprint TEXT,
    payload_fingerprint TEXT,
    status TEXT NOT NULL CHECK (status IN ('queued', 'sending', 'accepted', 'failed', 'expired', 'cancelled')),
    business_created_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL,
    accepted_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    last_error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    outcome_uncertain INTEGER NOT NULL DEFAULT 0 CHECK (outcome_uncertain IN (0, 1)),
    UNIQUE(source_key, event_key)
)"""

APPLICATION_EVENTS_STATUS_SQL = (
    "CREATE INDEX IF NOT EXISTS application_events_status "
    "ON application_events (status, updated_at)"
)
APPLICATION_OUTBOX_V9_SQL = """CREATE TABLE application_outbox (
    notification_id TEXT PRIMARY KEY REFERENCES application_events(notification_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    next_attempt_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)"""
APPLICATION_OUTBOX_DUE_SQL = (
    "CREATE INDEX IF NOT EXISTS application_outbox_due "
    "ON application_outbox (next_attempt_at, expires_at, lease_until)"
)

# Agent tables retained from schema 8 are not part of this hash.
SCHEMA_V9_SQL = "\n".join((
    MIGRATIONS_SQL,
    SETTINGS_SQL,
    PRIORITY_EFFECTS_SQL,
    APPLICATION_EVENTS_V9_SQL,
    APPLICATION_EVENTS_STATUS_SQL,
    APPLICATION_OUTBOX_V9_SQL,
    APPLICATION_OUTBOX_DUE_SQL,
))
SCHEMA_V9_CHECKSUM = hashlib.sha256(SCHEMA_V9_SQL.encode("utf-8")).hexdigest()

EVENT_STATES = ("queued", "sending", "accepted", "failed", "expired", "cancelled")
TERMINAL_STATES = ("accepted", "failed", "expired", "cancelled")
