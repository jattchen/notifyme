import json
import os
import socket
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(ROOT))

from notify_me.bark import BarkEndpoint, BarkTransport  # noqa: E402
from notify_me.binding import Binding  # noqa: E402
from notify_me.errors import NotifyMeError  # noqa: E402


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body

    def close(self):
        return None

    def getcode(self):
        return self.status


class SequenceOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.requests = []

    def open(self, request, timeout=None):
        self.calls += 1
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class BarkTests(unittest.TestCase):
    def test_parse_rejects_placeholder_and_hides_key_in_public_view(self):
        with self.assertRaises(NotifyMeError) as caught:
            BarkEndpoint.parse("https://api.day.app/changeme")
        self.assertEqual(caught.exception.code, "invalid_bark_key")
        endpoint = BarkEndpoint.parse("https://api.day.app/Abcdefgh1234")
        view = endpoint.public_view()
        self.assertEqual(view["host"], "api.day.app")
        self.assertNotIn("key", view)
        self.assertNotIn("Abcdefgh1234", json.dumps(view))

    def test_retry_posts_at_most_twice(self):
        endpoint = BarkEndpoint.parse("https://api.day.app/Abcdefgh1234")
        opener = SequenceOpener(
            [
                urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
                FakeResponse(200, b'{"code":200}'),
            ]
        )
        transport = BarkTransport(opener=opener)
        result = transport.send_with_retry(
            endpoint,
            {
                "device_key": endpoint.key,
                "title": "任务阻塞",
                "body": "请查看",
            },
            sleep=lambda _delay: None,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(opener.calls, 2)

    def test_timeout_does_not_retry(self):
        endpoint = BarkEndpoint.parse("https://api.day.app/Abcdefgh1234")
        opener = SequenceOpener(
            [
                socket.timeout("timed out"),
                FakeResponse(200, b'{"code":200}'),
            ]
        )
        transport = BarkTransport(opener=opener)
        result = transport.send_with_retry(
            endpoint,
            {
                "device_key": endpoint.key,
                "title": "任务阻塞",
                "body": "请查看",
            },
            sleep=lambda _delay: None,
        )
        self.assertFalse(result.accepted)
        self.assertFalse(result.retryable)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(opener.calls, 1)

    def test_permanent_failure_does_not_retry(self):
        endpoint = BarkEndpoint.parse("https://api.day.app/Abcdefgh1234")
        opener = SequenceOpener([FakeResponse(400, b'{"code":400}')])
        transport = BarkTransport(opener=opener)
        result = transport.send_with_retry(
            endpoint,
            {
                "device_key": endpoint.key,
                "title": "任务阻塞",
                "body": "请查看",
            },
            sleep=lambda _delay: None,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(opener.calls, 1)


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_save_is_private_and_sqlite_is_not_created(self):
        binding = Binding(self.home)
        endpoint = BarkEndpoint.parse("https://api.day.app/Abcdefgh1234")
        view = binding.save(endpoint)
        self.assertEqual(view["host"], "api.day.app")
        self.assertNotIn("key", view)
        mode = stat.S_IMODE(binding.path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertFalse((self.home / "state.sqlite").exists())
        loaded = binding.load()
        self.assertEqual(loaded.key, endpoint.key)
        public = json.dumps(binding.public_view())
        self.assertNotIn("Abcdefgh1234", public)
