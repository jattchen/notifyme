# -*- coding: utf-8 -*-
"""Group 6: schema 8 upgrade, four install paths, and interrupt recovery."""

import json
import os
import sqlite3
from pathlib import Path

from notify_me_app.configuration import application_identity
from notify_me_app.storage import ManualClock

from tests.support import IsolatedCase, mode_of, write_private
from tests.test_migration import SALT, _load_vectors, _write_schema8
from tests.test_outbox import _push


class InstallationTests(IsolatedCase):
    def setUp(self):
        IsolatedCase.setUp(self)
        self.bin_dir = os.path.join(self.root, "bin")
        os.mkdir(self.bin_dir, 0o700)
        self.launcher = os.path.join(self.bin_dir, "notify-me")
        self.clock = ManualClock(500000)

    def tearDown(self):
        os.environ.pop("NOTIFY_ME_TEST_MIGRATION_FAIL_AFTER", None)
        IsolatedCase.tearDown(self)

    def test_dry_run_and_fresh_install_do_not_create_config(self):
        code, body, _text, _err = self.cli(
            ["install", "--launcher", self.launcher, "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(body["status"], "dry_run")
        self.assertNotEqual(body["source_commit"], "dev")
        self.assertEqual(len(body["build_digest"]), 64)
        self.assertFalse(os.path.exists(self.launcher))
        self.assertFalse(os.path.exists(self.config))

        code, body, _text, _err = self.cli(["install", "--launcher", self.launcher])
        self.assertEqual(body["status"], "installed")
        self.assertFalse(body["migrated"])
        self.assertFalse(body["created_database"])
        self.assertIsNone(body["config_dir"])
        self.assertEqual(mode_of(self.launcher), 0o700)
        self.assertFalse(os.path.exists(self.config))
        code, status, _text, _err = self.cli(["status"])
        self.assertEqual(status["status"], "configuration_missing")
        self.assertFalse(os.path.exists(self.config))
        code, missing, _text, _err = self.cli(
            ["recover", "--launcher", self.launcher]
        )
        self.assert_error("recovery_unavailable", missing)

    def test_upgrade_preserves_salt_binding_identity_and_custom_effect(self):
        env_bytes = self._prepare_legacy()
        code, body, _text, _err = self.cli(
            ["install", "--launcher", self.launcher, "--config-dir", self.config]
        )
        self.assertEqual(code, 0)
        self.assertTrue(body["migrated"])
        self.assertFalse(body["created_database"])
        self.assertEqual(self._read(os.path.join(self.config, ".env")), env_bytes)
        self.assertEqual(self._salt(), SALT)
        self.assertEqual(self._schema_version(), 9)
        self.assertFalse(os.path.exists(os.path.join(self.config, "install-recovery.json")))
        self.assertFalse(os.path.exists(os.path.join(self.config, "state.sqlite3.pre-schema9")))
        self.assertTrue(self._read(self.launcher).startswith(b"#!/usr/bin/env python3\n"))
        self.assertEqual(mode_of(self.launcher), 0o700)
        self.assertEqual(self._agent_status(), "accepted")
        vector = _load_vectors()[0]
        _code, view = self.row_status(vector["source"], vector["event_id"])
        self.assertEqual(view["status"], "accepted")
        self.assertEqual(view["notification_id"], vector["notification_id"])
        code, replay, _text, _err = self.cli(
            _push(vector["event_id"], priority="P1", title="Again", body="Still")
        )
        self.assertEqual(replay["status"], "deduplicated")
        self.assertEqual(replay["previous_status"], "accepted")
        self.assertEqual(replay["notification_id"], vector["notification_id"])
        self.assertEqual(self.transport.calls, 0)
        code, queued, _text, _err = self.cli(
            _push("custom-p2", priority="P2", title="Bell", body="Tone", extra=["--enqueue-only"])
        )
        self.assertEqual(queued["expires_at"], 500000 + 15000)
        code, sent, _text, _err = self.cli(
            ["push-drain", "--source", "aiusage", "--event-id", "custom-p2"]
        )
        self.assertEqual(sent["status"], "accepted")
        self.assertEqual(self.transport.payloads[0]["sound"], "bell")
        self.assertEqual(self.transport.payloads[0]["level"], "active")
        code, again, _text, _err = self.cli(
            ["install", "--launcher", self.launcher, "--config-dir", self.config]
        )
        self.assertEqual(again["status"], "installed")
        self.assertFalse(again["migrated"])
        self.assertEqual(self._read(os.path.join(self.config, ".env")), env_bytes)
        self.assertEqual(self._salt(), SALT)
        self.assertFalse(os.path.exists(os.path.join(self.config, "install-recovery.json")))

    def test_each_interrupt_can_return_to_the_old_program(self):
        self._prepare_legacy()
        self._expect_interrupt("after-backup")
        self.assertEqual(self._schema_version(), 8)
        self.assertTrue(os.path.exists(os.path.join(self.config, "state.sqlite3.pre-schema9")))
        self.assertEqual(self._read(self.launcher), b"OLD-LAUNCHER\n")
        recovered = self._recover()
        self.assertFalse(recovered["database_restored"])
        self.assertFalse(recovered["code_restored"])
        self.assertEqual(self._schema_version(), 8)
        self.assertFalse(os.path.exists(os.path.join(self.config, "state.sqlite3.pre-schema9")))
        self.assertEqual(self._read(self.launcher), b"OLD-LAUNCHER\n")

        self._prepare_legacy()
        self._expect_interrupt("after-migrate")
        self.assertEqual(self._schema_version(), 9)
        recovered = self._recover()
        self.assertTrue(recovered["database_restored"])
        self.assertFalse(recovered["code_restored"])
        self.assertEqual(recovered["schema_version"], 8)
        self.assertEqual(self._schema_version(), 8)
        vector = _load_vectors()[0]
        _code, view = self.row_status(vector["source"], vector["event_id"])
        self.assertEqual(view["status"], "accepted")
        self.assertEqual(view["notification_id"], vector["notification_id"])

        self._prepare_legacy()
        self._expect_interrupt("after-replace")
        self.assertTrue(self._read(self.launcher).startswith(b"#!/usr/bin/env python3\n"))
        recovered = self._recover()
        self.assertTrue(recovered["database_restored"])
        self.assertTrue(recovered["code_restored"])
        self.assertFalse(recovered["delivery_preserved"])
        self.assertEqual(self._schema_version(), 8)
        self.assertEqual(self._read(self.launcher), b"OLD-LAUNCHER\n")
        self.assertFalse(os.path.exists(self.launcher + ".previous"))

    def test_replace_journal_survives_either_side_of_the_commit(self):
        self._prepare_legacy()
        self._expect_interrupt("before-replace")
        marker = json.loads(self._read(os.path.join(self.config, "install-recovery.json")).decode("utf-8"))
        self.assertEqual(marker["phase"], "replacing")
        self.assertEqual(marker["previous_sha256"], self._sha(b"OLD-LAUNCHER\n"))
        self.assertEqual(self._read(self.launcher), b"OLD-LAUNCHER\n")
        self.assertEqual(self._schema_version(), 9)
        recovered = self._recover()
        self.assertFalse(recovered["code_restored"])
        self.assertTrue(recovered["database_restored"])
        self.assertEqual(recovered["database_rollback"], "restored")
        self.assertEqual(self._schema_version(), 8)
        self.assertEqual(self._read(self.launcher), b"OLD-LAUNCHER\n")
        self.assertFalse(os.path.exists(self.launcher + ".previous"))

        self._prepare_legacy()
        self._expect_interrupt("after-commit")
        marker = json.loads(self._read(os.path.join(self.config, "install-recovery.json")).decode("utf-8"))
        self.assertEqual(marker["phase"], "replacing")
        self.assertFalse(marker["launcher_replaced"])
        self.assertTrue(self._read(self.launcher).startswith(b"#!/usr/bin/env python3\n"))
        self.assertEqual(self._sha(self._read(self.launcher)), marker["new_sha256"])
        self.assertEqual(self._sha(self._read(self.launcher + ".previous")), marker["previous_sha256"])
        recovered = self._recover()
        self.assertTrue(recovered["code_restored"])
        self.assertTrue(recovered["database_restored"])
        self.assertEqual(recovered["schema_version"], 8)
        self.assertEqual(self._read(self.launcher), b"OLD-LAUNCHER\n")
        self.assertFalse(os.path.exists(os.path.join(self.config, "install-recovery.json")))

        self._remove_tree(self.config)
        self._expect_interrupt_at(self.launcher, None, "before-replace")
        journal = os.path.join(self.bin_dir, "install-recovery.json")
        self.assertTrue(os.path.exists(journal))
        self.assertFalse(os.path.exists(self.config))
        self.assertEqual(self._read(self.launcher), b"OLD-LAUNCHER\n")
        code, recovered, _text, _err = self.cli(["recover", "--launcher", self.launcher])
        self.assertEqual(recovered["status"], "recovered")
        self.assertFalse(recovered["code_restored"])
        self.assertEqual(recovered["database_rollback"], "unchanged")
        self.assertEqual(self._read(self.launcher), b"OLD-LAUNCHER\n")
        self.assertFalse(os.path.exists(journal))
        self.assertFalse(os.path.exists(self.config))

        os.unlink(self.launcher)
        self._expect_interrupt_at(self.launcher, None, "after-commit")
        self.assertTrue(self._read(self.launcher).startswith(b"#!/usr/bin/env python3\n"))
        self.assertFalse(os.path.exists(self.launcher + ".previous"))
        self.assertFalse(os.path.exists(self.config))
        code, recovered, _text, _err = self.cli(["recover", "--launcher", self.launcher])
        self.assertTrue(recovered["code_restored"])
        self.assertFalse(os.path.exists(self.launcher))
        self.assertFalse(os.path.exists(self.config))
        self.assertFalse(os.path.exists(journal))

    def test_schema9_replace_and_accept_barrier_keep_the_database(self):
        self._prepare_legacy()
        code, body, _text, _err = self.cli(
            ["install", "--launcher", self.launcher, "--config-dir", self.config]
        )
        self.assertEqual(body["status"], "installed")
        self.assertEqual(self._schema_version(), 9)
        os.chmod(self.launcher, 0o700)
        os.unlink(self.launcher)
        write_private(self.launcher, b"CURRENT-LAUNCHER\n")
        os.chmod(self.launcher, 0o700)
        self._expect_interrupt("after-commit")
        self.assertTrue(self._read(self.launcher).startswith(b"#!/usr/bin/env python3\n"))
        self.assertFalse(os.path.exists(os.path.join(self.config, "state.sqlite3.pre-schema9")))
        recovered = self._recover()
        self.assertTrue(recovered["code_restored"])
        self.assertFalse(recovered["database_restored"])
        self.assertEqual(recovered["database_rollback"], "unchanged")
        self.assertEqual(recovered["schema_version"], 9)
        self.assertEqual(self._read(self.launcher), b"CURRENT-LAUNCHER\n")
        self.assertEqual(self._schema_version(), 9)

        self._prepare_legacy()
        self._expect_interrupt("after-replace")
        code, recovered, _text, _err = self.cli(
            ["recover", "--launcher", self.launcher, "--config-dir", self.config],
            env=self.env(NOTIFY_ME_TEST_RECOVER_BARRIER="admit-accepted"),
        )
        self.assertEqual(code, 0, recovered)
        self.assertEqual(recovered["status"], "recovered")
        self.assertTrue(recovered["code_restored"])
        self.assertFalse(recovered["database_restored"])
        self.assertTrue(recovered["delivery_preserved"])
        self.assertEqual(recovered["database_rollback"], "preserved")
        self.assertEqual(recovered["schema_version"], 9)
        self.assertEqual(self._schema_version(), 9)
        self.assertEqual(self._read(self.launcher), b"OLD-LAUNCHER\n")
        self.assertFalse(os.path.exists(os.path.join(self.config, "state.sqlite3.pre-schema9")))
        _code, kept = self.row_status("aiusage", "barrier-accept")
        self.assertEqual(kept["status"], "accepted")
        self.assertTrue(kept["service_confirmed"])
        self.assertFalse(kept["retryable"])
        self.assertEqual(self.transport.calls, 0)
        legacy = _load_vectors()[0]
        _code, old = self.row_status(legacy["source"], legacy["event_id"])
        self.assertEqual(old["status"], "accepted")
        self.assertEqual(old["notification_id"], legacy["notification_id"])

        self._prepare_legacy()
        self._expect_interrupt("after-replace")
        held = sqlite3.connect(os.path.join(self.config, "state.sqlite3"))
        held.execute("BEGIN IMMEDIATE")
        try:
            recovered = self._recover()
        finally:
            held.rollback()
            held.close()
        self.assertTrue(recovered["code_restored"])
        self.assertFalse(recovered["database_restored"])
        self.assertEqual(recovered["database_rollback"], "blocked")
        self.assertFalse(recovered["delivery_preserved"])
        self.assertEqual(self._schema_version(), 9)
        self.assertEqual(self._read(self.launcher), b"OLD-LAUNCHER\n")
        self.assertFalse(os.path.exists(os.path.join(self.config, "state.sqlite3.pre-schema9")))
        legacy = _load_vectors()[0]
        _code, old = self.row_status(legacy["source"], legacy["event_id"])
        self.assertEqual(old["notification_id"], legacy["notification_id"])

    def test_delivery_blocks_database_rollback(self):
        self._prepare_legacy()
        self._expect_interrupt("after-replace")
        _source, _event, notification_id = application_identity(SALT, "aiusage", "after-upgrade-1")
        code, accepted, _text, _err = self.cli(
            _push("after-upgrade-1", title="Upgraded", body="Delivered")
        )
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["notification_id"], notification_id)
        recovered = self._recover()
        self.assertTrue(recovered["delivery_preserved"])
        self.assertFalse(recovered["database_restored"])
        self.assertTrue(recovered["code_restored"])
        self.assertEqual(recovered["schema_version"], 9)
        self.assertEqual(self._read(self.launcher), b"OLD-LAUNCHER\n")
        self.assertFalse(os.path.exists(os.path.join(self.config, "state.sqlite3.pre-schema9")))
        _code, view = self.row_status("aiusage", "after-upgrade-1")
        self.assertEqual(view["status"], "accepted")
        self.assertEqual(view["notification_id"], notification_id)
        legacy = _load_vectors()[0]
        _code, old = self.row_status(legacy["source"], legacy["event_id"])
        self.assertEqual(old["status"], "accepted")
        self.assertEqual(old["notification_id"], legacy["notification_id"])

    def test_mid_migration_failure_rolls_back_to_schema8(self):
        self._prepare_legacy()
        before = self._read(os.path.join(self.config, "state.sqlite3"))
        os.environ["NOTIFY_ME_TEST_MODE"] = "1"
        os.environ["NOTIFY_ME_TEST_MIGRATION_FAIL_AFTER"] = "1"
        try:
            code, body, _text, _err = self.cli(
                ["install", "--launcher", self.launcher, "--config-dir", self.config]
            )
        finally:
            os.environ.pop("NOTIFY_ME_TEST_MODE", None)
            os.environ.pop("NOTIFY_ME_TEST_MIGRATION_FAIL_AFTER", None)
        self.assert_error("state_schema_error", body)
        self.assertEqual(self._schema_version(), 8)
        self.assertEqual(self._read(os.path.join(self.config, "state.sqlite3")), before)
        code, blocked, _text, _err = self.cli(
            ["install", "--launcher", self.launcher, "--config-dir", self.config]
        )
        self.assert_error("recovery_required", blocked)
        self.assertEqual(self._read(os.path.join(self.config, "state.sqlite3")), before)
        recovered = self._recover()
        self.assertEqual(recovered["schema_version"], 8)
        self.assertEqual(self._read(os.path.join(self.config, "state.sqlite3")), before)
        self.assertFalse(os.path.exists(os.path.join(self.config, "install-recovery.json")))

    def test_binding_migration_uses_only_the_explicit_file(self):
        plugin = os.path.join(self.root, "plugin")
        os.mkdir(plugin, 0o700)
        chosen = os.path.join(plugin, "binding.json")
        decoy = os.path.join(plugin, "other-binding.json")
        replacement = os.path.join(plugin, "replacement.json")
        self._binding(chosen, "chosenkey1", "http://127.0.0.1:9/prefix")
        self._binding(decoy, "decoykey01", "http://127.0.0.1:9/prefix")
        self._binding(replacement, "replacekey", "http://127.0.0.1:9/prefix")
        os.chmod(decoy, 0)
        before = self._read(chosen)
        code, body, _text, _err = self.cli(["install", "--launcher", self.launcher])
        self.assertEqual(body["status"], "installed")
        self.assertFalse(os.path.exists(self.config))
        self.assertEqual(self._read(chosen), before)
        code, status, _text, _err = self.cli(["status"])
        self.assertEqual(status["status"], "configuration_missing")
        self.assertFalse(os.path.exists(self.config))
        for argv in (
            ["migrate-binding", "--config-dir", self.config],
            ["migrate-binding", "--source", "grok", "--config-dir", self.config],
            ["migrate-binding", "--source", "other", "--binding-file", chosen, "--config-dir", self.config],
        ):
            code, body, _text, _err = self.cli(argv)
            self.assert_error("invalid_arguments", body)
            self.assertFalse(os.path.exists(self.config))
        self.assertEqual(mode_of(decoy), 0)
        code, bound, text, _err = self.cli(
            [
                "migrate-binding",
                "--source",
                "grok",
                "--binding-file",
                chosen,
                "--config-dir",
                self.config,
            ]
        )
        self.assertEqual(bound["status"], "bound")
        self.assertEqual(bound["source"], "grok")
        self.assertFalse(bound["replaced"])
        self.assertNotIn("chosenkey1", text)
        self.assertNotIn("decoykey01", text)
        env_bytes = self._read(os.path.join(self.config, ".env"))
        self.assertIn(b"BARK_URL=http://127.0.0.1:9/prefix/chosenkey1\n", env_bytes)
        self.assertNotIn(b"decoykey01", env_bytes)
        self.assertEqual(mode_of(self.config), 0o700)
        self.assertEqual(mode_of(os.path.join(self.config, ".env")), 0o600)
        code, status, text, _err = self.cli(["status"])
        self.assertTrue(status["bound"])
        self.assertEqual(status["host"], "127.0.0.1")
        self.assertNotIn("chosenkey1", text)
        code, blocked, _text, _err = self.cli(
            [
                "migrate-binding",
                "--source",
                "cursor",
                "--binding-file",
                replacement,
                "--config-dir",
                self.config,
            ]
        )
        self.assert_error("binding_exists", blocked)
        self.assertEqual(self._read(os.path.join(self.config, ".env")), env_bytes)
        code, installed, _text, _err = self.cli(
            ["install", "--launcher", self.launcher, "--config-dir", self.config]
        )
        self.assertEqual(installed["status"], "installed")
        self.assertEqual(self._read(os.path.join(self.config, ".env")), env_bytes)
        self.assertEqual(self._read(chosen), before)
        code, replaced, _text, _err = self.cli(
            [
                "migrate-binding",
                "--source",
                "codex",
                "--binding-file",
                replacement,
                "--config-dir",
                self.config,
                "--replace-binding",
            ]
        )
        self.assertEqual(replaced["status"], "bound")
        self.assertTrue(replaced["replaced"])
        self.assertIn(b"replacekey", self._read(os.path.join(self.config, ".env")))
        self.assertNotIn(b"decoykey01", self._read(os.path.join(self.config, ".env")))

    def _prepare_legacy(self):
        if os.path.isdir(self.config):
            for name in os.listdir(self.config):
                os.chmod(os.path.join(self.config, name), 0o700)
                os.unlink(os.path.join(self.config, name))
            os.rmdir(self.config)
        self.make_config()
        self.write_env()
        _write_schema8(Path(self.config) / "state.sqlite3", _load_vectors())
        if os.path.exists(self.launcher):
            os.chmod(self.launcher, 0o700)
            os.unlink(self.launcher)
        previous = self.launcher + ".previous"
        if os.path.exists(previous):
            os.unlink(previous)
        write_private(self.launcher, b"OLD-LAUNCHER\n")
        os.chmod(self.launcher, 0o700)
        return self._read(os.path.join(self.config, ".env"))

    def _expect_interrupt(self, name):
        self._expect_interrupt_at(self.launcher, self.config, name)

    def _expect_interrupt_at(self, launcher, config, name):
        argv = ["install", "--launcher", launcher]
        if config is not None:
            argv.extend(["--config-dir", config])
        code, body, _text, _err = self.cli(argv, env=self.env(NOTIFY_ME_TEST_FAULT=name))
        self.assertEqual(code, 1)
        self.assert_error("install_interrupted", body)
        if config is not None:
            self.assertTrue(os.path.exists(os.path.join(config, "install-recovery.json")))
        else:
            self.assertTrue(os.path.exists(os.path.join(os.path.dirname(launcher), "install-recovery.json")))

    def _sha(self, data):
        import hashlib

        return hashlib.sha256(data).hexdigest()

    def _remove_tree(self, path):
        if not os.path.isdir(path):
            return
        for dirpath, dirnames, filenames in os.walk(path, topdown=False):
            for name in filenames + dirnames:
                target = os.path.join(dirpath, name)
                try:
                    os.chmod(target, 0o700)
                except OSError:
                    pass
                if os.path.isdir(target) and not os.path.islink(target):
                    os.rmdir(target)
                else:
                    os.unlink(target)
        os.rmdir(path)

    def _recover(self):
        code, body, _text, _err = self.cli(
            ["recover", "--launcher", self.launcher, "--config-dir", self.config]
        )
        self.assertEqual(code, 0, body)
        self.assertEqual(body["status"], "recovered")
        self.assertFalse(os.path.exists(os.path.join(self.config, "install-recovery.json")))
        return body

    def _binding(self, path, key, server):
        payload = {"server": server, "host": "127.0.0.1", "key": key}
        write_private(path, json.dumps(payload).encode("utf-8"))
        os.chmod(path, 0o600)

    def _read(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def _salt(self):
        connection = sqlite3.connect(os.path.join(self.config, "state.sqlite3"))
        try:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key='scope_salt'"
            ).fetchone()
        finally:
            connection.close()
        return json.loads(row[0])

    def _schema_version(self):
        connection = sqlite3.connect(os.path.join(self.config, "state.sqlite3"))
        try:
            return connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        finally:
            connection.close()

    def _agent_status(self):
        connection = sqlite3.connect(os.path.join(self.config, "state.sqlite3"))
        try:
            return connection.execute(
                "SELECT status FROM notifications WHERE notification_id='agent-kept'"
            ).fetchone()[0]
        finally:
            connection.close()
