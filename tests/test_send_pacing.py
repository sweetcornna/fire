import unittest
from unittest.mock import Mock, patch

import core.tasks as tasks


class SendPacingTests(unittest.TestCase):
    def test_messages_are_spaced_by_a_random_pause(self):
        with patch.dict(tasks.config, {"sendIntervalMinSeconds": 20, "sendIntervalMaxSeconds": 60}), \
             patch.object(tasks.time, "sleep") as sleep:
            pause = tasks._pace_next_delivery("账号")

        self.assertTrue(20 <= pause <= 60, pause)
        sleep.assert_called_once()
        self.assertEqual(sleep.call_args.args[0], pause)

    def test_pacing_can_be_switched_off(self):
        with patch.dict(tasks.config, {"sendIntervalMinSeconds": 0, "sendIntervalMaxSeconds": 0}), \
             patch.object(tasks.time, "sleep") as sleep:
            self.assertEqual(tasks._pace_next_delivery("账号"), 0)

        sleep.assert_not_called()

    def test_an_inverted_range_never_shortens_below_the_minimum(self):
        with patch.dict(tasks.config, {"sendIntervalMinSeconds": 40, "sendIntervalMaxSeconds": 10}), \
             patch.object(tasks.time, "sleep"):
            self.assertEqual(tasks._pace_next_delivery("账号"), 40)


class SendQuotaTests(unittest.TestCase):
    def setUp(self):
        self.stack = patch.multiple(
            tasks,
            wait_for_chat_ready=Mock(return_value=True),
            _preload_friend_list=Mock(return_value=[]),
            _persist_cookie_snapshot=Mock(),
            probe_placeholder_identities=Mock(return_value={}),
            build_message=Mock(return_value="消息"),
            _pace_next_delivery=Mock(return_value=0),
        )
        self.stack.start()
        self.addCleanup(self.stack.stop)

    def _run(self, targets, quota, delivered):
        browser = Mock()
        browser.new_context.return_value.new_page.return_value = Mock()
        sent = []

        def send(page, account, target, message=None, delivery_key=None):
            if target in delivered:
                sent.append(target)
                return target
            return None

        with patch.dict(tasks.config, {"maxSendsPerRun": quota}), patch.object(
            tasks, "scroll_and_select_user", return_value=iter(targets)
        ), patch.object(tasks, "_send_target_with_retries", side_effect=send), patch.object(
            tasks, "_target_delivery_statuses", return_value={}
        ), patch.object(tasks, "_delivery_state_key", side_effect=lambda value: value):
            error = None
            try:
                tasks.do_user_task(browser, "显示名", [], targets, "account")
            except RuntimeError as raised:
                error = raised
        return sent, error

    def test_a_run_stops_at_its_quota_and_leaves_the_rest_for_later(self):
        targets = ["甲", "乙", "丙", "丁"]

        sent, error = self._run(targets, quota=2, delivered=set(targets))

        self.assertEqual(sent, ["甲", "乙"])
        # Stopping on quota is planned, so the run is not reported as failed.
        self.assertIsNone(error)

    def test_targets_that_could_not_be_sent_still_fail_the_run(self):
        targets = ["甲", "乙"]

        sent, error = self._run(targets, quota=0, delivered={"甲"})

        self.assertEqual(sent, ["甲"])
        self.assertIsNotNone(error)
        self.assertIn("1/2", str(error))


if __name__ == "__main__":
    unittest.main()
