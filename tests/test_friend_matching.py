import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

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


class _EditableInput:
    def __init__(self, placeholder="发送消息"):
        self.placeholder = placeholder
        self.typed = []
        self.pressed = []

    def get_attribute(self, name):
        if name in {"placeholder", "data-placeholder"}:
            return self.placeholder
        return ""

    def is_visible(self):
        return True

    def type(self, value, *args, **kwargs):
        self.typed.append(value)

    def press(self, key, *args, **kwargs):
        self.pressed.append(key)


class _SelectorLocator:
    def __init__(self, values):
        self.values = values

    def count(self):
        return len(self.values)

    def nth(self, index):
        return self.values[index]


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
        if "scrollHeight" in script:
            # A list that still loads keeps growing; a finished one does not.
            return {"top": self.scroll_top, "height": self.scroll_height()}
        if "scrollTop" in script:
            return self.scroll_top
        return None

    def scroll_height(self):
        if self.max_scroll_top is None:
            return self.scroll_top + 800
        return self.max_scroll_top

    def wait_for_selector(self, selector, timeout=None):
        if (
            tasks.CHAT_EDITOR_SELECTOR in selector
            and not self.chat_editor_available
        ):
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
        # Real time still passes while sleep is stubbed out, so the live
        # wait for the list to load more would dominate the suite.
        growth = patch.dict(tasks.config, {"listGrowthWaitSeconds": 0})
        growth.start()
        self.addCleanup(growth.stop)

    def tearDown(self):
        tasks.time.sleep = self._sleep
        tasks.userIDDict = self._user_id_dict

    def _load_api_users(self, users):
        class Response:
            url = "https://www.douyin.com/aweme/v1/web/im/user/info"
            status = 200

            def json(self):
                return {"data": users}

        tasks.handle_response(Response())

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

    def test_alias_shared_nickname_never_imports_another_users_ids(self):
        users = [
            {"short_id": "101", "unique_id": "id-a", "sec_uid": "sec-a",
             "nickname": "同名", "remark_name": "甲备注"},
            {"short_id": "202", "unique_id": "id-b", "sec_uid": "sec-b",
             "nickname": "同名", "remark_name": "乙备注"},
        ]
        for ordered_users in (users, list(reversed(users))):
            with self.subTest(first_id=ordered_users[0]["unique_id"]):
                tasks.userIDDict.clear()
                self._load_api_users(ordered_users)

                for own, other in ((users[0], users[1]), (users[1], users[0])):
                    terms = tasks.get_search_terms_for_target(own["unique_id"])
                    self.assertNotIn("同名", terms)
                    for key in ("short_id", "unique_id", "sec_uid", "remark_name"):
                        self.assertNotIn(other[key], terms)
                        self.assertIsNone(
                            tasks.checkTargetName(other[key], [own["unique_id"]])
                        )
                        self.assertEqual(
                            tasks.checkTargetName(own[key], [own["unique_id"]]),
                            own["unique_id"],
                        )

    def test_alias_ambiguous_conversation_is_not_selected_for_either_id(self):
        self._load_api_users([
            {"unique_id": "id-a", "nickname": "同名"},
            {"unique_id": "id-b", "nickname": "同名"},
        ])
        page = _Page(["同名"], after_first_scroll=lambda: None)

        for target in ("id-a", "id-b"):
            self.assertIsNone(
                tasks.click_matching_visible_user(page, "主账号", [target])
            )
        self.assertEqual(page.clicked_titles, [])
        self.assertEqual(
            tasks.summarize_target_matches(["同名"], ["id-a", "id-b"]),
            ({}, ["id-a", "id-b"]),
        )

    def test_alias_unique_remarks_disambiguate_shared_nickname(self):
        self._load_api_users([
            {"unique_id": "id-a", "nickname": "同名", "remark_name": "甲备注"},
            {"unique_id": "id-b", "nickname": "同名", "remark_name": "乙备注"},
        ])

        for targets in (["id-a", "id-b"], ["id-b", "id-a"]):
            self.assertEqual(
                tasks.summarize_target_matches(["乙备注", "甲备注"], targets),
                ({"id-b": "乙备注", "id-a": "甲备注"}, []),
            )

    def test_alias_ambiguous_nickname_target_has_no_matching_candidates(self):
        self._load_api_users([
            {"unique_id": "id-a", "nickname": "同名"},
            {"unique_id": "id-b", "nickname": "同名"},
        ])

        self.assertEqual(tasks.get_search_terms_for_target("同名"), [])
        for visible_title in ("同名", "id-a", "id-b"):
            self.assertIsNone(tasks.checkTargetName(visible_title, ["同名"]))

    def test_alias_nickname_remark_chain_cannot_reach_other_users(self):
        self._load_api_users([
            {"unique_id": "id-a", "nickname": "连接甲", "remark_name": "独立备注"},
            {"unique_id": "id-b", "nickname": "连接乙", "remark_name": "连接甲"},
            {"unique_id": "id-c", "nickname": "第三人", "remark_name": "连接乙"},
        ])

        terms = tasks.get_search_terms_for_target("id-a")
        self.assertIn("独立备注", terms)
        for unrelated in ("连接甲", "连接乙", "第三人", "id-b", "id-c"):
            self.assertNotIn(unrelated, terms)
            self.assertIsNone(tasks.checkTargetName(unrelated, ["id-a"]))

    def test_alias_stable_id_takes_priority_over_another_users_nickname(self):
        self._load_api_users([
            {"unique_id": "id-a", "nickname": "甲"},
            {"unique_id": "id-b", "nickname": "id-a", "remark_name": "乙"},
        ])

        self.assertNotIn("id-a", tasks.get_search_terms_for_target("id-b"))
        self.assertIsNone(tasks.checkTargetName("id-a", ["id-b"]))
        self.assertIsNone(tasks.checkTargetName("乙", ["id-a"]))
        self.assertEqual(tasks.checkTargetName("id-a", ["id-b", "id-a"]), "id-a")

    def test_alias_same_identity_updates_keep_nonconflicting_names(self):
        self._load_api_users([{"unique_id": "id-a", "nickname": "旧昵称"}])
        self._load_api_users([{"unique_id": "id-a", "nickname": "新昵称"}])

        for nickname in ("旧昵称", "新昵称"):
            self.assertEqual(tasks.checkTargetName(nickname, ["id-a"]), "id-a")
        self.assertEqual(tasks.checkTargetName("id-a", ["新昵称"]), "新昵称")

    def test_alias_user_number_does_not_import_a_conflicting_nickname(self):
        self._load_api_users([
            {"unique_id": "123", "nickname": "甲"},
            {"unique_id": "id-b", "nickname": "用户123"},
        ])

        terms = tasks.get_search_terms_for_target("用户123")
        self.assertIn("123", terms)
        self.assertIn("甲", terms)
        self.assertNotIn("用户123", terms)
        self.assertNotIn("id-b", terms)
        self.assertIsNone(tasks.checkTargetName("id-b", ["用户123"]))
        self.assertEqual(tasks.checkTargetName("123", ["用户123"]), "用户123")

    def test_alias_unmapped_names_still_require_normalized_exact_match(self):
        self.assertEqual(tasks.checkTargetName(" 甲\u3000", ["甲"]), "甲")
        self.assertEqual(tasks.checkTargetName("匿名信🧊", ["匿名信"]), "匿名信")
        self.assertEqual(tasks.checkTargetName("犬系晓冰(尤铭译", ["犬系晓冰"]), "犬系晓冰")
        self.assertIsNone(tasks.checkTargetName("甲乙", ["甲"]))
        self.assertIsNone(tasks.checkTargetName("", [""]))
        self.assertEqual(tasks.get_search_terms_for_target("\u200b"), [])

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

    def test_handle_response_indexes_numeric_ids_for_unhydrated_titles(self):
        class MockResponse:
            url = "https://www.douyin.com/aweme/v1/web/im/user/info"
            status = 200

            def json(self):
                return {
                    "data": [
                        {
                            "short_id": "64261848150",
                            "unique_id": "1001007018940899",
                            "sec_uid": "MS4wLjABAAAA...",
                            "nickname": "测试好友昵称",
                            "remark_name": "测试备注",
                        }
                    ]
                }

        tasks.handle_response(MockResponse())
        self.assertIn("64261848150", tasks.userIDDict)
        self.assertIn("1001007018940899", tasks.userIDDict)
        self.assertIn("测试好友昵称", tasks.userIDDict)
        self.assertIn("测试备注", tasks.userIDDict)
        self.assertEqual(
            tasks.checkTargetName("64261848150", ["测试备注"]),
            "测试备注",
        )
        self.assertEqual(
            tasks.checkTargetName("1001007018940899", ["测试好友昵称"]),
            "测试好友昵称",
        )

    def test_handle_response_accepts_a_single_user_object(self):
        class MockResponse:
            url = "https://www.douyin.com/aweme/v1/web/im/user/info"
            status = 200

            def json(self):
                return {
                    "data": {
                        "user": {
                            "short_id": "7788",
                            "unique_id": "single-user",
                            "nickname": "单个好友",
                        }
                    }
                }

        tasks.handle_response(MockResponse())
        self.assertEqual(tasks.checkTargetName("单个好友", ["single-user"]), "single-user")

    def test_corrupt_delivery_state_is_not_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"DELIVERY_STATE_FILE": os.path.join(directory, "state.json")},
            clear=False,
        ):
            with open(os.path.join(directory, "state.json"), "w", encoding="utf-8") as handle:
                json.dump([], handle)
            with self.assertRaisesRegex(RuntimeError, "顶层必须是对象"):
                tasks._load_delivery_state()

    def test_submit_uses_editable_input_and_stable_delivery_key(self):
        editable = _EditableInput()

        class Page:
            def locator(self, selector):
                if selector == tasks.CHAT_INPUT_SELECTOR_PARTS[0]:
                    return _SelectorLocator([editable])
                return _SelectorLocator([])

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"DELIVERY_STATE_FILE": os.path.join(directory, "state.json")},
            clear=False,
        ), patch.object(tasks, "build_message", return_value="甲\\n乙"), patch.object(
            tasks, "_chat_submission_snapshot",
            side_effect=[
                {"editor_text": "", "message_count": 0, "failure_count": 0},
                {"editor_text": "甲 乙", "message_count": 0, "failure_count": 0},
            ],
        ), patch.object(tasks, "_wait_for_submission_confirmation"), patch.object(
            tasks, "_chat_target_match", return_value=(True, ["好友"])
        ):
            result = tasks._submit_chat_message(
                Page(), "显示名", "好友", delivery_key="stable-account-id"
            )
            with open(
                os.path.join(directory, "state.json"), encoding="utf-8"
            ) as handle:
                state = json.load(handle)

        self.assertEqual(result, "好友")
        self.assertEqual(editable.typed, ["甲", "乙"])
        self.assertEqual(editable.pressed, ["Shift+Enter", "Enter"])
        self.assertIn("stable-account-id", state["days"][tasks.date.today().isoformat()])


class ListGrowthScrollTests(unittest.TestCase):
    """The list pauses while it fetches the next page of conversations."""

    class _ScrollPage:
        def __init__(self, heights):
            self.heights = list(heights)
            self.scroll_top = 0
            self.reads = 0

        def locator(self, selector):
            page = self

            class _Handle:
                def element_handle(self, *args, **kwargs):
                    return page if selector == tasks.CONVERSATION_LIST_SELECTOR else None

            return _Handle()

        def evaluate(self, script, element=None):
            if "scrollHeight" in script:
                height = self.heights[min(self.reads, len(self.heights) - 1)]
                self.reads += 1
                return {"top": self.scroll_top, "height": height}
            if "scrollTop +=" in script:
                # Pinned at the end of what is currently rendered.
                return None
            return None

    def setUp(self):
        self._sleep = tasks.time.sleep
        tasks.time.sleep = lambda _seconds: None
        self.addCleanup(lambda: setattr(tasks.time, "sleep", self._sleep))

    def test_a_list_still_loading_is_not_taken_for_the_bottom(self):
        page = self._ScrollPage([2355, 2355, 6107])

        self.assertTrue(tasks._scroll_conversation_list(page, "账号", settle_seconds=5))

    def test_a_list_that_stops_growing_is_the_bottom(self):
        page = self._ScrollPage([6107])

        self.assertFalse(tasks._scroll_conversation_list(page, "账号", settle_seconds=0))


class DecoratedNameMatchingTests(unittest.TestCase):
    def test_target_keeps_matching_after_the_contact_drops_its_emoji(self):
        self.assertEqual(tasks.checkTargetName("AllenWolf", ["AllenWolf🐺"]), "AllenWolf🐺")
        self.assertEqual(tasks.checkTargetName("樊昕", ["樊昕🌈"]), "樊昕🌈")

    def test_emoji_stripping_never_picks_between_two_candidates(self):
        self.assertIsNone(tasks.checkTargetName("同名", ["同名🌈", "同名🐺"]))


class SearchDiagnosticsTests(unittest.TestCase):
    def test_fruitless_search_records_a_bounded_page_snapshot(self):
        page = Mock()
        page.evaluate.return_value = [
            {"css": "searchResultitem", "parent": "searchResultlist", "text": "候选好友"}
        ]

        with patch.object(tasks, "_search_snapshot_budget", 1):
            with self.assertLogs(tasks.logger, level="WARNING") as logs:
                tasks._log_search_result_snapshot(page, "账号", "目标")
            tasks._log_search_result_snapshot(page, "账号", "目标")

        self.assertEqual(page.evaluate.call_count, 1)
        self.assertIn("候选好友", logs.output[0])

    def test_snapshot_failure_never_breaks_the_search_path(self):
        page = Mock()
        page.evaluate.side_effect = RuntimeError("detached frame")

        with patch.object(tasks, "_search_snapshot_budget", 1):
            tasks._log_search_result_snapshot(page, "账号", "目标")


if __name__ == "__main__":
    unittest.main()
