import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import core.tasks as tasks


class ChatReadinessTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.clock = 0.0
        self.page = Mock()
        self.page.locator.return_value.count.return_value = 0
        self.stack.enter_context(patch.object(tasks.time, "monotonic", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(tasks.time, "sleep", side_effect=self.advance))
        self.stack.enter_context(patch.object(tasks, "_dismiss_login_prompt", return_value=False))
        self.find = self.stack.enter_context(patch.object(tasks, "find_search_input", return_value=None))

    def advance(self, seconds):
        self.clock += seconds

    def test_transient_login_prompt_can_hydrate_into_a_ready_chat(self):
        self.find.return_value = object()
        with patch.object(tasks, "_logged_out", side_effect=[True, True, False]):
            self.assertTrue(tasks.wait_for_chat_ready(self.page, "账号", timeout=1500))
        self.assertEqual(self.clock, 1.0)
        self.find.assert_called_once_with(self.page)

    def test_persistent_login_is_rejected_after_the_original_deadline(self):
        self.find.return_value = object()
        with patch.object(tasks, "_logged_out", return_value=True), patch.object(
            tasks, "_page_state", return_value={"body": "扫码登录"}
        ):
            with self.assertRaisesRegex(RuntimeError, "1000ms 后仍显示登录页"):
                tasks.wait_for_chat_ready(self.page, "账号", timeout=1000)
        self.assertEqual(self.clock, 1.0)
        self.find.assert_not_called()
        self.page.locator.assert_not_called()

    def test_loading_failure_without_a_login_prompt_is_not_reported_as_expired_credentials(self):
        with patch.object(tasks, "_logged_out", return_value=False), patch.object(
            tasks, "_page_state", return_value={"body": "加载中"}
        ):
            self.assertFalse(tasks.wait_for_chat_ready(self.page, "账号", timeout=1000))
        self.assertEqual(self.clock, 1.0)

    def test_ready_chat_does_not_wait_for_the_full_timeout(self):
        self.find.return_value = object()
        with patch.object(tasks, "_logged_out", return_value=False):
            self.assertTrue(tasks.wait_for_chat_ready(self.page, "账号", timeout=30000))
        self.assertEqual(self.clock, 0.0)


if __name__ == "__main__":
    unittest.main()
