import unittest
from unittest.mock import Mock, patch

import core.tasks as tasks


class ListPreloadTests(unittest.TestCase):
    def test_preload_collects_names_and_restores_scroll_without_selecting_a_chat(self):
        page = Mock()
        handle = object()
        page.locator.return_value.element_handle.return_value = handle
        with patch.object(tasks, "collect_friend_titles", return_value=["甲", "乙", "123"]) as collect, \
             patch.object(tasks, "_submit_chat_message") as send:
            self.assertEqual(tasks._preload_friend_list(page, "账号"), ["甲", "乙", "123"])
        collect.assert_called_once_with(page, "账号")
        page.locator.return_value.element_handle.assert_called_once_with(timeout=tasks.FALLBACK_ELEMENT_TIMEOUT_MS)
        page.evaluate.assert_called_once_with("element => { element.scrollTop = 0; }", handle)
        page.locator.return_value.click.assert_not_called()
        send.assert_not_called()

    def test_missing_scroll_container_does_not_discard_collected_identity_data(self):
        page = Mock()
        page.locator.return_value.element_handle.side_effect = RuntimeError("detached list")
        with patch.object(tasks, "collect_friend_titles", return_value=["甲"]):
            self.assertEqual(tasks._preload_friend_list(page, "账号"), ["甲"])
        page.evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
