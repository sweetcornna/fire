import json
import os
import unittest
from unittest.mock import patch

import utils.config as config


class UserDataConfigTests(unittest.TestCase):
    def setUp(self):
        self.original_user_data = config.userData
        config.userData = None

    def tearDown(self):
        config.userData = self.original_user_data

    def test_strict_loading_rejects_a_missing_account_cookie(self):
        tasks = [
            {"unique_id": "1", "targets": ["甲"]},
            {"unique_id": "2", "targets": ["乙"]},
        ]
        with patch.dict(
            os.environ,
            {
                "TASKS": json.dumps(tasks),
                "COOKIES_1": json.dumps([{"name": "sessionid", "value": "a"}]),
                "COOKIES_2": "",
                "REQUIRE_ALL_TARGETS": "1",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "拒绝只执行部分续火任务"):
                config.get_userData()

    def test_targets_are_normalized_before_they_reach_the_runner(self):
        tasks = [{"unique_id": "1", "targets": [" 甲\u3000", 123]}]
        with patch.dict(
            os.environ,
            {
                "TASKS": json.dumps(tasks, ensure_ascii=False),
                "COOKIES_1": json.dumps([{"name": "sessionid", "value": "a"}]),
                "REQUIRE_ALL_TARGETS": "1",
            },
            clear=False,
        ):
            result = config.get_userData()

        self.assertEqual(result[0]["targets"], ["甲", "123"])


if __name__ == "__main__":
    unittest.main()
