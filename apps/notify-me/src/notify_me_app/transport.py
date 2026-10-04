# -*- coding: utf-8 -*-
"""Bark HTTP boundary. The device key is added only on the outgoing payload."""

import copy
import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .errors import NotifyMeError


_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PLACEHOLDER_KEYS = {
    "key",
    "devicekey",
    "device-key",
    "yourkey",
    "your-key",
    "your-device-key",
    "changeme",
    "change-me",
    "test-key",
    "example-key",
    "xxx",
}


@dataclass(frozen=True)
class BarkEndpoint(object):
    server: str
    key: str
    host: str

    @property
    def push_url(self):
        return self.server + "/push"

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, str) or not raw.strip():
            raise NotifyMeError("invalid_bark_url")
        value = raw.strip()
        if any(char.isspace() for char in value):
            raise NotifyMeError("invalid_bark_url")
        try:
            parsed = urllib.parse.urlsplit(value)
            port = parsed.port
        except (ValueError, UnicodeError):
            raise NotifyMeError("invalid_bark_url")
        if parsed.scheme.lower() not in ("http", "https"):
            raise NotifyMeError("invalid_bark_url")
        if not parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise NotifyMeError("invalid_bark_url")
        if parsed.fragment:
            raise NotifyMeError("invalid_bark_url")
        if port is not None and not 1 <= port <= 65535:
            raise NotifyMeError("invalid_bark_url")
        try:
            host = parsed.hostname
        except ValueError:
            host = None
        if not host:
            raise NotifyMeError("invalid_bark_url")
        host = host.lower().rstrip(".")
        if parsed.scheme.lower() == "http" and host not in ("localhost", "127.0.0.1", "::1"):
            raise NotifyMeError("insecure_bark_url")
        raw_path = parsed.path or ""
        if "//" in raw_path:
            raise NotifyMeError("invalid_bark_url")
        segments = [segment for segment in raw_path.split("/") if segment]
        if not segments:
            raise NotifyMeError("invalid_bark_url")
        key = segments[-1]
        for index, segment in enumerate(segments):
            decoded = urllib.parse.unquote(segment)
            if index == len(segments) - 1 and decoded != segment:
                raise NotifyMeError("invalid_bark_url")
            if decoded in (".", "..") or any(char in decoded for char in ("/", "\\")):
                raise NotifyMeError("invalid_bark_url")
        if len(key) < 8 or not _KEY_RE.fullmatch(key):
            raise NotifyMeError("invalid_bark_key")
        if key.lower() in _PLACEHOLDER_KEYS or set(key.lower()) == {"x"}:
            raise NotifyMeError("invalid_bark_key")
        if ":" in host and not host.startswith("["):
            normalized_host = "[{}]".format(host)
        else:
            normalized_host = host
        if port is not None and not (
            (parsed.scheme.lower() == "https" and port == 443)
            or (parsed.scheme.lower() == "http" and port == 80)
        ):
            normalized_netloc = "{}:{}".format(normalized_host, port)
        else:
            normalized_netloc = normalized_host
        server = "{}://{}".format(parsed.scheme.lower(), normalized_netloc)
        if len(segments) > 1:
            server = "{}/{}".format(server, "/".join(segments[:-1]))
        return cls(server=server, key=key, host=host)

    @classmethod
    def from_stored(cls, data):
        if not isinstance(data, dict):
            raise NotifyMeError("invalid_binding")
        server = data.get("server")
        key = data.get("key")
        host = data.get("host")
        if not isinstance(server, str) or not isinstance(key, str) or not isinstance(host, str):
            raise NotifyMeError("invalid_binding")
        if not server or not key or not host:
            raise NotifyMeError("invalid_binding")
        try:
            parsed = cls.parse("{}/{}".format(server.rstrip("/"), key))
        except NotifyMeError:
            raise NotifyMeError("invalid_binding")
        if parsed.key != key or parsed.host != host or parsed.server != server.rstrip("/"):
            raise NotifyMeError("invalid_binding")
        return parsed


@dataclass(frozen=True)
class TransportResult(object):
    accepted: bool
    retryable: bool
    category: str
    http_status: object = None
    attempts: int = 1


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _classify_http_status(status):
    if 300 <= status <= 399:
        return False, "redirect_rejected"
    if status in (408, 425, 429) or 500 <= status <= 599:
        return True, "retryable_http"
    return False, "permanent_http"


def _retry(send_once, endpoint, payload, sleep, max_attempts):
    sleeper = sleep or time.sleep
    last = None
    for attempt in range(1, max_attempts + 1):
        last = send_once(endpoint, payload)
        if last.accepted or not last.retryable or attempt == max_attempts:
            return TransportResult(
                last.accepted, last.retryable, last.category, last.http_status, attempt
            )
        sleeper(0.2)
    return last


class BarkTransport(object):
    def __init__(self, timeout=5.0, opener=None):
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(_NoRedirectHandler())

    def send(self, endpoint, payload):
        if not isinstance(endpoint, BarkEndpoint):
            raise NotifyMeError("invalid_bark_url")
        if not isinstance(payload, dict) or payload.get("device_key") != endpoint.key:
            raise NotifyMeError("invalid_payload")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            endpoint.push_url,
            data=encoded,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            response = self.opener.open(request, timeout=self.timeout)
            status = getattr(response, "status", None) or response.getcode()
            try:
                body = response.read(65537)
            finally:
                response.close()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                exc.read(65537)
            finally:
                exc.close()
            retryable, category = _classify_http_status(status)
            return TransportResult(False, retryable, category, status)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, http.client.HTTPException):
            return TransportResult(False, True, "network_error")
        if status < 200 or status >= 300:
            retryable, category = _classify_http_status(status)
            return TransportResult(False, retryable, category, status)
        if not isinstance(body, (bytes, bytearray)) or len(body) > 65536 or not body:
            return TransportResult(False, True, "invalid_response", status)
        try:
            response_json = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError, AttributeError):
            return TransportResult(False, True, "invalid_response", status)
        if not isinstance(response_json, dict):
            return TransportResult(False, True, "invalid_response", status)
        if response_json.get("code") == 200:
            return TransportResult(True, False, "accepted", status)
        code = response_json.get("code")
        if code in (408, 425, 429) or (isinstance(code, int) and 500 <= code <= 599):
            return TransportResult(False, True, "bark_retryable", status)
        return TransportResult(False, False, "bark_rejected", status)

    def send_with_retry(self, endpoint, payload, sleep=None, max_attempts=2):
        return _retry(self.send, endpoint, payload, sleep, max_attempts)


class FakeBarkTransport(object):
    """In-process stand-in. It never opens a socket."""

    def __init__(self, result=None, results=None):
        self.result = result or TransportResult(True, False, "accepted", 200)
        self.results = list(results or [])
        self.payloads = []
        self.calls = 0

    def send(self, endpoint, payload):
        self.calls += 1
        self.payloads.append(copy.deepcopy(payload))
        if self.results:
            return self.results.pop(0)
        return self.result

    def send_with_retry(self, endpoint, payload, sleep=None, max_attempts=2):
        return _retry(self.send, endpoint, payload, sleep, max_attempts)
