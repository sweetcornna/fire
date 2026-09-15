import unittest
from unittest.mock import Mock, patch

import core.tasks as tasks


FOLLOWING_RESPONSE = {
    "followings": [
        {
            "nickname": "互关好友",
            "remark_name": "备注名",
            "sec_uid": "sec-1",
            "unique_id": "friend1",
            "short_id": "111",
            "follow_status": 1,
            "follower_status": 1,
        },
        {
            "nickname": "只有我关注他",
            "sec_uid": "sec-2",
            "unique_id": "friend2",
            "follow_status": 1,
            "follower_status": 0,
        },
        {
            "nickname": "接口用状态2表示互关",
            "sec_uid": "sec-3",
            "follow_status": 2,
        },
    ],
    "has_more": False,
}


class FriendListParsingTests(unittest.TestCase):
    def test_every_contact_is_parsed_with_its_ids(self):
        records = tasks.parse_friend_list_payload(FOLLOWING_RESPONSE)

        self.assertEqual([record["nickname"] for record in records],
                         ["互关好友", "只有我关注他", "接口用状态2表示互关"])
        self.assertEqual(records[0]["remark_name"], "备注名")
        self.assertEqual(records[0]["unique_id"], "friend1")

    def test_only_mutual_contacts_count_as_friends(self):
        records = tasks.parse_friend_list_payload(FOLLOWING_RESPONSE)
        mutual = [record["nickname"] for record in records if tasks.is_mutual_friend(record)]

        self.assertEqual(mutual, ["互关好友", "接口用状态2表示互关"])

    def test_a_contact_listed_twice_is_kept_once(self):
        payload = {"data": {"users": FOLLOWING_RESPONSE["followings"] * 2}}

        self.assertEqual(len(tasks.parse_friend_list_payload(payload)), 3)

    def test_a_payload_without_contacts_yields_nothing(self):
        for payload in ({}, {"status_code": 0}, [], "", None):
            self.assertEqual(tasks.parse_friend_list_payload(payload), [])


class FriendListDiagnosticTests(unittest.TestCase):
    def _response(self, url, payload):
        response = Mock()
        response.url = url
        response.status = 200
        response.json.return_value = payload
        return response

    def test_diagnostic_collects_mutual_friends_and_sends_nothing(self):
        page = Mock()
        page.evaluate.return_value = []
        handlers = []
        page.on.side_effect = lambda event, handler: handlers.append(handler)

        with patch.object(tasks.time, "sleep"), patch.object(
            tasks, "retry_operation",
            side_effect=lambda name, operation, **kwargs: handlers[0](
                self._response(
                    "https://www.douyin.com/aweme/v1/web/user/following/list/?count=20",
                    FOLLOWING_RESPONSE,
                )
            ),
        ), patch.object(tasks, "_submit_chat_message") as send:
            mutual = tasks.diagnose_friend_list(page, "账号")

        self.assertEqual([record["nickname"] for record in mutual],
                         ["互关好友", "接口用状态2表示互关"])
        send.assert_not_called()

    def test_an_unrelated_response_is_ignored(self):
        page = Mock()
        handlers = []
        page.on.side_effect = lambda event, handler: handlers.append(handler)
        page.evaluate.return_value = []

        with patch.object(tasks.time, "sleep"), patch.object(
            tasks, "retry_operation",
            side_effect=lambda name, operation, **kwargs: handlers[0](
                self._response("https://www.douyin.com/aweme/v1/web/aweme/post/", FOLLOWING_RESPONSE)
            ),
        ):
            self.assertEqual(tasks.diagnose_friend_list(page, "账号"), [])


if __name__ == "__main__":
    unittest.main()
