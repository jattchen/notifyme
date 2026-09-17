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


def _truncate_before_main_call(source: str) -> str:
    lines = source.splitlines(keepends=True)
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == 'main "$@"':
            return "".join(lines[:index])
    return source


def _run_piped_install_sh(env, stdout_tty, script=None):
    if script is None:
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

    def test_truncated_before_main_does_not_run_plugin_install(self):
        truncated = _truncate_before_main_call(
            INSTALL_SH.read_text(encoding="utf-8")
        )
        _returncode, stderr = _run_piped_install_sh(
            self.env,
            stdout_tty=True,
            script=truncated.encode("utf-8"),
        )
        self.assertFalse(self.grok_log.is_file(), stderr)

    def test_complete_plugin_skips_install_and_clone_before_bind(self):
        grok_home = Path(self.env["GROK_HOME"])
        plugin = grok_home / "installed-plugins" / "notify-me-already"
        scripts = plugin / "scripts"
        package = scripts / "notify_me"
        package.mkdir(parents=True)
        bind_log = Path(self.tmpdir.name) / "bind.log"
        scripts.joinpath("notify_me.py").write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    Path({!r}).write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n"
            "    raise SystemExit(0)\n".format(str(bind_log)),
            encoding="utf-8",
        )
        scripts.joinpath("mcp_server.py").write_text("# mcp\n", encoding="utf-8")
        package.joinpath("__init__.py").write_text("", encoding="utf-8")
        package.joinpath("paths.py").write_text(
            "from pathlib import Path\n"
            "\n"
            "def installed_plugin_root(grok_dir=None):\n"
            "    return Path({!r})\n".format(str(plugin)),
            encoding="utf-8",
        )
        grok = Path(self.tmpdir.name) / "bin" / "grok"
        grok.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$GROK_STUB_LOG\"\n"
            "if [ \"$1\" = plugin ] && [ \"$2\" = install ]; then\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (Path(self.tmpdir.name) / "bin" / "gh").unlink()
        _returncode, stderr = _run_piped_install_sh(self.env, stdout_tty=True)
        logged = ""
        if self.grok_log.is_file():
            logged = self.grok_log.read_text(encoding="utf-8")
        self.assertNotIn("plugin install", logged, stderr)
        self.assertNotIn("没有 gh", stderr)
        self.assertIn("plugin enable notify-me", logged)
        self.assertIn("mcp add notify_me", logged)
        self.assertIn("mcp add notifyme", logged)
        reached_bind = bind_log.is_file() or "/dev/tty" in stderr
        self.assertTrue(reached_bind, stderr)
        if bind_log.is_file():
            self.assertEqual(bind_log.read_text(encoding="utf-8"), "install")


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
