import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.tasks as task_flow
from deploy import production_runner


class ProductionRunnerTests(unittest.TestCase):
    def test_validate_tasks_rejects_empty_targets(self):
        with self.assertRaisesRegex(RuntimeError, "没有有效 targets"):
            production_runner._validate_tasks(
                [{"unique_id": "123", "targets": []}], "/tmp/tasks.json"
            )

    def test_validate_tasks_rejects_duplicate_accounts(self):
        tasks = [
            {"unique_id": "123", "targets": ["甲"]},
            {"unique_id": "123", "targets": ["乙"]},
        ]
        with self.assertRaisesRegex(RuntimeError, "重复 unique_id"):
            production_runner._validate_tasks(tasks, "/tmp/tasks.json")

    def test_validate_tasks_normalizes_account_and_targets(self):
        result = production_runner._validate_tasks(
            [{"unique_id": 123, "targets": [" 甲\u3000", 456]}],
            "/tmp/tasks.json",
        )
        self.assertEqual(result[0]["unique_id"], "123")
        self.assertEqual(result[0]["targets"], ["甲", "456"])

    def test_cookie_file_supports_multi_account_mapping(self):
        cookies = {"123": [{"name": "sessionid", "value": "a"}]}
        result = production_runner._cookie_payload_for_task(cookies, "123", "/tmp/cookies.json")
        self.assertEqual(result[0]["value"], "a")

    def test_set_cookie_environment_rejects_one_list_for_multiple_accounts(self):
        tasks = [
            {"unique_id": "123", "targets": ["甲"]},
            {"unique_id": "456", "targets": ["乙"]},
        ]
        with self.assertRaisesRegex(RuntimeError, "单账号数组"):
            production_runner._set_cookie_environment(
                tasks, [{"name": "sessionid", "value": "a"}], "/tmp/cookies.json"
            )

    def test_main_passes_validated_environment_to_application(self):
        tasks = [{"unique_id": "123", "targets": ["甲"]}]
        cookies = [{"name": "sessionid", "value": "a"}]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            tasks_path = directory / "tasks.json"
            cookies_path = directory / "cookies.json"
            tasks_path.write_text(json.dumps(tasks), encoding="utf-8")
            cookies_path.write_text(json.dumps(cookies), encoding="utf-8")

            fake_application = type("Application", (), {"main": staticmethod(lambda: None)})
            with patch.dict(
                os.environ,
                {
                    "HUOHUA_TASKS_FILE": str(tasks_path),
                    "HUOHUA_COOKIES_FILE": str(cookies_path),
                    "MESSAGE_AI_ENABLE": "0",
                },
                clear=False,
            ), patch.dict("sys.modules", {"main": fake_application}):
                production_runner.main()
                self.assertEqual(json.loads(os.environ["TASKS"]), tasks)
                self.assertEqual(json.loads(os.environ["COOKIES_123"]), cookies)
                self.assertEqual(os.environ["REQUIRE_ALL_TARGETS"], "1")

    def test_cookie_snapshot_preserves_other_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"
            original = {
                "123": [{"name": "sessionid", "value": "old-123"}],
                "456": [{"name": "sessionid", "value": "keep-456"}],
            }
            path.write_text(json.dumps(original), encoding="utf-8")

            with patch.dict(
                os.environ,
                {"HUOHUA_COOKIE_PERSIST_FILE": str(path)},
                clear=False,
            ):
                task_flow._persist_cookie_snapshot(
                    [{"name": "sessionid", "value": "new-123"}], "123"
                )

            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["123"][0]["value"], "new-123")
            self.assertEqual(result["456"][0]["value"], "keep-456")

    def test_cookie_snapshot_drops_entries_a_browser_cannot_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"

            with patch.dict(
                os.environ,
                {"HUOHUA_COOKIE_PERSIST_FILE": str(path)},
                clear=False,
            ):
                task_flow._persist_cookie_snapshot(
                    [
                        {"name": "", "value": "douyin.com", "domain": "www.douyin.com"},
                        {"name": "sessionid", "value": "live", "domain": ".douyin.com"},
                    ]
                )

            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([cookie["name"] for cookie in result], ["sessionid"])


if __name__ == "__main__":
    unittest.main()
