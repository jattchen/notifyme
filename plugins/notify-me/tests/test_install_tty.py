import json
import os
import pty
import subprocess
import sys
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from notify_me.cli import main  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO / "install.sh"
TTY_REFUSAL = "请在 macOS「终端」里运行"


def _stub_env(tmpdir):
    bindir = Path(tmpdir) / "bin"
    bindir.mkdir()
    grok_log = Path(tmpdir) / "grok.log"
    grok = bindir / "grok"
    grok.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$GROK_STUB_LOG"\nexit 1\n',
        encoding="utf-8",
    )
    grok.chmod(0o755)
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    gh.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = "{}:/usr/bin:/bin".format(bindir)
    env["GROK_STUB_LOG"] = str(grok_log)
    env["GROK_HOME"] = str(Path(tmpdir) / "grok-home")
    return env, grok_log


def _run_piped_install_sh(env, stdout_tty):
    script = INSTALL_SH.read_bytes()
    if not stdout_tty:
        completed = subprocess.run(
            ["bash"],
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=15,
            start_new_session=True,
        )
        return completed.returncode, completed.stderr.decode("utf-8", "replace")

    master_fd, slave_fd = pty.openpty()
    try:
        def _drain():
            try:
                while True:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
            except OSError:
                return

        reader = threading.Thread(target=_drain, daemon=True)
        proc = subprocess.Popen(
            ["bash"],
            stdin=subprocess.PIPE,
            stdout=slave_fd,
            stderr=subprocess.PIPE,
            env=env,
        )
        reader.start()
        os.close(slave_fd)
        slave_fd = -1
        _, stderr = proc.communicate(script, timeout=15)
        reader.join(timeout=2)
        return proc.returncode, stderr.decode("utf-8", "replace")
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)


class PipedInstallShTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.env, self.grok_log = _stub_env(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_piped_install_sh_does_not_refuse_when_stdout_is_tty(self):
        returncode, stderr = _run_piped_install_sh(self.env, stdout_tty=True)
        self.assertNotIn(TTY_REFUSAL, stderr)
        self.assertTrue(self.grok_log.is_file(), stderr)
        self.assertIn("plugin install", self.grok_log.read_text(encoding="utf-8"))
        self.assertNotEqual(returncode, 0)

    def test_piped_install_sh_refuses_without_terminal(self):
        returncode, stderr = _run_piped_install_sh(self.env, stdout_tty=False)
        self.assertEqual(returncode, 1)
        self.assertIn(TTY_REFUSAL, stderr)
        self.assertFalse(self.grok_log.is_file())

    def test_piped_install_sh_removes_gh_fallback_temp_dir(self):
        scratch = Path(self.tmpdir.name) / "scratch"
        scratch.mkdir()
        self.env["TMPDIR"] = str(scratch)
        returncode, stderr = _run_piped_install_sh(self.env, stdout_tty=True)
        self.assertNotEqual(returncode, 0, stderr)
        leftovers = [path.name for path in scratch.iterdir()]
        self.assertEqual(leftovers, [])


class TtyStdout(StringIO):
    def isatty(self):
        return True


class InstallCliTtyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["GROK_HOME"] = self.tmpdir.name
        os.environ["GROK_NOTIFY_ME_HOME"] = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("GROK_HOME", None)
        os.environ.pop("GROK_NOTIFY_ME_HOME", None)

    def test_install_with_stdout_tty_does_not_refuse_piped_stdin(self):
        buf = TtyStdout()
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            with mock.patch("sys.stdin.isatty", return_value=False), mock.patch(
                "sys.stdout", buf
            ), mock.patch("sys.stderr", StringIO()):
                code = main(["install"])
        payload = json.loads(buf.getvalue())
        self.assertNotEqual(payload.get("error", {}).get("code"), "tty_required")
        self.assertEqual(code, 1)
