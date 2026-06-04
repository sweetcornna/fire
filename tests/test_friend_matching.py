import unittest

import core.tasks as tasks


class _TitleLocator:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class _ConversationItem:
    def __init__(self, page, title, visible=True):
        self.page = page
        self.title = title
        self.visible = visible

    def locator(self, selector):
        return _TitleLocator(self.title)

    def is_visible(self):
        return self.visible

    def click(self):
        self.page.clicked_titles.append(self.title)


class _Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def all(self):
        if self.selector == tasks.CONVERSATION_ITEM_SELECTOR:
            return [
                _ConversationItem(self.page, title, visible)
                for title, visible in self.page.iter_titles()
            ]
        return []

    def element_handle(self):
        if self.selector == tasks.CONVERSATION_LIST_SELECTOR:
            return object()
        return None

    def count(self):
        if "input" in self.selector or "搜索" in self.selector:
            return 1
        return 0

    def nth(self, index):
        return self

    def is_visible(self):
        return True

    def click(self):
        self.page.search_clicks += 1

    def fill(self, value):
        self.page.search_terms.append(value)
        self.page.titles = self.page.search_results.get(value, [])


class _Page:
    def __init__(
        self,
        titles,
        after_first_scroll,
        search_results=None,
        max_scroll_top=None,
    ):
        self.titles = titles
        self.after_first_scroll = after_first_scroll
        self.search_results = search_results or {}
        self.max_scroll_top = max_scroll_top
        self.search_terms = []
        self.search_clicks = 0
        self.clicked_titles = []
        self.scroll_top = 0
        self.scrolls = 0

    def iter_titles(self):
        for item in self.titles:
            if isinstance(item, tuple):
                yield item
            else:
                yield item, True

    def locator(self, selector):
        return _Locator(self, selector)

    def evaluate(self, script, element):
        if "scrollTop +=" in script:
            self.scrolls += 1
            next_scroll_top = self.scroll_top + 800
            if self.max_scroll_top is not None:
                next_scroll_top = min(next_scroll_top, self.max_scroll_top)
            self.scroll_top = next_scroll_top
            if self.scrolls == 1:
                self.after_first_scroll()
            return None
        if "scrollTop" in script:
            return self.scroll_top
        return None


class FriendMatchingTests(unittest.TestCase):
    def setUp(self):
        self._sleep = tasks.time.sleep
        self._user_id_dict = tasks.userIDDict
        tasks.time.sleep = lambda _seconds: None
        tasks.userIDDict = {}

    def tearDown(self):
        tasks.time.sleep = self._sleep
        tasks.userIDDict = self._user_id_dict

    def test_scroll_rechecks_seen_friend_after_api_mapping_arrives(self):
        def add_mapping():
            tasks.userIDDict["熊霖竹"] = [
                "20060941610",
                "20060941610",
                "",
                "兴隆竹🏵️",
                "熊霖竹",
            ]

        page = _Page(["熊霖竹"], after_first_scroll=add_mapping)

        selected = next(tasks.scroll_and_select_user(page, "主账号", ["兴隆竹🏵️"]))

        self.assertEqual(selected, "兴隆竹🏵️")
        self.assertEqual(page.clicked_titles, ["熊霖竹"])

    def test_scroll_searches_remaining_target_after_list_reaches_bottom(self):
        page = _Page(
            ["其他好友"],
            after_first_scroll=lambda: None,
            search_results={"学姐说保研": ["学姐说保研"]},
        )

        selected = next(tasks.scroll_and_select_user(page, "主账号", ["学姐说保研"]))

        self.assertEqual(selected, "学姐说保研")
        self.assertIn("学姐说保研", page.search_terms)
        self.assertEqual(page.clicked_titles, ["学姐说保研"])

    def test_visible_search_match_skips_invisible_items(self):
        page = _Page([("目标好友", False)], after_first_scroll=lambda: None)

        selected = tasks.click_matching_visible_user(page, "主账号", ["目标好友"])

        self.assertIsNone(selected)
        self.assertEqual(page.clicked_titles, [])

    def test_user_number_target_searches_plain_number_too(self):
        self.assertEqual(
            tasks.get_search_terms_for_target("用户2061764921260"),
            ["用户2061764921260", "2061764921260"],
        )

    def test_builds_douyin_user_search_url(self):
        self.assertEqual(
            tasks.get_user_search_url("用户2061764921260"),
            "https://www.douyin.com/search/%E7%94%A8%E6%88%B72061764921260?type=user",
        )

    def test_user_number_target_matches_current_name_from_api_alias(self):
        tasks.userIDDict["涵老师"] = [
            "",
            "2061764921260",
            "",
            "涵老师",
            "涵老师",
        ]

        self.assertEqual(
            tasks.get_search_terms_for_target("用户2061764921260"),
            ["用户2061764921260", "2061764921260", "涵老师"],
        )
        self.assertEqual(
            tasks.checkTargetName("涵老师", ["用户2061764921260"]),
            "用户2061764921260",
        )

    def test_scroll_does_not_select_already_completed_target_again(self):
        tasks.userIDDict["重复好友"] = [
            "target-a",
            "target-a",
            "",
            "重复好友",
            "重复好友",
        ]
        page = _Page(
            ["重复好友"],
            after_first_scroll=lambda: None,
            max_scroll_top=0,
        )

        selected = tasks.scroll_and_select_user(
            page, "主账号", ["target-a", "target-b"]
        )

        self.assertEqual(next(selected), "target-a")
        with self.assertRaises(StopIteration):
            next(selected)
        self.assertEqual(page.clicked_titles, ["重复好友"])


if __name__ == "__main__":
    unittest.main()
