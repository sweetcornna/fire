import unittest

import core.tasks as tasks


class SelectorTests(unittest.TestCase):
    def test_conversation_item_selector_uses_class_selector_so_pinned_items_match(self):
        self.assertEqual(tasks.CONVERSATION_ITEM_SELECTOR, '.conversationConversationItemwrapper')
        self.assertEqual(tasks.CONVERSATION_TITLE_SELECTOR, '.conversationConversationItemtitle')
        self.assertEqual(tasks.CONVERSATION_LIST_SELECTOR, '.conversationConversationListwrapper')
        self.assertEqual(tasks.CHAT_EDITOR_SELECTOR, '.messageEditorimChatEditorContainer')
        self.assertIn('[contenteditable="true"]', tasks.CHAT_EDITOR_FALLBACK_SELECTOR)


if __name__ == '__main__':
    unittest.main()
