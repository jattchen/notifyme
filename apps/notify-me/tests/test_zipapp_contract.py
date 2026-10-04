# -*- coding: utf-8 -*-
"""Group 7: the built zipapp, not the in-process package, must keep the contract."""

import io
import json
import os
import stat
import subprocess
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

from notify_me_app.installation import (
    build_digest,
    build_zipapp_bytes,
    commit_label,
    package_files,
    repo_root_from_here,
)

from tests.support import IsolatedCase, mode_of, write_private


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        self.server.payloads.append((self.path, raw))
        body = b'{"code":200}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class ZipappContractTests(IsolatedCase):
    def test_artifact_version_and_localhost_push(self):
        root = repo_root_from_here()
        files = package_files(root)
        label = commit_label(root)
        digest = build_digest(label, files)
        blob, built_label, built_digest = build_zipapp_bytes(root)
        self.assertEqual(built_label, label)
        self.assertEqual(built_digest, digest)
        self.assertTrue(blob.startswith(b"#!/usr/bin/env python3\n"))
        archive = zipfile.ZipFile(io.BytesIO(blob.split(b"\n", 1)[1]))
        names = set(archive.namelist())
        self.assertIn("notify_me_app/build_info.py", names)
        self.assertIn("__main__.py", names)
        self.assertEqual(
            archive.read("__main__.py"),
            b"from notify_me_app.cli import main\nraise SystemExit(main())\n",
        )
        packaged = {rel: data for rel, data, _file_digest in files}
        for name in names:
            if name in ("notify_me_app/build_info.py", "__main__.py"):
                continue
            self.assertEqual(archive.read(name), packaged[name])
        info = archive.read("notify_me_app/build_info.py").decode("utf-8")
        self.assertIn('SOURCE_COMMIT = "{}"'.format(label), info)
        self.assertIn('BUILD_DIGEST = "{}"'.format(digest), info)
        self.assertIn('ARTIFACT = "zipapp"', info)

        artifact = os.path.join(self.root, "notify-me")
        write_private(artifact, blob)
        os.chmod(artifact, 0o700)
        self.assertEqual(mode_of(artifact), 0o700)
        missing = os.path.join(self.root, "missing-config")
        env = self._artifact_env(missing)
        python = self._python_version(artifact, env)
        direct = self._run([artifact, "version", "--json"], env)
        self.assertEqual(python["source_commit"], label)
        self.assertEqual(python["build_digest"], digest)
        self.assertEqual(direct["source_commit"], label)
        self.assertEqual(direct["build_digest"], digest)
        self.assertEqual(python["artifact"], "zipapp")
        self.assertEqual(python["protocol_version"], 1)
        self.assertEqual(python["python_version"], "3.9.6")
        self.assertFalse(os.path.exists(missing))

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        server.payloads = []
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            port = server.server_address[1]
            key = "zipappkey1"
            self.make_config()
            url = "http://127.0.0.1:{}/{}".format(port, key)
            descriptor = os.open(os.path.join(self.config, ".env"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write("BARK_URL={}\n".format(url))
            os.chmod(os.path.join(self.config, ".env"), 0o600)
            env = self._artifact_env(self.config)
            env["NOTIFY_ME_TEST_NOW"] = "1700000000"
            body = self._run(
                [
                    artifact,
                    "push",
                    "--source",
                    "aiusage",
                    "--event-id",
                    "zip-evt",
                    "--priority",
                    "P0",
                    "--title",
                    "Zip",
                    "--body",
                    "App",
                ],
                env,
            )
            self.assertTrue(body["ok"])
            self.assertEqual(body["status"], "accepted")
            self.assertTrue(body["service_confirmed"])
            self.assertFalse(body["outcome_uncertain"])
            self.assertEqual(body["expires_at"], 1700000900)
            self.assertNotIn(key, json.dumps(body))
            self.assertEqual(len(server.payloads), 1)
            path, raw = server.payloads[0]
            self.assertEqual(path, "/push")
            posted = json.loads(raw.decode("utf-8"))
            self.assertEqual(posted["id"], body["notification_id"])
            self.assertEqual(posted["device_key"], key)
            self.assertEqual(posted["group"], "notify-me")
            replay = self._run(
                [
                    artifact,
                    "push",
                    "--source",
                    "aiusage",
                    "--event-id",
                    "zip-evt",
                    "--priority",
                    "P0",
                    "--title",
                    "Zip",
                    "--body",
                    "App",
                ],
                env,
            )
            self.assertEqual(replay["status"], "deduplicated")
            self.assertEqual(replay["previous_status"], "accepted")
            self.assertEqual(len(server.payloads), 1)
        finally:
            server.shutdown()
            server.server_close()

    def _artifact_env(self, config):
        return {
            "HOME": self.home,
            "PATH": "/usr/bin:/bin",
            "NOTIFY_ME_FORBID_HOST_PATHS": "1",
            "NOTIFY_ME_TEST_MODE": "1",
            "NOTIFY_ME_CONFIG_DIR": config,
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def _run(self, argv, env):
        completed = subprocess.run(
            argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        self.assertLessEqual(len(completed.stdout), 65536)
        self.assertTrue(completed.stdout.endswith(b"\n"))
        return json.loads(completed.stdout.decode("utf-8"))

    def _python_version(self, artifact, env):
        return self._run(["/usr/bin/python3", artifact, "version", "--json"], env)
