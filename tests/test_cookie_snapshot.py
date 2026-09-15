import json
import tempfile
import unittest
from pathlib import Path

from utils import cookie_snapshot


def cookie(name, value="v", domain=".douyin.com"):
    return {"name": name, "value": value, "domain": domain}


class CookieSnapshotTests(unittest.TestCase):
    def write(self, payload):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "cookies.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_snapshot_with_a_session_cookie_is_accepted(self):
        path = self.write([cookie("sessionid"), cookie("ttwid")])

        self.assertEqual(cookie_snapshot.main([path]), 0)

    def test_snapshot_without_any_login_cookie_is_refused(self):
        with self.assertRaises(SystemExit) as refused:
            cookie_snapshot.validate([cookie("ttwid"), cookie("msToken")])

        self.assertIn("登录态", str(refused.exception))

    def test_empty_or_malformed_snapshot_is_refused(self):
        for payload in ([], {}, [cookie("sessionid", value="")], ["sessionid"]):
            with self.assertRaises(SystemExit):
                cookie_snapshot.validate(payload)

    def test_the_nameless_cookie_douyin_serves_does_not_fail_the_check(self):
        nameless = {"name": "", "value": "douyin.com", "domain": "www.douyin.com"}

        self.assertEqual(
            cookie_snapshot.validate([nameless, cookie("sid_guard")]), ["sid_guard"]
        )

    def test_a_snapshot_of_only_unusable_entries_is_refused(self):
        with self.assertRaises(SystemExit):
            cookie_snapshot.validate([{"name": "", "value": "douyin.com"}])

    def test_missing_file_is_refused_without_a_traceback(self):
        with self.assertRaises(SystemExit):
            cookie_snapshot.load_snapshot("/nonexistent/cookies.json")

    def test_validation_never_echoes_a_cookie_value(self):
        secret = "sid-guard-secret-value"
        with self.assertRaises(SystemExit) as refused:
            cookie_snapshot.validate([{"name": "sid_guard", "value": secret}])

        self.assertNotIn(secret, str(refused.exception))


class SessionFingerprintTests(unittest.TestCase):
    def test_rotation_is_visible_without_exposing_the_session(self):
        import core.tasks as tasks

        before = tasks._cookie_fingerprint([cookie("sid_guard", "old"), cookie("ttwid", "x")])
        after = tasks._cookie_fingerprint([cookie("sid_guard", "new")])

        self.assertEqual(list(before), ["sid_guard"])  # only login cookies count
        self.assertNotEqual(before["sid_guard"], after["sid_guard"])
        self.assertEqual(len(before["sid_guard"]), 8)
        self.assertNotIn("old", json.dumps(before))


class ConversationSyncSummaryTests(unittest.TestCase):
    def test_summary_reports_shape_without_any_conversation_content(self):
        import core.tasks as tasks

        body = json.dumps(
            {
                "data": {
                    "has_more": True,
                    "next_cursor": "42",
                    "conversation_list": [
                        {"conversation_id": "c1", "name": "私密昵称"},
                        {"conversation_id": "c2", "name": "另一个昵称"},
                    ],
                }
            }
        ).encode("utf-8")

        summary = tasks.describe_conversation_payload("application/json", body)

        self.assertEqual(summary["conversations"], 2)
        self.assertTrue(summary["has_more"])
        self.assertEqual(summary["bytes"], len(body))
        self.assertNotIn("私密昵称", json.dumps(summary, ensure_ascii=False))

    def test_a_binary_response_still_reports_its_size(self):
        import core.tasks as tasks

        summary = tasks.describe_conversation_payload("application/octet-stream", b"\x00\x01\x02")

        self.assertEqual(summary, {"type": "application/octet-stream", "bytes": 3})


if __name__ == "__main__":
    unittest.main()
