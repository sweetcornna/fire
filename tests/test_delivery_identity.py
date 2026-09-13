import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import core.tasks as tasks


class DeliveryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "delivery.json"
        self.stack.enter_context(patch.dict(os.environ, {"DELIVERY_STATE_FILE": str(self.path)}))
        self.stack.enter_context(patch.object(tasks, "userIDDict", {}))
        self.stack.enter_context(patch.object(tasks, "_unconfirmed_submissions", set()))

    def load_users(self, *users):
        response = Mock(url="https://www.douyin.com/aweme/v1/web/im/user/info", status=200)
        response.json.return_value = {"data": list(users)}
        tasks.handle_response(response)

    def friend(self, nickname="旧昵称"):
        return {"short_id": "123", "unique_id": "friend-123", "sec_uid": "sec-123",
                "nickname": nickname, "remark_name": nickname}

    def write_accounts(self, accounts):
        self.path.write_text(json.dumps({"days": {tasks.date.today().isoformat(): accounts}}),
                             encoding="utf-8")

    def test_pending_id_blocks_submission_through_every_confirmed_alias(self):
        self.load_users(self.friend())
        tasks._mark_target_pending_today("account", "friend-123", "消息")
        targets = ["friend-123", "123", "sec-123", "旧昵称"]
        self.assertEqual(tasks._target_delivery_statuses("account", targets),
                         dict.fromkeys(targets, "pending"))
        with patch.object(tasks, "_wait_for_chat_input", side_effect=AssertionError("new send")):
            for target in targets:
                with self.subTest(target=target), self.assertRaises(tasks.DeliveryUncertainError):
                    tasks._submit_chat_message(Mock(), "显示名", target, "消息", "account")

    def test_submitted_id_skips_alias_before_accessing_the_editor(self):
        self.load_users(self.friend())
        tasks._mark_target_pending_today("account", "friend-123", "消息")
        tasks._mark_target_sent_today("account", "friend-123")
        tasks._unconfirmed_submissions.clear()
        with patch.object(tasks, "_wait_for_chat_input", side_effect=AssertionError("new send")):
            self.assertEqual(tasks._submit_chat_message(Mock(), "显示名", "旧昵称", "消息", "account"),
                             "旧昵称")
        self.assertEqual(tasks._completed_targets_for_today("account", ["旧昵称", "sec-123"]),
                         {"旧昵称", "sec-123"})

    def test_persisted_identity_survives_restart_and_replacement_of_api_names(self):
        self.load_users(self.friend())
        tasks._mark_target_pending_today("account", "旧昵称", "消息")
        tasks._unconfirmed_submissions.clear()
        tasks.userIDDict.clear()
        self.load_users(self.friend("新昵称"))
        self.assertEqual(tasks._target_delivery_statuses("account", ["新昵称", "friend-123"]),
                         {"新昵称": "pending", "friend-123": "pending"})
        tasks._mark_target_sent_today("account", "friend-123")
        tasks._unconfirmed_submissions.clear()
        self.assertEqual(tasks._target_delivery_statuses("account", ["新昵称", "123"]),
                         {"新昵称": "submitted", "123": "submitted"})

    def test_persisted_stable_id_can_be_checked_before_api_hydration(self):
        self.load_users(self.friend())
        tasks._mark_target_pending_today("account", "旧昵称", "消息")
        tasks._unconfirmed_submissions.clear()
        tasks.userIDDict.clear()
        self.assertEqual(tasks._target_delivery_statuses("account", ["friend-123", "sec-123"]),
                         {"friend-123": "pending", "sec-123": "pending"})

    def test_pending_wins_across_legacy_alias_entries_and_account_aliases(self):
        self.load_users(self.friend())
        for pending_account in ("account", "显示名"):
            with self.subTest(pending_account=pending_account):
                accounts = {
                    "account": {"friend-123": {"status": "submitted"}},
                    "显示名": {"sec-123": {"sent_at": "2026-09-13T01:00:00Z"}},
                }
                accounts[pending_account]["旧昵称"] = {"status": "pending"}
                self.write_accounts(accounts)
                targets = ["旧昵称", "friend-123", "123", "sec-123"]
                self.assertEqual(tasks._target_delivery_statuses("account", targets, aliases=("显示名",)),
                                 dict.fromkeys(targets, "pending"))
                self.assertEqual(tasks._completed_targets_for_today("account", targets, aliases=("显示名",)),
                                 set())

    def test_legacy_sent_at_alias_remains_complete(self):
        self.load_users(self.friend())
        self.write_accounts({"account": {"旧昵称": {"sent_at": "2026-09-13T01:00:00Z"}}})
        self.assertEqual(tasks._target_delivery_statuses("account", ["friend-123", "sec-123"]),
                         {"friend-123": "sent", "sec-123": "sent"})

    def test_shared_nickname_does_not_transfer_state_between_users(self):
        self.load_users(self.friend("同名"),
                        {"unique_id": "other-id", "sec_uid": "other-sec", "nickname": "同名"})
        tasks._mark_target_pending_today("account", "friend-123", "消息")
        self.assertEqual(tasks._target_delivery_statuses("account", ["other-id", "other-sec", "同名"]), {})
        self.assertEqual(tasks._target_delivery_statuses("account", ["123"]), {"123": "pending"})

    def test_ambiguous_legacy_nickname_cannot_complete_either_user(self):
        self.load_users(self.friend("同名"), {"unique_id": "other-id", "nickname": "同名"})
        self.write_accounts({"account": {"同名": {"sent_at": "2026-09-13T01:00:00Z"}}})
        self.assertEqual(tasks._target_delivery_statuses("account", ["friend-123", "other-id"]), {})

    def test_reused_nickname_does_not_overwrite_the_original_identity(self):
        self.load_users(self.friend())
        tasks._mark_target_pending_today("account", "旧昵称", "第一条")
        tasks._unconfirmed_submissions.clear()
        tasks.userIDDict.clear()
        self.load_users(self.friend("新昵称"),
                        {"unique_id": "other-id", "sec_uid": "other-sec", "nickname": "旧昵称"})
        self.assertEqual(tasks._target_delivery_statuses("account", ["other-id", "旧昵称"]), {})
        tasks._mark_target_pending_today("account", "旧昵称", "第二条")
        tasks._mark_target_sent_today("account", "旧昵称")
        tasks._unconfirmed_submissions.clear()
        self.assertEqual(tasks._target_delivery_statuses("account", ["friend-123", "新昵称", "other-id", "旧昵称"]),
                         {"friend-123": "pending", "新昵称": "pending",
                          "other-id": "submitted", "旧昵称": "submitted"})

    def test_finishing_preserves_the_identity_captured_before_enter(self):
        self.load_users(self.friend())
        tasks._mark_target_pending_today("account", "旧昵称", "消息")
        tasks.userIDDict.clear()
        self.load_users(self.friend("新昵称"), {"unique_id": "other-id", "nickname": "旧昵称"})
        tasks._mark_target_sent_today("account", "旧昵称")
        tasks._unconfirmed_submissions.clear()
        self.assertEqual(tasks._target_delivery_statuses("account", ["friend-123", "新昵称"]),
                         {"friend-123": "submitted", "新昵称": "submitted"})
        self.assertEqual(tasks._target_delivery_statuses("account", ["other-id", "旧昵称"]), {})

    def test_ids_with_conflicting_stable_fields_are_not_the_same_identity(self):
        self.load_users(self.friend())
        tasks._mark_target_pending_today("account", "旧昵称", "消息")
        tasks._unconfirmed_submissions.clear()
        tasks.userIDDict.clear()
        self.load_users({"unique_id": "friend-123", "sec_uid": "different-sec", "nickname": "另一人"})
        self.assertEqual(tasks._target_delivery_statuses("account", ["另一人"]), {})

    def test_unconfirmed_number_and_nickname_are_not_automatically_merged(self):
        tasks._mark_target_pending_today("account", "用户123", "消息")
        tasks._unconfirmed_submissions.clear()
        self.assertEqual(tasks._target_delivery_statuses("account", ["用户123", "123", "其他人"]),
                         {"用户123": "pending"})

    def test_same_recipient_is_still_isolated_between_sender_accounts(self):
        self.load_users(self.friend())
        tasks._mark_target_pending_today("account", "旧昵称", "消息")
        self.assertEqual(tasks._target_delivery_statuses("other-account", ["旧昵称", "friend-123"]), {})

    def test_final_write_failure_retains_cross_alias_pending_after_restart(self):
        self.load_users(self.friend())
        tasks._mark_target_pending_today("account", "旧昵称", "消息")
        with patch.object(tasks, "_persist_delivery_state", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                tasks._mark_target_sent_today("account", "旧昵称")
        tasks._unconfirmed_submissions.clear()
        self.assertEqual(tasks._target_delivery_statuses("account", ["friend-123", "sec-123"]),
                         {"friend-123": "pending", "sec-123": "pending"})


if __name__ == "__main__":
    unittest.main()
