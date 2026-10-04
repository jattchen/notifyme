# -*- coding: utf-8 -*-
"""Priority effects, public text checks, and the historical application identity."""

import hashlib
import hmac
import json
import re

from .errors import NotifyMeError


PRIORITIES = ("P0", "P1", "P2", "P3")
LEVELS = ("critical", "timeSensitive", "active", "passive")
_SOUND = re.compile(r"[A-Za-z0-9_-]{1,64}$")
_SOURCE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET = re.compile(
    r"(?:https?://|ftp://|\b(?:api[-_ ]?key|access[-_ ]?token|auth(?:entication)?|bearer|credential|password|passwd|secret|private[-_ ]?key)\b|\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9])")

P0_EFFECT = {
    "level": "critical",
    "sound": "alarm",
    "volume": 8,
    "call": False,
    "delivery_ttl_seconds": 900,
}
P1_EFFECT = {
    "level": "timeSensitive",
    "sound": "telegraph",
    "call": False,
    "delivery_ttl_seconds": 7200,
}
P2_EFFECT = {
    "level": "active",
    "sound": "glass",
    "call": False,
    "delivery_ttl_seconds": 14400,
}
DEFAULT_PRIORITY_EFFECTS = {
    "P0": P0_EFFECT,
    "P1": P1_EFFECT,
    "P2": P2_EFFECT,
    "P3": None,
}
BACKOFF_SECONDS = {"critical": 30, "timeSensitive": 120, "active": 300, "passive": 300}
BACKOFF_CAP_SECONDS = 3600
RETENTION_SECONDS = 30 * 24 * 3600
LEASE_SECONDS = 60
TRANSPORT_TIMEOUT_SECONDS = 5.0
TRANSPORT_ATTEMPTS = 2
MAX_ACTIVE_PER_SOURCE = 200
MAX_ACTIVE_TOTAL = 1000
MAX_ROWS_PER_SOURCE = 2000
MAX_ROWS_TOTAL = 10000
ICON_URL = "https://hcn58q8zsfep.feishuapp.com/app/app_17acsapfz2z/codex-bark-icon.png"
CREATED_AT_FUTURE_SKEW = 120


def validate_priority(priority):
    if priority not in PRIORITIES:
        raise NotifyMeError("invalid_priority")
    return priority


def validate_effect(effect, allow_none=False):
    if effect is None and allow_none:
        return None
    if not isinstance(effect, dict):
        raise NotifyMeError("invalid_effect")
    allowed = {"level", "sound", "volume", "call", "delivery_ttl_seconds"}
    if set(effect) - allowed:
        raise NotifyMeError("invalid_effect")
    level = effect.get("level")
    sound = effect.get("sound")
    call = effect.get("call", False)
    ttl = effect.get("delivery_ttl_seconds")
    volume = effect.get("volume")
    if level not in LEVELS:
        raise NotifyMeError("invalid_effect")
    if not isinstance(sound, str) or not _SOUND.fullmatch(sound):
        raise NotifyMeError("invalid_effect")
    if not isinstance(call, bool):
        raise NotifyMeError("invalid_effect")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 604800:
        raise NotifyMeError("invalid_effect")
    if level == "critical":
        if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 10:
            raise NotifyMeError("invalid_effect")
    elif volume is not None:
        raise NotifyMeError("invalid_effect")
    normalized = {
        "level": level,
        "sound": sound,
        "call": call,
        "delivery_ttl_seconds": ttl,
    }
    if level == "critical":
        normalized["volume"] = volume
    return normalized


def bark_fields(effect):
    validated = validate_effect(effect)
    payload = {"level": validated["level"], "sound": validated["sound"]}
    if validated.get("volume") is not None:
        payload["volume"] = validated["volume"]
    if validated.get("call"):
        payload["call"] = True
    return payload


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def content_fingerprint(title, body, priority):
    return fingerprint({"body": body, "priority": priority, "title": title})


def safe_text(value, limit, code):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise NotifyMeError(code)
    for char in value:
        if ord(char) < 32 or ord(char) in (0x7F, 0x2028, 0x2029):
            raise NotifyMeError(code)
    if _SECRET.search(value) or _LONG_TOKEN.search(value):
        raise NotifyMeError(code)
    return value


def validate_source(source):
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise NotifyMeError("invalid_source")
    return source


def validate_event_id(event_id):
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise NotifyMeError("invalid_event_id")
    return event_id


def application_identity(salt_hex, source, event_id):
    """Historical HMAC. source_scope_salt and the application prefix stay put."""

    validate_source(source)
    validate_event_id(event_id)
    if not isinstance(salt_hex, str):
        raise NotifyMeError("state_corrupt")
    try:
        secret = bytes.fromhex(salt_hex)
    except (TypeError, ValueError):
        raise NotifyMeError("state_corrupt")
    if not secret:
        raise NotifyMeError("state_corrupt")
    source_key = hmac.new(
        secret, ("application\0" + source).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    event_key = hmac.new(
        bytes.fromhex(source_key), event_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    notification_id = "nm_" + hmac.new(
        secret,
        ("application\0" + source + "\0" + event_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:40]
    return source_key, event_key, notification_id


def backoff_seconds(level, attempts_before):
    base = BACKOFF_SECONDS.get(level, 300)
    try:
        shift = int(attempts_before)
    except (TypeError, ValueError):
        shift = 0
    if shift < 0:
        shift = 0
    if shift > 16:
        shift = 16
    return min(base * (2 ** shift), BACKOFF_CAP_SECONDS)


def parse_unix_seconds(value, code):
    if isinstance(value, bool) or value is None:
        raise NotifyMeError(code)
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise NotifyMeError(code)
        try:
            number = float(text)
        except ValueError:
            raise NotifyMeError(code)
    else:
        raise NotifyMeError(code)
    if number != number or number in (float("inf"), float("-inf")):
        raise NotifyMeError(code)
    if number < 0 or number > 100000000000:
        raise NotifyMeError(code)
    return int(number)
