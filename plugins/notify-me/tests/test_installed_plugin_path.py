import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SCRIPTS))

from notify_me.bark import BarkEndpoint  # noqa: E402
from notify_me.binding import Binding  # noqa: E402
from notify_me.errors import NotifyMeError  # noqa: E402
from notify_me.install import _ensure_mcp, _ensure_plugin, run_install  # noqa: E402
from notify_me.paths import installed_plugin_root  # noqa: E402
from notify_me import paths as notify_me_paths  # noqa: E402

DOCUMENTED_SCRIPT = "~/.grok/notify-me"
DOCUMENTED_GLOB = "~/.grok/installed-plugins/notify-me-*/scripts/notify_me.py"
HARDCODED_HASH = "notify-me-b47b0296"
LEGACY_PLUGIN = "~/.grok/plugins/notify-me"
GITHUB_REPO = "jattchen/notifyme"
LEGACY_GITHUB_REPO = "jattchen/notify_me"


def _make_plugin(installed, name, mtime=None):
    root = installed / name
    script = root / "scripts" / "notify_me.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# notify-me\n", encoding="utf-8")
    (root / "scripts" / "mcp_server.py").write_text("# mcp\n", encoding="utf-8")
    package = root / "scripts" / "notify_me"
    package.mkdir(parents=True, exist_ok=True)
    (package / "paths.py").write_text("# paths\n", encoding="utf-8")
    if mtime is not None:
        os.utime(root, (mtime, mtime))
        os.utime(script.parent, (mtime, mtime))
        os.utime(script, (mtime, mtime))
    return root.resolve()


def _make_stub_plugin(installed, name, mtime=None):
    root = installed / name
    script = root / "scripts" / "notify_me.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# notify-me stub\n", encoding="utf-8")
    if mtime is not None:
        os.utime(root, (mtime, mtime))
        os.utime(script.parent, (mtime, mtime))
        os.utime(script, (mtime, mtime))
    return root.resolve()


def _write_registry(installed, repos):
    payload = {"version": 1, "repos": repos}
    (installed / "registry.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


_TRAP_CLI = """\
#!/usr/bin/env python3
import json
import sys
sys.stdout.write(json.dumps({
    "ok": False,
    "error": {"code": "invalid_arguments", "message": "leftover"},
}))
sys.stdout.write("\\n")
raise SystemExit(1)
"""


def _make_trap_plugin(installed, name, mtime=None):
    root = _make_plugin(installed, name, mtime=mtime)
    (root / "scripts" / "notify_me.py").write_text(_TRAP_CLI, encoding="utf-8")
    return root


def _make_real_plugin(installed, name, mtime=None):
    root = installed / name
    dest = root / "scripts"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SCRIPTS, dest)
    if mtime is not None:
        os.utime(root, (mtime, mtime))
        os.utime(dest, (mtime, mtime))
        os.utime(dest / "notify_me.py", (mtime, mtime))
    return root.resolve()


def _extract_install_sh_resolver():
    text = (REPO / "install.sh").read_text(encoding="utf-8")
    start_token = "<<'NOTIFY_ME_RESOLVE_PLUGIN'\n"
    end_token = "\nNOTIFY_ME_RESOLVE_PLUGIN"
    start = text.index(start_token) + len(start_token)
    end = text.index(end_token, start)
    return text[start:end]


def _run_install_sh_resolver(grok_dir):
    source = _extract_install_sh_resolver()
    result = subprocess.run(
        ["python3", "-", str(grok_dir)],
        input=source,
        capture_output=True,
        text=True,
    )
    return result


def _make_old_api_plugin(installed, name, mtime=None):
    root = _make_plugin(installed, name, mtime=mtime)
    (root / "scripts" / "notify_me" / "__init__.py").write_text("", encoding="utf-8")
    return root


def _old_api_first_then_current(installed):
    leftover = _make_old_api_plugin(installed, "notify-me-oldapi", mtime=1_000)
    current = _make_real_plugin(installed, "notify-me-current", mtime=2_000)
    candidates = list(installed.glob("notify-me-*"))
    first = candidates[0]
    if first.resolve() != leftover:
        later = next(path for path in candidates if path.resolve() != first.resolve())
        shutil.rmtree(first)
        shutil.rmtree(later)
        leftover = _make_old_api_plugin(installed, first.name, mtime=1_000)
        current = _make_real_plugin(installed, later.name, mtime=2_000)
    return leftover, current


def _run_written_stable_entry_resolver(grok_dir):
    dest = notify_me_paths.write_stable_entry(grok_dir)
    source = Path(dest).read_text(encoding="utf-8")
    prefix, _, _ = source.partition("resolve = _resolver()")
    runner = prefix + (
        "resolve = _resolver()\n"
        "root = resolve() if resolve is not None else None\n"
        "if root is None:\n"
        "    raise SystemExit(1)\n"
        "print(root)\n"
    )
    env = os.environ.copy()
    env["GROK_HOME"] = str(grok_dir)
    return subprocess.run(
        [sys.executable, "-"],
        input=runner,
        capture_output=True,
        text=True,
        env=env,
    )


class InstalledPluginRootTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmpdir.name)
        self.installed = self.home / "installed-plugins"
        self.installed.mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_single_hashed_dir_is_found(self):
        plugin = _make_plugin(self.installed, "notify-me-abc123")
        found = installed_plugin_root(self.home)
        self.assertEqual(found, plugin)
        self.assertTrue((found / "scripts" / "notify_me.py").is_file())

    def test_does_not_use_legacy_plugins_dir(self):
        plugin = _make_plugin(self.installed, "notify-me-abc123")
        legacy = self.home / "plugins" / "notify-me"
        (legacy / "scripts").mkdir(parents=True)
        (legacy / "scripts" / "notify_me.py").write_text("# legacy\n", encoding="utf-8")
        self.assertEqual(installed_plugin_root(self.home), plugin)

    def test_registry_path_wins_over_newer_leftover(self):
        leftover = _make_plugin(self.installed, "notify-me-oldhash", mtime=2_000)
        chosen = _make_plugin(self.installed, "notify-me-reghash", mtime=1_000)
        _write_registry(
            self.installed,
            {
                leftover.name: {
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "path": str(leftover),
                    "plugins": {"other": {"version": "1.0.0"}},
                },
                chosen.name: {
                    "updated_at": "2026-02-01T00:00:00+00:00",
                    "path": str(chosen),
                    "plugins": {"notify-me": {"version": "1.0.0"}},
                },
            },
        )
        self.assertEqual(installed_plugin_root(self.home), chosen)

    def test_latest_registry_notify_me_wins_among_several(self):
        older = _make_plugin(self.installed, "notify-me-oldone", mtime=2_000)
        newer = _make_plugin(self.installed, "notify-me-newone", mtime=1_000)
        _write_registry(
            self.installed,
            {
                older.name: {
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "path": str(older),
                    "plugins": {"notify-me": {"version": "1.0.0"}},
                },
                newer.name: {
                    "updated_at": "2026-03-01T00:00:00+00:00",
                    "path": str(newer),
                    "plugins": {"notify-me": {"version": "1.0.0"}},
                },
            },
        )
        self.assertEqual(installed_plugin_root(self.home), newer)

    def test_newest_mtime_when_registry_missing(self):
        older = _make_plugin(self.installed, "notify-me-aaaaaaa", mtime=1_000)
        newer = _make_plugin(self.installed, "notify-me-bbbbbbb", mtime=2_000)
        self.assertEqual(installed_plugin_root(self.home), newer)
        self.assertNotEqual(installed_plugin_root(self.home), older)

    def test_older_complete_wins_over_newer_stub_without_registry(self):
        complete = _make_plugin(self.installed, "notify-me-complete", mtime=1_000)
        stub = _make_stub_plugin(self.installed, "notify-me-stub", mtime=2_000)
        found = installed_plugin_root(self.home)
        self.assertEqual(found, complete)
        self.assertNotEqual(found, stub)

    def test_only_stubs_are_not_usable(self):
        _make_stub_plugin(self.installed, "notify-me-stub-one", mtime=1_000)
        _make_stub_plugin(self.installed, "notify-me-stub-two", mtime=2_000)
        self.assertIsNone(installed_plugin_root(self.home))

    def test_corrupt_registry_falls_back_to_newest(self):
        older = _make_plugin(self.installed, "notify-me-aaaaaaa", mtime=1_000)
        newer = _make_plugin(self.installed, "notify-me-bbbbbbb", mtime=2_000)
        (self.installed / "registry.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(installed_plugin_root(self.home), newer)
        self.assertNotEqual(installed_plugin_root(self.home), older)

    def test_older_complete_wins_over_newer_stub_when_registry_corrupt(self):
        complete = _make_plugin(self.installed, "notify-me-complete", mtime=1_000)
        stub = _make_stub_plugin(self.installed, "notify-me-stub", mtime=2_000)
        (self.installed / "registry.json").write_text("{not json", encoding="utf-8")
        found = installed_plugin_root(self.home)
        self.assertEqual(found, complete)
        self.assertNotEqual(found, stub)

    def test_stale_registry_path_falls_back_to_usable_dir(self):
        missing = self.installed / "notify-me-missing"
        usable = _make_plugin(self.installed, "notify-me-present")
        _write_registry(
            self.installed,
            {
                "notify-me-missing": {
                    "updated_at": "2026-09-01T00:00:00+00:00",
                    "path": str(missing),
                    "plugins": {"notify-me": {"version": "1.0.0"}},
                }
            },
        )
        self.assertEqual(installed_plugin_root(self.home), usable)

    def test_ignores_dirs_without_script_and_other_plugins(self):
        empty = self.installed / "notify-me-empty"
        empty.mkdir()
        other = _make_plugin(self.installed, "something-else-ffff")
        plugin = _make_plugin(self.installed, "notify-me-realone")
        self.assertEqual(installed_plugin_root(self.home), plugin)
        self.assertNotEqual(installed_plugin_root(self.home), other)
        self.assertTrue(empty.is_dir())

    def test_returns_none_when_missing(self):
        self.assertIsNone(installed_plugin_root(self.home))

    def test_does_not_hardcode_a_hash(self):
        plugin = _make_plugin(self.installed, "notify-me-zzzzzzzz")
        found = installed_plugin_root(self.home)
        self.assertEqual(found, plugin)
        self.assertNotIn("b47b0296", str(found))


class WriteStableEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmpdir.name)
        self.installed = self.home / "installed-plugins"
        self.installed.mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_failed_rewrite_keeps_existing_stable_entry(self):
        _make_plugin(self.installed, "notify-me-abc123")
        dest = notify_me_paths.stable_entry_path(self.home)
        old_contents = "#!/usr/bin/env python3\n# previous-stable-entry\n"
        dest.write_text(old_contents, encoding="utf-8")
        os.chmod(dest, 0o700)

        def fail_write(*args, **kwargs):
            raise OSError("disk full")

        with mock.patch.object(Path, "write_text", side_effect=fail_write):
            with mock.patch(
                "notify_me.paths.os.replace",
                side_effect=fail_write,
            ):
                with self.assertRaises(OSError):
                    notify_me_paths.write_stable_entry(self.home)

        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_text(encoding="utf-8"), old_contents)
        self.assertTrue(os.access(dest, os.X_OK))


class DualMcpNameInstallTests(unittest.TestCase):
    def test_install_py_adds_notifyme_and_keeps_notify_me(self):
        text = (SCRIPTS / "notify_me" / "install.py").read_text(encoding="utf-8")
        self.assertIn('"notify_me"', text)
        self.assertIn('"notifyme"', text)
        self.assertIn('"--name", "notifyme"', text)
        self.assertNotIn("mcp\", \"remove\", \"notify_me\"", text)

    def test_mcp_json_declares_both_servers(self):
        payload = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        servers = payload["mcpServers"]
        self.assertIn("notify_me", servers)
        self.assertIn("notifyme", servers)
        self.assertEqual(
            servers["notify_me"]["args"],
            ["-u", "${CLAUDE_PLUGIN_ROOT}/scripts/mcp_server.py"],
        )
        self.assertEqual(
            servers["notifyme"]["args"],
            ["-u", "${CLAUDE_PLUGIN_ROOT}/scripts/mcp_server.py", "--name", "notifyme"],
        )


class GitHubRepoNameTests(unittest.TestCase):
    def test_install_entrypoints_use_notifyme_repo(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        install_sh = (REPO / "install.sh").read_text(encoding="utf-8")
        install_py = (SCRIPTS / "notify_me" / "install.py").read_text(encoding="utf-8")
        for text, label in (
            (readme, "README.md"),
            (install_sh, "install.sh"),
            (install_py, "install.py"),
        ):
            self.assertIn(GITHUB_REPO, text, label)
            self.assertNotIn(LEGACY_GITHUB_REPO, text, label)


class DocumentedCommandTests(unittest.TestCase):
    def test_skill_and_readme_use_installed_plugins_glob(self):
        skill = (ROOT / "skills" / "notify-me" / "SKILL.md").read_text(encoding="utf-8")
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        for text, label in ((skill, "SKILL.md"), (readme, "README.md")):
            self.assertIn(DOCUMENTED_SCRIPT, text, label)
            self.assertNotIn(DOCUMENTED_GLOB, text, label)
            self.assertNotIn(LEGACY_PLUGIN, text, label)
            self.assertNotIn(HARDCODED_HASH, text, label)
            self.assertNotIn("api.day.app", text, label)
        self.assertIn("python3 {} install".format(DOCUMENTED_SCRIPT), skill)
        self.assertIn("python3 {} doctor".format(DOCUMENTED_SCRIPT), skill)
        self.assertIn("python3 {} doctor".format(DOCUMENTED_SCRIPT), readme)

    def test_documented_glob_expands_to_single_install(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            plugin = _make_plugin(
                home / ".grok" / "installed-plugins",
                "notify-me-deadbeef",
            )
            env = os.environ.copy()
            env["HOME"] = str(home)
            expanded = subprocess.check_output(
                [
                    "bash",
                    "-lc",
                    "printf '%s' ~/.grok/installed-plugins/notify-me-*/scripts/notify_me.py",
                ],
                env=env,
                text=True,
            )
            self.assertEqual(Path(expanded).resolve(), plugin / "scripts" / "notify_me.py")

    def test_documented_doctor_hits_current_plugin_when_leftovers_exist(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            grok = home / ".grok"
            installed = grok / "installed-plugins"
            leftover = _make_trap_plugin(installed, "notify-me-leftover", mtime=2_000)
            current = _make_real_plugin(installed, "notify-me-current", mtime=1_000)
            _write_registry(
                installed,
                {
                    leftover.name: {
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "path": str(leftover),
                        "plugins": {"other": {"version": "1.0.0"}},
                    },
                    current.name: {
                        "updated_at": "2026-02-01T00:00:00+00:00",
                        "path": str(current),
                        "plugins": {"notify-me": {"version": "1.0.0"}},
                    },
                },
            )
            self.assertEqual(installed_plugin_root(grok), current)
            self.assertTrue(hasattr(notify_me_paths, "write_stable_entry"))
            entry = notify_me_paths.write_stable_entry(grok)
            self.assertIsNotNone(entry)

            skill = (ROOT / "skills" / "notify-me" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            readme = (REPO / "README.md").read_text(encoding="utf-8")
            match = re.search(r"^python3 (\S+) doctor$", skill, re.M)
            self.assertIsNotNone(match, "SKILL.md must document a doctor command")
            command = match.group(0)
            self.assertNotIn(
                "notify-me-*",
                command,
                "documented doctor must not be an unquoted glob",
            )
            self.assertIn(command, skill)
            self.assertIn(command, readme)

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["GROK_HOME"] = str(grok)
            env["GROK_NOTIFY_ME_HOME"] = str(home / "state")
            result = subprocess.run(
                ["bash", "-lc", command],
                env=env,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout or "{}")
            self.assertNotEqual(
                payload.get("error", {}).get("code"),
                "invalid_arguments",
                result.stdout,
            )
            self.assertTrue(payload.get("ok"), result.stdout)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual(payload.get("error", {}).get("message"), "leftover")

    def test_stable_entry_resolves_when_grok_home_uses_tilde(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            grok = home / "custom"
            plugin = _make_real_plugin(
                grok / "installed-plugins",
                "notify-me-current",
            )
            entry = notify_me_paths.write_stable_entry(grok)
            self.assertIsNotNone(entry)
            self.assertTrue(plugin.is_dir())

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["GROK_HOME"] = "~/custom"
            env["GROK_NOTIFY_ME_HOME"] = str(home / "state")
            result = subprocess.run(
                [sys.executable, str(entry), "doctor"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotIn(
                "notify-me is not installed",
                result.stderr,
                result.stderr,
            )
            payload = json.loads(result.stdout or "{}")
            self.assertTrue(payload.get("ok"), result.stdout)
            self.assertEqual(result.returncode, 0, result.stderr)


class InstallShResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmpdir.name)
        self.installed = self.home / "installed-plugins"
        self.installed.mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_install_sh_does_not_hardcode_legacy_plugin_path(self):
        text = (REPO / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn('$HOME/.grok/plugins/notify-me', text)
        self.assertNotIn("${HOME}/.grok/plugins/notify-me", text)
        self.assertIn("installed-plugins", text)
        self.assertIn("notify-me-*", text)
        self.assertNotIn(HARDCODED_HASH, text)
        self.assertNotIn("api.day.app", text)

    def test_install_sh_registers_notifyme_without_removing_notify_me(self):
        text = (REPO / "install.sh").read_text(encoding="utf-8")
        self.assertIn(
            'grok mcp add notify_me -- python3 -u "$plugin/scripts/mcp_server.py"',
            text,
        )
        self.assertIn(
            'grok mcp add notifyme -- python3 -u "$plugin/scripts/mcp_server.py" --name notifyme',
            text,
        )
        self.assertIn("notify_me:", text)
        self.assertIn("notifyme:", text)
        self.assertNotIn("mcp remove notify_me", text)

    def test_install_sh_resolver_delegates_to_python(self):
        source = _extract_install_sh_resolver()
        self.assertIn("installed_plugin_root", source)
        self.assertNotIn("registry.json", source)
        self.assertNotIn("st_mtime_ns", source)

    def test_install_sh_resolver_matches_python(self):
        leftover = _make_real_plugin(self.installed, "notify-me-oldhash", mtime=2_000)
        chosen = _make_real_plugin(self.installed, "notify-me-reghash", mtime=1_000)
        _write_registry(
            self.installed,
            {
                leftover.name: {
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "path": str(leftover),
                    "plugins": {"other": {"version": "1.0.0"}},
                },
                chosen.name: {
                    "updated_at": "2026-02-01T00:00:00+00:00",
                    "path": str(chosen),
                    "plugins": {"notify-me": {"version": "1.0.0"}},
                },
            },
        )
        expected = installed_plugin_root(self.home)
        result = _run_install_sh_resolver(self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), expected)

    def test_install_sh_resolver_newest_without_registry(self):
        _make_real_plugin(self.installed, "notify-me-aaaaaaa", mtime=1_000)
        newer = _make_real_plugin(self.installed, "notify-me-bbbbbbb", mtime=2_000)
        result = _run_install_sh_resolver(self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), newer)

    def test_install_sh_resolver_prefers_complete_over_newer_stub(self):
        complete = _make_real_plugin(self.installed, "notify-me-complete", mtime=1_000)
        _make_stub_plugin(self.installed, "notify-me-stub", mtime=2_000)
        result = _run_install_sh_resolver(self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), complete)

    def test_install_sh_resolver_only_stubs_exits_nonzero(self):
        _make_stub_plugin(self.installed, "notify-me-stub", mtime=2_000)
        result = _run_install_sh_resolver(self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_install_sh_resolver_missing_exits_nonzero(self):
        result = _run_install_sh_resolver(self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_resolvers_skip_complete_old_api_without_installed_plugin_root(self):
        leftover, current = _old_api_first_then_current(self.installed)
        candidates = list(self.installed.glob("notify-me-*"))
        self.assertEqual(candidates[0].resolve(), leftover)
        self.assertIn(current, [path.resolve() for path in candidates[1:]])
        leftover_paths = leftover / "scripts" / "notify_me" / "paths.py"
        self.assertTrue((leftover / "scripts" / "notify_me.py").is_file())
        self.assertTrue((leftover / "scripts" / "mcp_server.py").is_file())
        self.assertTrue(leftover_paths.is_file())
        self.assertNotIn(
            "installed_plugin_root",
            leftover_paths.read_text(encoding="utf-8"),
        )

        install_result = _run_install_sh_resolver(self.home)
        self.assertNotIn("ImportError", install_result.stderr, install_result.stderr)
        self.assertEqual(install_result.returncode, 0, install_result.stderr)
        self.assertEqual(Path(install_result.stdout.strip()).resolve(), current)

        entry_result = _run_written_stable_entry_resolver(self.home)
        self.assertNotIn("ImportError", entry_result.stderr, entry_result.stderr)
        self.assertEqual(entry_result.returncode, 0, entry_result.stderr)
        self.assertEqual(Path(entry_result.stdout.strip()).resolve(), current)


def _write_grok_mcp_stub(bindir):
    grok = bindir / "grok"
    grok.write_text(
        """#!/bin/sh
if [ "$1" = mcp ] && [ "$2" = list ]; then
  printf '%s\\n' "$GROK_MCP_LIST"
  exit 0
fi
if [ "$1" = mcp ] && [ "$2" = add ]; then
  printf '%s\\n' "$*" >> "$GROK_MCP_ADD_LOG"
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    grok.chmod(0o755)


def _stale_mcp_list(old_plugin):
    server = old_plugin / "scripts" / "mcp_server.py"
    return (
        "  notify_me: python3 -u {0}\n"
        "  notifyme: python3 -u {0} --name notifyme\n"
    ).format(server)


def _extract_install_sh_mcp_register():
    text = (REPO / "install.sh").read_text(encoding="utf-8")
    end = text.index("# Do not inherit the install script pipe")
    for token in (
        'mcp_list="$(grok mcp list',
        "if ! grok mcp list",
        "grok mcp add notify_me",
    ):
        found = text.find(token)
        if found != -1 and found < end:
            return text[found:end]
    raise AssertionError("install.sh MCP registration block not found")


def _run_install_sh_mcp_register(plugin, env):
    block = _extract_install_sh_mcp_register()
    script = "plugin={}\n{}".format(shlex.quote(str(plugin)), block)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )


class EnsureMcpUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        installed = self.root / "installed-plugins"
        installed.mkdir()
        self.old_plugin = _make_plugin(installed, "notify-me-b47b0296")
        self.new_plugin = _make_plugin(installed, "notify-me-2d1cddc3")
        self.add_log = self.root / "mcp-add.log"
        _write_grok_mcp_stub(self.bindir)
        self.env = os.environ.copy()
        self.env["PATH"] = "{}:/usr/bin:/bin".format(self.bindir)
        self.env["GROK_MCP_LIST"] = _stale_mcp_list(self.old_plugin)
        self.env["GROK_MCP_ADD_LOG"] = str(self.add_log)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _added_commands(self):
        if not self.add_log.is_file():
            return ""
        return self.add_log.read_text(encoding="utf-8")

    def _assert_mcp_rewritten_to_current_plugin(self):
        logged = self._added_commands()
        new_server = str(self.new_plugin / "scripts" / "mcp_server.py")
        old_server = str(self.old_plugin / "scripts" / "mcp_server.py")
        self.assertIn("mcp add notify_me", logged)
        self.assertIn("mcp add notifyme", logged)
        self.assertIn(new_server, logged)
        self.assertEqual(logged.count(new_server), 2)
        self.assertNotIn(old_server, logged)
        self.assertNotIn("notify-me-b47b0296", logged)

    def test_ensure_mcp_rewrites_existing_names_to_current_plugin_dir(self):
        original_path = os.environ.get("PATH")
        os.environ["PATH"] = self.env["PATH"]
        os.environ["GROK_MCP_LIST"] = self.env["GROK_MCP_LIST"]
        os.environ["GROK_MCP_ADD_LOG"] = self.env["GROK_MCP_ADD_LOG"]
        try:
            _ensure_mcp(self.new_plugin)
        finally:
            if original_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = original_path
            os.environ.pop("GROK_MCP_LIST", None)
            os.environ.pop("GROK_MCP_ADD_LOG", None)
        self._assert_mcp_rewritten_to_current_plugin()

    def test_install_sh_rewrites_existing_names_to_current_plugin_dir(self):
        result = _run_install_sh_mcp_register(self.new_plugin, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_mcp_rewritten_to_current_plugin()


def _write_grok_log_stub(bindir):
    grok = bindir / "grok"
    grok.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$GROK_STUB_LOG"
exit 0
""",
        encoding="utf-8",
    )
    grok.chmod(0o755)


class EnsurePluginSelfInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        self.grok_home = self.root / "grok-home"
        self.installed = self.grok_home / "installed-plugins"
        self.installed.mkdir(parents=True)
        self.grok_log = self.root / "grok.log"
        _write_grok_log_stub(self.bindir)
        self.env = os.environ.copy()
        self.env["PATH"] = "{}:/usr/bin:/bin".format(self.bindir)
        self.env["GROK_HOME"] = str(self.grok_home)
        self.env["GROK_STUB_LOG"] = str(self.grok_log)
        self.env["GROK_NOTIFY_ME_HOME"] = str(self.root / "state")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _logged(self):
        if not self.grok_log.is_file():
            return ""
        return self.grok_log.read_text(encoding="utf-8")

    def _run_ensure_plugin_from(self, plugin):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys;"
                    "sys.path.insert(0, sys.argv[1]);"
                    "from notify_me.install import _ensure_plugin;"
                    "_ensure_plugin()"
                ),
                str(plugin / "scripts"),
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def test_ensure_plugin_does_not_reinstall_current_hash_dir(self):
        plugin = _make_real_plugin(self.installed, "notify-me-abc123")
        (plugin / "plugin.json").write_text("{}\n", encoding="utf-8")
        result = self._run_ensure_plugin_from(plugin)
        self.assertEqual(result.returncode, 0, result.stderr)
        logged = self._logged()
        self.assertNotIn("plugin install {}".format(plugin), logged)
        self.assertNotIn("jattchen/notifyme#plugins/notify-me", logged)

    def test_ensure_plugin_does_not_reinstall_leftover_hash_dir(self):
        leftover = _make_real_plugin(self.installed, "notify-me-oldhash", mtime=2_000)
        current = _make_real_plugin(self.installed, "notify-me-current", mtime=1_000)
        (leftover / "plugin.json").write_text("{}\n", encoding="utf-8")
        (current / "plugin.json").write_text("{}\n", encoding="utf-8")
        _write_registry(
            self.installed,
            {
                leftover.name: {
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "path": str(leftover),
                    "plugins": {"other": {"version": "1.0.0"}},
                },
                current.name: {
                    "updated_at": "2026-02-01T00:00:00+00:00",
                    "path": str(current),
                    "plugins": {"notify-me": {"version": "1.0.0"}},
                },
            },
        )
        result = self._run_ensure_plugin_from(leftover)
        self.assertEqual(result.returncode, 0, result.stderr)
        logged = self._logged()
        self.assertNotIn("plugin install {}".format(leftover), logged)
        self.assertNotIn("plugin install {}".format(current), logged)
        self.assertNotIn("jattchen/notifyme#plugins/notify-me", logged)

    def test_ensure_plugin_source_install_failure_does_not_use_leftover(self):
        leftover = _make_real_plugin(self.installed, "notify-me-oldhash", mtime=1_000)
        (leftover / "plugin.json").write_text("{}\n", encoding="utf-8")
        source = self.root / "src" / "plugins" / "notify-me"
        shutil.copytree(ROOT, source)
        grok = self.bindir / "grok"
        grok.write_text(
            """#!/bin/sh
printf '%s\\n' "$*" >> "$GROK_STUB_LOG"
if [ "$1" = plugin ] && [ "$2" = install ]; then
  printf '%s\\n' "refused local source install" >&2
  exit 1
fi
exit 0
""",
            encoding="utf-8",
        )
        grok.chmod(0o755)
        self.assertFalse(
            str(source.resolve()).startswith(str(self.installed.resolve())),
        )
        self.assertEqual(source.parent.name, "plugins")
        original = {
            "PATH": os.environ.get("PATH"),
            "GROK_HOME": os.environ.get("GROK_HOME"),
            "GROK_STUB_LOG": os.environ.get("GROK_STUB_LOG"),
            "GROK_NOTIFY_ME_HOME": os.environ.get("GROK_NOTIFY_ME_HOME"),
        }
        os.environ["PATH"] = self.env["PATH"]
        os.environ["GROK_HOME"] = self.env["GROK_HOME"]
        os.environ["GROK_STUB_LOG"] = self.env["GROK_STUB_LOG"]
        os.environ["GROK_NOTIFY_ME_HOME"] = self.env["GROK_NOTIFY_ME_HOME"]
        try:
            with mock.patch(
                "notify_me.install.plugin_root",
                return_value=source,
            ):
                with self.assertRaises(NotifyMeError) as ctx:
                    dest = _ensure_plugin()
                    self.fail("used leftover {}".format(dest))
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(ctx.exception.code, "plugin_install_failed")
        self.assertIn("plugin install {}".format(source), self._logged())
        self.assertTrue(leftover.is_dir())
        self.assertEqual(installed_plugin_root(self.grok_home), leftover)


class GhFallbackTempCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        self.grok_home = self.root / "grok-home"
        (self.grok_home / "installed-plugins").mkdir(parents=True)
        grok = self.bindir / "grok"
        grok.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        grok.chmod(0o755)
        gh = self.bindir / "gh"
        gh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        gh.chmod(0o755)
        self._old = {
            "PATH": os.environ.get("PATH"),
            "GROK_HOME": os.environ.get("GROK_HOME"),
        }
        os.environ["PATH"] = "{}:/usr/bin:/bin".format(self.bindir)
        os.environ["GROK_HOME"] = str(self.grok_home)

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmpdir.cleanup()

    def test_gh_clone_fallback_removes_temp_dir_after_clone_fails(self):
        created = []
        real_mkdtemp = tempfile.mkdtemp

        def tracking_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(path)
            return path

        source = self.root / "no-plugin-json"
        source.mkdir()
        with mock.patch(
            "notify_me.install.plugin_root",
            return_value=source,
        ):
            with mock.patch(
                "notify_me.install.tempfile.mkdtemp",
                side_effect=tracking_mkdtemp,
            ):
                with self.assertRaises(NotifyMeError):
                    _ensure_plugin()
        self.assertTrue(created, "gh-clone fallback should create a temp dir")
        self.assertFalse(
            Path(created[0]).exists(),
            "fallback temp clone should be deleted after failure",
        )


class InstallBindingRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmpdir.name)
        self._old_env = {
            "GROK_NOTIFY_ME_HOME": os.environ.get("GROK_NOTIFY_ME_HOME"),
            "GROK_HOME": os.environ.get("GROK_HOME"),
        }
        os.environ["GROK_NOTIFY_ME_HOME"] = str(self.home)
        os.environ["GROK_HOME"] = str(self.home / "grok")

    def tearDown(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmpdir.cleanup()

    def test_rejected_install_test_keeps_existing_binding(self):
        old = BarkEndpoint.parse("https://api.day.app/OldWorkingKey1")
        Binding().save(old)
        self.assertEqual(Binding().public_view()["host"], "api.day.app")

        rejected = {
            "ok": False,
            "status": "failed",
            "category": "http",
            "http_status": 400,
            "attempts": 1,
        }
        with mock.patch("notify_me.install._require_tty"), mock.patch(
            "notify_me.install._ensure_plugin",
            return_value=self.home,
        ), mock.patch("notify_me.install._ensure_mcp"), mock.patch(
            "notify_me.install.getpass.getpass",
            return_value="https://bark.example.com/NewTypoKey123",
        ), mock.patch(
            "notify_me.install.Deliverer"
        ) as deliverer_cls:
            deliverer_cls.return_value.test.return_value = rejected
            result = run_install()

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error", {}).get("code"), "test_not_accepted")
        bound = Binding().load()
        self.assertEqual(bound.host, "api.day.app")
        self.assertEqual(bound.key, "OldWorkingKey1")
