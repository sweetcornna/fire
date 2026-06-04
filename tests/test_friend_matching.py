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
        self.page.chat_header_text = self.title


class _TextCandidate:
    def __init__(self, page, text, visible=True):
        self.page = page
        self.text = text
        self.visible = visible

    def is_visible(self):
        return self.visible

    def click(self):
        self.page.clicked_text_results.append(self.text)
        if self.text in self.page.text_result_chat_headers:
            self.page.chat_header_text = self.page.text_result_chat_headers[self.text]


class _TextLocator:
    def __init__(self, page, text):
        self.page = page
        self.text = text

    def count(self):
        return len(self.page.text_results.get(self.text, []))

    def nth(self, index):
        return self.page.text_results[self.text][index]


class _Locator:
    def __init__(self, page, selector, index=None):
        self.page = page
        self.selector = selector
        self.index = index
        self.broken = False
        if self._is_search() and self.index is not None:
            self.page.search_locator_creations += 1
            self.broken = (
                self.page.fail_first_search_locator
                and self.page.search_locator_creations == 1
            )

    def _is_search(self):
        return "input" in self.selector or "搜索" in self.selector

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
        if self._is_search():
            return len(self.page.search_boxes)
        return 0

    def nth(self, index):
        return _Locator(self.page, self.selector, index=index)

    def is_visible(self):
        if self._is_search() and self.index is not None:
            return self.page.search_boxes[self.index] is not None
        return True

    def bounding_box(self):
        if self.selector == tasks.CONVERSATION_LIST_SELECTOR:
            return self.page.list_box
        if self._is_search() and self.index is not None:
            return self.page.search_boxes[self.index]
        return None

    def click(self, *args, **kwargs):
        if self.broken:
            raise RuntimeError("stale search input")
        self.page.search_clicks += 1

    def fill(self, value, *args, **kwargs):
        if self.broken:
            raise RuntimeError("stale search input")
        self.page.search_terms.append(value)
        self.page.search_term_indexes.append(self.index)
        self.page.titles = self.page.search_results.get(value, [])

    def press(self, key, *args, **kwargs):
        if self.broken:
            raise RuntimeError("stale search input")

    def type(self, value, *args, **kwargs):
        if self.broken:
            raise RuntimeError("stale search input")
        self.page.search_terms.append(value)
        self.page.search_term_indexes.append(self.index)
        self.page.titles = self.page.search_results.get(value, [])


class _Page:
    def __init__(
        self,
        titles,
        after_first_scroll,
        search_results=None,
        max_scroll_top=None,
        fail_first_search_locator=False,
        search_boxes=None,
        list_box=None,
        chat_editor_available=True,
        chat_header_text="",
        text_results=None,
        text_result_chat_headers=None,
    ):
        self.titles = titles
        self.after_first_scroll = after_first_scroll
        self.search_results = search_results or {}
        self.max_scroll_top = max_scroll_top
        self.search_terms = []
        self.search_term_indexes = []
        self.search_clicks = 0
        self.search_locator_creations = 0
        self.fail_first_search_locator = fail_first_search_locator
        self.search_boxes = search_boxes or [
            {"x": 0, "y": 0, "width": 100, "height": 30}
        ]
        self.list_box = list_box or {"x": 0, "y": 40, "width": 300, "height": 600}
        self.chat_editor_available = chat_editor_available
        self.chat_header_text = chat_header_text
        self.text_result_chat_headers = text_result_chat_headers or {}
        self.clicked_text_results = []
        self.text_results = {}
        for text, results in (text_results or {}).items():
            self.text_results[text] = [
                _TextCandidate(self, result) if isinstance(result, str) else result
                for result in results
            ]
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
        if isinstance(element, dict) and "terms" in element:
            terms = [tasks._norm_value(term) for term in element["terms"]]
            header = tasks._norm_value(self.chat_header_text)
            matched = any(term and term in header for term in terms)
            return {"matched": matched, "snippets": [header] if matched else []}
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

    def wait_for_selector(self, selector, timeout=None):
        if selector == tasks.CHAT_EDITOR_SELECTOR and not self.chat_editor_available:
            raise RuntimeError("chat editor unavailable")
        return True

    def get_by_text(self, text, exact=True):
        return _TextLocator(self, text)


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

    def test_summarizes_target_matches_without_clicking(self):
        tasks.userIDDict["熊霖竹"] = [
            "20060941610",
            "20060941610",
            "",
            "兴隆竹🏵️",
            "熊霖竹",
        ]

        matched, unmatched = tasks.summarize_target_matches(
            ["熊霖竹", "其他好友"],
            ["兴隆竹🏵️", "漏发好友"],
        )

        self.assertEqual(matched, {"兴隆竹🏵️": "熊霖竹"})
        self.assertEqual(unmatched, ["漏发好友"])

    def test_find_search_input_prefers_sidebar_search_near_conversation_list(self):
        page = _Page(
            [],
            after_first_scroll=lambda: None,
            search_boxes=[
                {"x": 500, "y": 10, "width": 300, "height": 36},
                {"x": 16, "y": 110, "width": 260, "height": 36},
            ],
            list_box={"x": 0, "y": 156, "width": 320, "height": 600},
        )

        search_input = tasks.find_search_input(page)
        search_input.fill("目标")

        self.assertEqual(page.search_term_indexes[-1], 1)

    def test_search_reacquires_input_after_failed_term(self):
        tasks.userIDDict["备用名"] = [
            "target-id",
            "target-id",
            "",
            "备用名",
            "备用名",
        ]
        page = _Page(
            [],
            after_first_scroll=lambda: None,
            search_results={"备用名": ["备用名"]},
            fail_first_search_locator=True,
        )

        selected = tasks.search_and_select_target(page, "主账号", "target-id")

        self.assertEqual(selected, "target-id")
        self.assertEqual(page.clicked_titles, ["备用名"])
        self.assertIn("备用名", page.search_terms)
        self.assertGreaterEqual(page.search_locator_creations, 2)

    def test_text_result_does_not_select_when_chat_header_stays_on_previous_user(self):
        tasks.userIDDict["黑眼圈"] = [
            "1369556832",
            "",
            "",
            "黑眼圈",
            "黑眼圈",
        ]
        page = _Page(
            [],
            after_first_scroll=lambda: None,
            chat_header_text="熊霖竹",
            text_results={"黑眼圈": ["黑眼圈"]},
        )

        selected = tasks.click_visible_text_result(
            page,
            "主账号",
            "1369556832",
            ["1369556832", "黑眼圈"],
        )

        self.assertIsNone(selected)
        self.assertEqual(page.clicked_text_results, ["黑眼圈"])

    def test_text_result_selects_after_chat_header_switches_to_target(self):
        tasks.userIDDict["黑眼圈"] = [
            "1369556832",
            "",
            "",
            "黑眼圈",
            "黑眼圈",
        ]
        page = _Page(
            [],
            after_first_scroll=lambda: None,
            chat_header_text="熊霖竹",
            text_results={"黑眼圈": ["黑眼圈"]},
            text_result_chat_headers={"黑眼圈": "黑眼圈"},
        )

        selected = tasks.click_visible_text_result(
            page,
            "主账号",
            "1369556832",
            ["1369556832", "黑眼圈"],
        )

        self.assertEqual(selected, "1369556832")
        self.assertEqual(page.clicked_text_results, ["黑眼圈"])


if __name__ == "__main__":
    unittest.main()
