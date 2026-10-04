# -*- coding: utf-8 -*-
"""Isolated CLI fixtures. They never touch the real host install."""

import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest

from notify_me_app.cli import main
from notify_me_app.storage import ManualClock
from notify_me_app.transport import FakeBarkTransport


DEVICE_KEY = "devicekey1"
BARK_URL = "http://127.0.0.1:9/" + DEVICE_KEY


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="notify-me-cli-")
        os.chmod(self.root, 0o700)
        self.home = os.path.join(self.root, "home")
        os.mkdir(self.home, 0o700)
        self.config = os.path.join(self.root, "config")
        self.clock = ManualClock(1700000000)
        self.transport = FakeBarkTransport()

    def tearDown(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def make_config(self):
        os.mkdir(self.config, 0o700)
        return self.config

    def env(self, **extra):
        values = {
            "HOME": self.home,
            "PATH": "/usr/bin:/bin",
            "NOTIFY_ME_FORBID_HOST_PATHS": "1",
            "NOTIFY_ME_TEST_MODE": "1",
            "NOTIFY_ME_CONFIG_DIR": self.config,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        values.update(extra)
        return values

    def write_env(self, url=None):
        self.make_config_if_missing()
        path = os.path.join(self.config, ".env")
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("BARK_URL={}\n".format(url or BARK_URL))
        os.chmod(path, 0o600)
        return path

    def make_config_if_missing(self):
        if not os.path.isdir(self.config):
            os.mkdir(self.config, 0o700)

    def cli(self, argv, env=None, transport=None, clock=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = stdout
        sys.stderr = stderr
        try:
            code = main(
                list(argv),
                env=self.env() if env is None else env,
                transport=self.transport if transport is None else transport,
                clock=self.clock if clock is None else clock,
            )
        finally:
            sys.stdout = old_out
            sys.stderr = old_err
        text = stdout.getvalue()
        try:
            body = json.loads(text)
        except ValueError:
            self.fail("stdout is not JSON: {!r} stderr={!r}".format(text, stderr.getvalue()))
        self.assert_public(text)
        self.assertLessEqual(len(text.encode("utf-8")), 65536)
        self.assertTrue(text.endswith("\n"))
        return code, body, text, stderr.getvalue()

    def assert_public(self, text):
        self.assertNotIn(DEVICE_KEY, text)
        self.assertNotIn("BARK_URL", text)
        self.assertNotIn("device_key", text)
        self.assertNotIn("fake_credential", text)

    def assert_error(self, code, body):
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["error"], {"code": code})
        self.assertEqual(body["protocol_version"], 1)

    def names(self, path):
        if not os.path.exists(path):
            return None
        return sorted(os.listdir(path))

    def database_bytes(self):
        path = os.path.join(self.config, "state.sqlite3")
        with open(path, "rb") as handle:
            return handle.read()

    def event_count(self):
        path = os.path.join(self.config, "state.sqlite3")
        if not os.path.exists(path):
            return 0
        connection = sqlite3.connect(path)
        try:
            return connection.execute("SELECT COUNT(*) FROM application_events").fetchone()[0]
        finally:
            connection.close()

    def row_status(self, source, event_id):
        code, body, _text, _err = self.cli(
            ["push-status", "--source", source, "--event-id", event_id]
        )
        return code, body


def write_private(path, data):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o600)


def mode_of(path):
    return stat.S_IMODE(os.lstat(path).st_mode)
