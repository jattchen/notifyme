import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(ROOT))

from notify_me.bark import BarkEndpoint, TransportResult  # noqa: E402
from notify_me.binding import Binding  # noqa: E402
from notify_me.deliver import Deliverer, TEST_TITLE, TITLE_MARKS, TOOL_SCHEMA  # noqa: E402
from notify_me.errors import NotifyMeError  # noqa: E402


class FakeTransport:
    def __init__(self, results=None):
        self.payloads = []
        self.calls = 0
        self.results = list(results or [TransportResult(True, False, "accepted", 200, 1)])

    def send_with_retry(self, endpoint, payload, sleep=None, max_attempts=2):
        assert payload.get("device_key") == endpoint.key
        assert payload.get("body")
        self.calls += 1
        self.payloads.append({key: value for key, value in payload.items() if key != "device_key"})
        index = min(self.calls - 1, len(self.results) - 1)
        return self.results[index]


class DeliverTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["GROK_NOTIFY_ME_HOME"] = self.tmpdir.name
        os.environ["GROK_WORKSPACE_ROOT"] = str(Path.home())
        self.binding = Binding(Path(self.tmpdir.name))
        self.endpoint = BarkEndpoint.parse("https://api.day.app/Abcdefgh1234")
        self.binding.save(self.endpoint)
        self.transport = FakeTransport()
        self.deliverer = Deliverer(binding=self.binding, transport=self.transport)

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("GROK_NOTIFY_ME_HOME", None)
        os.environ.pop("GROK_WORKSPACE_ROOT", None)

    def test_schema_is_minimal(self):
        props = TOOL_SCHEMA["inputSchema"]["properties"]
        self.assertEqual(set(props), {"op", "condition", "item_id", "state", "message", "dry_run"})
        self.assertEqual(TOOL_SCHEMA["inputSchema"]["properties"]["op"]["enum"], ["send", "test"])
        self.assertEqual(
            TOOL_SCHEMA["inputSchema"]["properties"]["condition"]["enum"],
            ["answer", "auth", "action", "severe-risk", "done"],
        )
        dumped = json.dumps(TOOL_SCHEMA)
        self.assertNotIn("bark_url", dumped)
        self.assertNotIn("subscribe", dumped)
        self.assertNotIn('"title"', dumped)
        self.assertNotIn("priority", dumped)

    def test_send_answer_accepted_then_deduplicated(self):
        first = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(first["status"], "accepted")
        self.assertTrue(first["ok"])
        self.assertEqual(self.transport.payloads[0]["title"], TITLE_MARKS["answer"])
        self.assertEqual(self.transport.payloads[0]["body"], "请提供 API token")
        self.assertEqual(self.transport.payloads[0]["level"], "timeSensitive")
        self.assertNotIn("device_key", first)
        dumped = json.dumps(first)
        self.assertNotIn("Abcdefgh1234", dumped)
        second = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(second["status"], "deduplicated")
        self.assertTrue(second["ok"])
        self.assertEqual(self.transport.calls, 1)

    def test_same_triple_different_workspaces_both_accepted(self):
        first_repo = Path(self.tmpdir.name) / "alpha-proj"
        second_repo = Path(self.tmpdir.name) / "beta-proj"
        (first_repo / ".git").mkdir(parents=True)
        (second_repo / ".git").mkdir(parents=True)
        first = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait",
                "state": "open",
                "message": "alpha 在等回答",
            },
            {"GROK_WORKSPACE_ROOT": str(first_repo)},
        )
        second = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait",
                "state": "open",
                "message": "beta 在等回答",
            },
            {"GROK_WORKSPACE_ROOT": str(second_repo)},
        )
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(self.transport.calls, 2)
        self.assertEqual(
            self.transport.payloads[0]["title"],
            "{} · {}".format(TITLE_MARKS["answer"], "alpha-proj"),
        )
        self.assertEqual(
            self.transport.payloads[1]["title"],
            "{} · {}".format(TITLE_MARKS["answer"], "beta-proj"),
        )
        self.assertEqual(self.transport.payloads[0]["group"], "alpha-proj")
        self.assertEqual(self.transport.payloads[1]["group"], "beta-proj")
        self.assertEqual(self.transport.payloads[0]["body"], "alpha 在等回答")
        self.assertEqual(self.transport.payloads[1]["body"], "beta 在等回答")
        retry = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait",
                "state": "open",
                "message": "alpha 再次等待",
            },
            {"GROK_WORKSPACE_ROOT": str(first_repo)},
        )
        self.assertEqual(retry["status"], "deduplicated")
        self.assertEqual(self.transport.calls, 2)

    def test_legacy_triple_does_not_suppress_named_project(self):
        accepted_path = Path(self.tmpdir.name) / "accepted.json"
        accepted_path.write_text(
            json.dumps([["wait", "open", "answer"]]),
            encoding="utf-8",
        )
        leftover = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait",
                "state": "open",
                "message": "home 仍应去重",
            }
        )
        self.assertEqual(leftover["status"], "deduplicated")
        self.assertEqual(self.transport.calls, 0)
        repo = Path(self.tmpdir.name) / "other-proj"
        (repo / ".git").mkdir(parents=True)
        named = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait",
                "state": "open",
                "message": "other 应再推一次",
            },
            {"GROK_WORKSPACE_ROOT": str(repo)},
        )
        self.assertEqual(named["status"], "accepted")
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual(
            self.transport.payloads[0]["title"],
            "{} · {}".format(TITLE_MARKS["answer"], "other-proj"),
        )
        self.assertEqual(self.transport.payloads[0]["group"], "other-proj")

    def test_same_basename_different_roots_both_accepted(self):
        first_repo = Path(self.tmpdir.name) / "one" / "shared-name"
        second_repo = Path(self.tmpdir.name) / "two" / "shared-name"
        (first_repo / ".git").mkdir(parents=True)
        (second_repo / ".git").mkdir(parents=True)
        first = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait",
                "state": "open",
                "message": "one 在等",
            },
            {"GROK_WORKSPACE_ROOT": str(first_repo)},
        )
        second = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait",
                "state": "open",
                "message": "two 在等",
            },
            {"GROK_WORKSPACE_ROOT": str(second_repo)},
        )
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(self.transport.calls, 2)
        self.assertEqual(self.transport.payloads[0]["group"], "shared-name")
        self.assertEqual(self.transport.payloads[1]["group"], "shared-name")
        self.assertEqual(self.transport.payloads[0]["body"], "one 在等")
        self.assertEqual(self.transport.payloads[1]["body"], "two 在等")

    def test_send_accepted_then_deduplicated_across_deliverers(self):
        other = Deliverer(
            binding=Binding(Path(self.tmpdir.name)),
            transport=self.transport,
        )
        first = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(first["status"], "accepted")
        second = other.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(second["status"], "deduplicated")
        self.assertEqual(self.transport.calls, 1)

    def test_overlapping_sends_same_key_post_once(self):
        started = threading.Event()
        release = threading.Event()
        count_lock = threading.Lock()

        class DelayedTransport:
            def __init__(self):
                self.payloads = []
                self.calls = 0

            def send_with_retry(self, endpoint, payload, sleep=None, max_attempts=2):
                assert payload.get("device_key") == endpoint.key
                assert payload.get("body")
                with count_lock:
                    self.calls += 1
                    self.payloads.append(
                        {key: value for key, value in payload.items() if key != "device_key"}
                    )
                started.set()
                if not release.wait(timeout=5):
                    raise AssertionError("transport gate was not released")
                return TransportResult(True, False, "accepted", 200, 1)

        transport = DelayedTransport()
        first = Deliverer(
            binding=Binding(Path(self.tmpdir.name)),
            transport=transport,
        )
        second = Deliverer(
            binding=Binding(Path(self.tmpdir.name)),
            transport=transport,
        )
        params = {
            "condition": "answer",
            "item_id": "wait-token",
            "state": "missing",
            "message": "请提供 API token",
        }
        results = [None, None]
        errors = [None, None]

        def run(index, deliverer):
            try:
                results[index] = deliverer.send(params)
            except Exception as exc:
                errors[index] = exc

        worker = threading.Thread(target=run, args=(0, first))
        overlap = threading.Thread(target=run, args=(1, second))
        worker.start()
        self.assertTrue(started.wait(timeout=5))
        overlap.start()
        time.sleep(0.3)
        release.set()
        worker.join(timeout=5)
        overlap.join(timeout=5)
        self.assertIsNone(errors[0])
        self.assertIsNone(errors[1])
        self.assertFalse(worker.is_alive())
        self.assertFalse(overlap.is_alive())
        self.assertEqual(transport.calls, 1)
        statuses = sorted(result["status"] for result in results)
        self.assertEqual(statuses, ["accepted", "deduplicated"])

    def test_accepted_persist_survives_replace_failure_across_deliverers(self):
        other = Deliverer(
            binding=Binding(Path(self.tmpdir.name)),
            transport=self.transport,
        )
        with mock.patch(
            "notify_me.deliver.os.replace", side_effect=OSError("replace failed")
        ):
            first = self.deliverer.send(
                {
                    "condition": "answer",
                    "item_id": "wait-token",
                    "state": "missing",
                    "message": "请提供 API token",
                }
            )
            self.assertEqual(first["status"], "accepted")
            self.assertTrue(first["ok"])
            second = other.send(
                {
                    "condition": "answer",
                    "item_id": "wait-token",
                    "state": "missing",
                    "message": "请提供 API token",
                }
            )
        self.assertEqual(second["status"], "deduplicated")
        self.assertEqual(self.transport.calls, 1)

    def test_persist_failure_after_accept_is_visible_on_first_return(self):
        with mock.patch(
            "notify_me.deliver.os.replace", side_effect=OSError("replace failed")
        ):
            first = self.deliverer.send(
                {
                    "condition": "answer",
                    "item_id": "wait-token",
                    "state": "missing",
                    "message": "请提供 API token",
                }
            )
        self.assertTrue(first["ok"])
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(first.get("persist"), "degraded")
        self.assertEqual(self.transport.calls, 1)

    def test_corrupt_accepted_json_does_not_repost_across_deliverers(self):
        first = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(first["status"], "accepted")
        self.assertTrue(first["ok"])
        accepted_path = Path(self.tmpdir.name) / "accepted.json"
        accepted_path.write_text("{not-valid-json", encoding="utf-8")
        other = Deliverer(
            binding=Binding(Path(self.tmpdir.name)),
            transport=self.transport,
        )
        try:
            second = other.send(
                {
                    "condition": "answer",
                    "item_id": "wait-token",
                    "state": "missing",
                    "message": "请提供 API token",
                }
            )
        except NotifyMeError:
            self.assertEqual(self.transport.calls, 1)
            return
        self.assertNotEqual(second.get("status"), "accepted")
        self.assertEqual(self.transport.calls, 1)

    def test_mixed_shape_accepted_list_does_not_repost(self):
        accepted_path = Path(self.tmpdir.name) / "accepted.json"
        accepted_path.write_text(
            json.dumps(
                [
                    {"not": "a-tuple"},
                    ["", "wait-token", "missing", "answer"],
                ]
            ),
            encoding="utf-8",
        )
        other = Deliverer(
            binding=Binding(Path(self.tmpdir.name)),
            transport=self.transport,
        )
        try:
            result = other.send(
                {
                    "condition": "answer",
                    "item_id": "wait-token",
                    "state": "missing",
                    "message": "请提供 API token",
                }
            )
        except NotifyMeError as exc:
            self.assertEqual(exc.code, "invalid_accepted")
            self.assertEqual(self.transport.calls, 0)
            return
        self.assertNotEqual(result.get("status"), "accepted")
        self.assertEqual(self.transport.calls, 0)

    def test_answer_then_done_same_incident_both_accepted(self):
        first = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "task-1",
                "state": "open",
                "message": "请选择下一步",
            }
        )
        second = self.deliverer.send(
            {
                "condition": "done",
                "item_id": "task-1",
                "state": "open",
                "message": "任务已完成",
            }
        )
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(self.transport.calls, 2)
        self.assertEqual(self.transport.payloads[0]["title"], TITLE_MARKS["answer"])
        self.assertEqual(self.transport.payloads[1]["title"], TITLE_MARKS["done"])
        retry = self.deliverer.send(
            {
                "condition": "done",
                "item_id": "task-1",
                "state": "open",
                "message": "任务已完成",
            }
        )
        self.assertEqual(retry["status"], "deduplicated")
        self.assertEqual(self.transport.calls, 2)

    def test_failed_send_can_retry(self):
        self.transport.results = [
            TransportResult(False, True, "network_error", None, 2),
            TransportResult(True, False, "accepted", 200, 1),
        ]
        first = self.deliverer.send(
            {
                "condition": "severe-risk",
                "item_id": "drop-prod",
                "state": "confirm",
                "message": "确认后将清空生产数据",
            }
        )
        self.assertEqual(first["status"], "failed")
        self.assertFalse(first["ok"])
        self.assertEqual(self.transport.payloads[0]["title"], TITLE_MARKS["severe-risk"])
        self.assertEqual(self.transport.payloads[0]["level"], "critical")
        second = self.deliverer.send(
            {
                "condition": "severe-risk",
                "item_id": "drop-prod",
                "state": "confirm",
                "message": "确认后将清空生产数据",
            }
        )
        self.assertEqual(second["status"], "accepted")
        self.assertTrue(second["ok"])
        self.assertEqual(self.transport.calls, 2)

    def test_failed_send_and_test_are_not_ok(self):
        self.transport.results = [
            TransportResult(False, True, "network_error", None, 2),
            TransportResult(False, False, "rejected", 400, 1),
        ]
        sent = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(sent["status"], "failed")
        self.assertFalse(sent["ok"])
        tested = self.deliverer.test({"message": "测试"})
        self.assertEqual(tested["status"], "failed")
        self.assertFalse(tested["ok"])

    def test_dry_run_does_not_post_or_dedup(self):
        result = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
                "dry_run": True,
            }
        )
        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(result["ok"])
        self.assertEqual(self.transport.calls, 0)
        accepted = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(accepted["status"], "accepted")

    def test_test_posts_and_skips_send_dedup(self):
        send = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(send["status"], "accepted")
        tested = self.deliverer.test({"message": "测试"})
        self.assertEqual(tested["status"], "accepted")
        self.assertEqual(self.transport.payloads[1]["title"], TEST_TITLE)
        self.assertEqual(self.transport.calls, 2)
        again = self.deliverer.test({})
        self.assertEqual(again["status"], "accepted")
        self.assertEqual(self.transport.calls, 3)

    def test_test_dry_run_does_not_post(self):
        result = self.deliverer.test({"dry_run": True})
        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(result["ok"])
        self.assertEqual(result["title"], TEST_TITLE)
        self.assertEqual(self.transport.calls, 0)

    def test_string_false_dry_run_posts_for_send_and_test(self):
        sent = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
                "dry_run": "false",
            }
        )
        self.assertNotEqual(sent["status"], "dry_run")
        self.assertEqual(sent["status"], "accepted")
        self.assertTrue(sent["ok"])
        self.assertEqual(self.transport.calls, 1)
        tested = self.deliverer.test({"dry_run": "false", "message": "测试"})
        self.assertNotEqual(tested["status"], "dry_run")
        self.assertEqual(tested["status"], "accepted")
        self.assertTrue(tested["ok"])
        self.assertEqual(self.transport.calls, 2)

    def test_dry_run_falsey_values_post_and_unknown_is_rejected(self):
        send_params = {
            "condition": "answer",
            "item_id": "wait-token",
            "state": "missing",
            "message": "请提供 API token",
        }
        for index, value in enumerate(("0", 0, False, None)):
            params = dict(send_params)
            params["item_id"] = "wait-token-{}".format(index)
            params["dry_run"] = value
            result = self.deliverer.send(params)
            self.assertNotEqual(result["status"], "dry_run", value)
            self.assertEqual(result["status"], "accepted", value)
        missing = self.deliverer.send(dict(send_params, item_id="wait-token-missing"))
        self.assertEqual(missing["status"], "accepted")
        self.assertEqual(self.transport.calls, 5)
        for value in ("true", "TRUE", True):
            result = self.deliverer.send(dict(send_params, item_id="dry-{}".format(value), dry_run=value))
            self.assertEqual(result["status"], "dry_run", value)
            self.assertEqual(self.transport.calls, 5)
        string_true_test = self.deliverer.test({"dry_run": "true"})
        self.assertEqual(string_true_test["status"], "dry_run")
        self.assertEqual(self.transport.calls, 5)
        with self.assertRaises(NotifyMeError) as caught:
            self.deliverer.send(dict(send_params, dry_run="yes"))
        self.assertEqual(caught.exception.code, "invalid_arguments")
        with self.assertRaises(NotifyMeError) as caught:
            self.deliverer.test({"dry_run": "yes"})
        self.assertEqual(caught.exception.code, "invalid_arguments")
        self.assertEqual(self.transport.calls, 5)

    def test_unknown_op_and_condition(self):
        with self.assertRaises(NotifyMeError) as caught:
            self.deliverer.dispatch({"op": "status"})
        self.assertEqual(caught.exception.code, "unsupported_command")
        with self.assertRaises(NotifyMeError) as caught:
            self.deliverer.send(
                {
                    "condition": "subscription",
                    "item_id": "a",
                    "state": "b",
                    "message": "x",
                }
            )
        self.assertEqual(caught.exception.code, "unsupported_condition")
        with self.assertRaises(NotifyMeError) as caught:
            self.deliverer.send(
                {
                    "condition": "blocking",
                    "item_id": "a",
                    "state": "b",
                    "message": "x",
                }
            )
        self.assertEqual(caught.exception.code, "unsupported_condition")

    def test_git_project_goes_in_title_not_body(self):
        repo = Path(self.tmpdir.name) / "demo-proj"
        (repo / ".git").mkdir(parents=True)
        result = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
                "dry_run": True,
            },
            {"GROK_WORKSPACE_ROOT": str(repo)},
        )
        self.assertEqual(result["title"], "{} · {}".format(TITLE_MARKS["answer"], "demo-proj"))
        self.assertEqual(result["body"], "请提供 API token")
        self.assertNotIn("demo-proj", result["body"])

    def test_git_project_sets_bark_group(self):
        repo = Path(self.tmpdir.name) / "demo-proj"
        (repo / ".git").mkdir(parents=True)
        result = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            },
            {"GROK_WORKSPACE_ROOT": str(repo)},
        )
        self.assertEqual(result["status"], "accepted")
        payload = self.transport.payloads[0]
        self.assertEqual(payload["group"], "demo-proj")
        self.assertEqual(payload["title"], "{} · {}".format(TITLE_MARKS["answer"], "demo-proj"))
        self.assertEqual(payload["body"], "请提供 API token")

    def test_nongit_workspace_uses_directory_name(self):
        workspace = Path(self.tmpdir.name) / "loopx"
        workspace.mkdir()
        result = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            },
            {"GROK_WORKSPACE_ROOT": str(workspace)},
        )
        self.assertEqual(result["status"], "accepted")
        payload = self.transport.payloads[0]
        self.assertEqual(payload["group"], "loopx")
        self.assertEqual(
            payload["title"], "{} · {}".format(TITLE_MARKS["answer"], "loopx")
        )
        self.assertEqual(payload["body"], "请提供 API token")

    def test_home_directory_is_not_used_as_project_name(self):
        result = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            },
            {"GROK_WORKSPACE_ROOT": str(Path.home())},
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.transport.payloads[0]["group"], "Grok")
        self.assertEqual(self.transport.payloads[0]["title"], TITLE_MARKS["answer"])

    def test_nongit_cwd_uses_directory_name(self):
        workspace = Path(self.tmpdir.name) / "loopx"
        workspace.mkdir()
        with mock.patch("notify_me.deliver.os.getcwd", return_value=str(workspace)):
            result = self.deliverer.send(
                {
                    "condition": "answer",
                    "item_id": "wait-token",
                    "state": "missing",
                    "message": "请提供 API token",
                },
                {"PWD": str(Path.home())},
            )
        self.assertEqual(result["status"], "accepted")
        payload = self.transport.payloads[0]
        self.assertEqual(payload["group"], "loopx")
        self.assertEqual(
            payload["title"], "{} · {}".format(TITLE_MARKS["answer"], "loopx")
        )

    def test_git_root_preferred_over_workspace_basename(self):
        repo = Path(self.tmpdir.name) / "demo-proj"
        nested = repo / "src"
        (repo / ".git").mkdir(parents=True)
        nested.mkdir()
        result = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            },
            {"GROK_WORKSPACE_ROOT": str(nested)},
        )
        self.assertEqual(result["status"], "accepted")
        payload = self.transport.payloads[0]
        self.assertEqual(payload["group"], "demo-proj")
        self.assertEqual(
            payload["title"], "{} · {}".format(TITLE_MARKS["answer"], "demo-proj")
        )

    def test_send_resolves_project_name_once(self):
        repo = Path(self.tmpdir.name) / "demo-proj"
        (repo / ".git").mkdir(parents=True)
        env = {"GROK_WORKSPACE_ROOT": str(repo)}
        with mock.patch(
            "notify_me.deliver.project_name", return_value="demo-proj"
        ) as resolved:
            result = self.deliverer.send(
                {
                    "condition": "answer",
                    "item_id": "wait-token",
                    "state": "missing",
                    "message": "请提供 API token",
                },
                env,
            )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(resolved.call_count, 1)
        resolved.assert_called_once_with(env)
        payload = self.transport.payloads[0]
        self.assertEqual(payload["group"], "demo-proj")
        self.assertEqual(
            payload["title"], "{} · {}".format(TITLE_MARKS["answer"], "demo-proj")
        )

    def test_same_project_tasks_share_bark_group(self):
        repo = Path(self.tmpdir.name) / "demo-proj"
        (repo / ".git").mkdir(parents=True)
        env = {"GROK_WORKSPACE_ROOT": str(repo)}
        first = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "task-a",
                "state": "waiting",
                "message": "任务 A 需要选择",
            },
            env,
        )
        second = self.deliverer.send(
            {
                "condition": "done",
                "item_id": "task-b",
                "state": "finished",
                "message": "任务 B 已完成",
            },
            env,
        )
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(self.transport.payloads[0]["group"], "demo-proj")
        self.assertEqual(self.transport.payloads[1]["group"], "demo-proj")

    def test_test_notification_keeps_grok_group(self):
        repo = Path(self.tmpdir.name) / "demo-proj"
        (repo / ".git").mkdir(parents=True)
        result = self.deliverer.test(
            {"message": "测试"},
            {"GROK_WORKSPACE_ROOT": str(repo)},
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.transport.payloads[0]["group"], "Grok")
        self.assertEqual(self.transport.payloads[0]["title"], TEST_TITLE)

    def test_stale_home_pwd_does_not_hide_git_cwd(self):
        repo = Path(self.tmpdir.name) / "demo-proj"
        (repo / ".git").mkdir(parents=True)
        with mock.patch("notify_me.deliver.os.getcwd", return_value=str(repo)):
            result = self.deliverer.send(
                {
                    "condition": "answer",
                    "item_id": "wait-token",
                    "state": "missing",
                    "message": "请提供 API token",
                    "dry_run": True,
                },
                {"PWD": str(Path.home())},
            )
        self.assertEqual(result["title"], "{} · {}".format(TITLE_MARKS["answer"], "demo-proj"))
        self.assertEqual(result["body"], "请提供 API token")

    def test_titles_and_effects_by_condition(self):
        expected_levels = {
            "answer": "timeSensitive",
            "auth": "timeSensitive",
            "action": "timeSensitive",
            "severe-risk": "critical",
            "done": "active",
        }
        for condition, mark in TITLE_MARKS.items():
            result = self.deliverer.send(
                {
                    "condition": condition,
                    "item_id": "item-{}".format(condition),
                    "state": "open",
                    "message": "正文",
                    "dry_run": True,
                }
            )
            self.assertEqual(result["title"], mark)
            self.assertEqual(result["body"], "正文")
            posted = self.deliverer.send(
                {
                    "condition": condition,
                    "item_id": "post-{}".format(condition),
                    "state": "open",
                    "message": "正文",
                }
            )
            self.assertEqual(posted["status"], "accepted")
            payload = self.transport.payloads[-1]
            self.assertEqual(payload["title"], mark)
            self.assertEqual(payload["level"], expected_levels[condition])

    def test_send_rejects_world_writable_state_dir(self):
        home = Path(self.tmpdir.name)
        home.chmod(0o777)
        self.assertEqual(stat.S_IMODE((home / "binding.json").stat().st_mode), 0o600)
        with self.assertRaises(NotifyMeError) as caught:
            self.deliverer.send(
                {
                    "condition": "answer",
                    "item_id": "wait-token",
                    "state": "missing",
                    "message": "请提供 API token",
                }
            )
        self.assertEqual(caught.exception.code, "insecure_binding")
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o777)

    def test_planted_accepted_leftover_does_not_suppress_send(self):
        leftover = Path(self.tmpdir.name) / ".accepted.planted"
        leftover.write_text(
            json.dumps([["", "wait-token", "missing", "answer"]]),
            encoding="utf-8",
        )
        leftover.chmod(0o644)
        result = self.deliverer.send(
            {
                "condition": "answer",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["ok"])
        self.assertEqual(self.transport.calls, 1)
