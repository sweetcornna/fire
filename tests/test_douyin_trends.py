import unittest
from unittest.mock import Mock, patch

from core.douyin_trends import fetch_douyin_hot_topics


class DouyinTrendsTest(unittest.TestCase):
    def setUp(self):
        fetch_douyin_hot_topics.cache_clear()

    def tearDown(self):
        fetch_douyin_hot_topics.cache_clear()

    @patch("core.douyin_trends.requests.get")
    def test_fetches_and_caches_current_topics(self, get):
        response = Mock()
        response.json.return_value = {
            "active_time": "2026-09-04 16:50:39",
            "word_list": [
                {"word": "开学版井柏然进行曲"},
                {"word": "  当我有一件燕麦格雷外套  "},
                {"not_word": "ignored"},
            ],
        }
        get.return_value = response

        first = fetch_douyin_hot_topics(limit=2)
        second = fetch_douyin_hot_topics(limit=2)

        self.assertEqual(
            first,
            (
                "2026-09-04 16:50:39",
                ("开学版井柏然进行曲", "当我有一件燕麦格雷外套"),
            ),
        )
        self.assertEqual(first, second)
        get.assert_called_once()

    @patch("core.douyin_trends.requests.get", side_effect=RuntimeError("offline"))
    def test_failure_returns_empty_topics(self, _get):
        self.assertEqual(fetch_douyin_hot_topics(), ("", ()))


if __name__ == "__main__":
    unittest.main()
