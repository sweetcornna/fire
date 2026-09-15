import unittest
from unittest.mock import patch

import core.tasks as tasks


class _TitleLocator:
    def __init__(self, text):
        self.text = text

    def inner_text(self, *args, **kwargs):
        return self.text


class _ConversationItem:
    def __init__(self, page, conversation):
        self.page = page
        self.conversation = conversation

    def locator(self, selector):
        return _TitleLocator(self.conversation["title"])

    def is_visible(self):
        return True

    def click(self, *args, **kwargs):
        self.page.clicked.append(self.conversation["title"])
        if self.conversation.get("opens", True):
            self.page.header_names = list(
                self.conversation.get("headers") or [self.conversation["name"]]
            )


class _Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def all(self):
        if self.selector == tasks.CONVERSATION_ITEM_SELECTOR:
            return [
                _ConversationItem(self.page, conversation)
                for conversation in self.page.visible_conversations()
            ]
        return []

    def element_handle(self, *args, **kwargs):
        if self.selector == tasks.CONVERSATION_LIST_SELECTOR:
            return self.page.list_handle
        return None


class _Page:
    """A virtualized conversation list that only renders a few rows at once."""

    def __init__(self, conversations, window=3, header=""):
        self.conversations = conversations
        self.window = window
        self.scroll_top = 0
        self.header_names = [header] if header else []
        self.clicked = []
        self.list_handle = object()

    def visible_conversations(self):
        return self.conversations[self.scroll_top:self.scroll_top + self.window]

    def locator(self, selector):
        return _Locator(self, selector)

    def evaluate(self, script, argument=None):
        if isinstance(argument, dict) and "headerSelector" in argument:
            return list(self.header_names)
        if "scrollTop = 0" in script:
            self.scroll_top = 0
            return None
        if "scrollTop +=" in script:
            self.scroll_top = min(
                self.scroll_top + 1, max(0, len(self.conversations) - self.window)
            )
            return None
        if "scrollHeight" in script:
            return {"top": self.scroll_top, "height": len(self.conversations)}
        if "scrollTop" in script:
            return self.scroll_top
        return None


def conversation(title, name, opens=True, headers=None):
    return {"title": title, "name": name, "opens": opens, "headers": headers}


class PlaceholderIdentityTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(tasks, "placeholderIdentityDict", {})
        self.identities = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(patch.object(tasks, "userIDDict", {}).start)
        sleep = patch.object(tasks.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        # Keep the header wait short: sleep is stubbed out, so the real
        # chat-open timeout would spin for seconds per unresolved click.
        timeouts = patch.dict(
            tasks.config, {"chatOpenTimeout": 50, "listGrowthWaitSeconds": 0}
        )
        timeouts.start()
        self.addCleanup(timeouts.stop)
        self.submit = patch.object(tasks, "_submit_chat_message").start()
        self.addCleanup(patch.stopall)

    def probe(self, page, targets, **kwargs):
        kwargs.setdefault("timeout", 30)
        return tasks.probe_placeholder_identities(page, "账号", targets, **kwargs)

    def test_id_only_conversation_is_opened_to_learn_who_it_belongs_to(self):
        page = _Page([conversation("2745912548403578", "Lakers")])

        resolved = self.probe(page, ["Lakers"])

        self.assertEqual(resolved, {"2745912548403578": "Lakers"})
        self.assertEqual(page.clicked, ["2745912548403578"])
        self.assertEqual(
            tasks.checkTargetName("2745912548403578", ["Lakers"]), "Lakers"
        )
        self.submit.assert_not_called()

    def test_named_conversations_are_never_opened_while_probing(self):
        page = _Page([conversation("已命名好友", "已命名好友")])

        self.assertEqual(self.probe(page, ["已命名好友"]), {})
        self.assertEqual(page.clicked, [])

    def test_probing_stops_once_every_pending_target_is_identified(self):
        page = _Page(
            [
                conversation("2745912548403578", "Lakers"),
                conversation("479033791626500", "陌生人"),
            ]
        )

        self.probe(page, ["Lakers"])

        self.assertEqual(page.clicked, ["2745912548403578"])

    def test_placeholders_below_the_visible_window_are_scrolled_into_reach(self):
        page = _Page(
            [
                conversation("已命名好友", "已命名好友"),
                conversation("101750481689", "陌生人"),
                conversation("2745912548403578", "Lakers"),
            ],
            window=1,
        )

        resolved = self.probe(page, ["Lakers"])

        self.assertEqual(resolved.get("2745912548403578"), "Lakers")
        self.assertEqual(page.clicked, ["101750481689", "2745912548403578"])

    def test_a_click_that_opens_nothing_records_no_identity(self):
        page = _Page(
            [
                conversation("2745912548403578", "Lakers"),
                conversation("479033791626500", "天伦", opens=False),
            ]
        )

        resolved = self.probe(page, ["Lakers", "天伦"])

        self.assertEqual(resolved, {"2745912548403578": "Lakers"})
        self.assertNotIn("479033791626500", tasks.placeholderIdentityDict)
        self.assertIsNone(tasks.checkTargetName("479033791626500", ["天伦"]))

    def test_an_already_open_chat_is_not_mistaken_for_a_probed_conversation(self):
        page = _Page(
            [conversation("2745912548403578", "天伦", opens=False)],
            header="上一位好友",
        )

        self.assertEqual(self.probe(page, ["上一位好友", "天伦"]), {})
        self.assertEqual(tasks.placeholderIdentityDict, {})

    def test_status_text_beside_the_name_does_not_hide_a_pending_target(self):
        page = _Page(
            [conversation("2745912548403578", "天伦", headers=["天伦 对方正在输入", "天伦"])]
        )

        resolved = self.probe(page, ["天伦"])

        self.assertEqual(resolved, {"2745912548403578": "天伦"})
        self.assertEqual(tasks.checkTargetName("2745912548403578", ["天伦"]), "天伦")

    def test_probing_without_pending_targets_does_not_touch_the_page(self):
        page = _Page([conversation("2745912548403578", "Lakers")])

        self.assertEqual(self.probe(page, []), {})
        self.assertEqual(page.clicked, [])
        self.assertEqual(page.scroll_top, 0)

    def test_probed_identity_never_overrides_an_ambiguous_nickname_rule(self):
        tasks.userIDDict.update(
            {
                "重名": ["111", "", "", "重名", "重名"],
                "另一个重名": ["222", "", "", "重名", "另一个重名"],
            }
        )
        page = _Page([conversation("2745912548403578", "重名")])

        self.probe(page, ["重名"])

        self.assertEqual(tasks.placeholderIdentityDict["2745912548403578"], "重名")
        self.assertIsNone(tasks.checkTargetName("2745912548403578", ["重名"]))

    def test_list_bottom_probes_id_only_conversations_before_falling_back_to_search(self):
        page = _Page([conversation("2745912548403578", "Lakers")])

        with patch.object(tasks, "wait_for_chat_editor", return_value=True), patch.object(
            tasks, "search_remaining_targets", return_value=iter(())
        ) as search:
            selected = list(tasks.scroll_and_select_user(page, "账号", ["Lakers"]))

        self.assertEqual(selected, ["Lakers"])
        search.assert_not_called()
        self.submit.assert_not_called()

    def test_a_target_that_only_loads_later_is_found_on_a_second_lap(self):
        page = _Page([conversation("先到的好友", "先到的好友")])

        def load_more():
            if len(page.conversations) == 1:
                page.conversations.append(conversation("迟到的好友", "迟到的好友"))

        original = tasks._reset_conversation_scroll

        def reset(*args, **kwargs):
            load_more()
            return original(*args, **kwargs)

        with patch.object(tasks, "wait_for_chat_editor", return_value=True), patch.object(
            tasks, "search_remaining_targets", return_value=iter(())
        ), patch.object(tasks, "_reset_conversation_scroll", side_effect=reset):
            selected = list(tasks.scroll_and_select_user(page, "账号", ["迟到的好友"]))

        self.assertEqual(selected, ["迟到的好友"])

    def test_selection_clicks_the_id_only_conversation_of_a_probed_target(self):
        page = _Page([conversation("2745912548403578", "Lakers")])
        self.probe(page, ["Lakers"])
        page.clicked.clear()

        with patch.object(tasks, "wait_for_chat_editor", return_value=True):
            selected = tasks.click_matching_visible_user(page, "账号", ["Lakers"])

        self.assertEqual(selected, "Lakers")
        self.assertEqual(page.clicked, ["2745912548403578"])


if __name__ == "__main__":
    unittest.main()
