import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import core.tasks as tasks


class DeliveryConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "delivery.json"
        self.stack.enter_context(patch.dict(os.environ, {"DELIVERY_STATE_FILE": str(self.path)}))
        self.stack.enter_context(patch.object(tasks, "_unconfirmed_submissions", set()))
        self.stack.enter_context(patch.object(tasks, "userIDDict", {}))
        self.stack.enter_context(patch.object(tasks, "build_message", return_value="消息"))
        self.stack.enter_context(patch.object(tasks.time, "sleep"))
        self.stack.enter_context(patch.object(tasks, "_dismiss_login_prompt", return_value=False))
        self.input = Mock(spec=["type", "press"])
        self.stack.enter_context(patch.object(tasks, "_chat_target_match", return_value=(True, ["好友"])))
        self.stack.enter_context(patch.object(tasks, "_wait_for_chat_input", return_value=self.input))
        self.snapshot = self.stack.enter_context(patch.object(
            tasks, "_chat_submission_snapshot",
            return_value={"editor_text": "", "message_count": 2, "failure_count": 0},
        ))
        self.confirm = self.stack.enter_context(patch.object(tasks, "_wait_for_submission_confirmation"))
        self.page = Mock()
        self.input.type.side_effect = lambda value, **kwargs: self.snapshot.return_value.update(editor_text=value)

    def submit(self):
        return tasks._submit_chat_message(self.page, "显示名", "好友", "消息", "account")

    def record(self):
        return json.loads(self.path.read_text())["days"][tasks.date.today().isoformat()]["account"]["好友"]

    def test_confirmed_ui_submission_is_persisted_and_not_repeated(self):
        self.assertEqual(self.submit(), "好友")
        self.assertEqual(self.record()["status"], "submitted")
        self.assertEqual(self.submit(), "好友")
        self.assertEqual(self.input.press.call_count, 1)
        self.assertEqual(tasks._completed_targets_for_today("account", ["好友"]), {"好友"})

    def test_no_confirmation_survives_restart_without_becoming_complete(self):
        self.confirm.side_effect = tasks.DeliveryUncertainError("no UI confirmation")
        with self.assertRaises(tasks.DeliveryUncertainError):
            self.submit()
        self.assertEqual(self.record()["status"], "pending")
        self.assertIn("message_hash", self.record())
        tasks._unconfirmed_submissions.clear()  # A new process has no memory of the attempt.
        with self.assertRaises(tasks.DeliveryUncertainError):
            self.submit()
        self.assertEqual(self.input.press.call_count, 1)
        self.assertEqual(tasks._completed_targets_for_today("account", ["好友"]), set())

    def test_state_is_pending_before_enter_is_pressed(self):
        observed = []
        self.input.press.side_effect = lambda *args, **kwargs: observed.append(self.record()["status"])
        self.submit()
        self.assertEqual(observed, ["pending"])

    def test_transient_final_write_retries_only_the_write(self):
        real_mark = tasks._mark_target_sent_today
        calls = []

        def mark(*args):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError("disk temporarily unavailable")
            return real_mark(*args)

        with patch.object(tasks, "_mark_target_sent_today", side_effect=mark):
            self.submit()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.input.press.call_count, 1)
        self.assertEqual(self.record()["status"], "submitted")

    def test_permanent_final_write_failure_cannot_trigger_whole_send_retry(self):
        with patch.object(tasks, "_mark_target_sent_today", side_effect=RuntimeError("disk failure")), \
             patch.object(tasks, "_send_message_to_target", side_effect=lambda *args: self.submit()), \
             patch.object(tasks, "_open_chat_page_for_retry") as reset:
            self.assertIsNone(tasks._send_target_with_retries(self.page, "显示名", "好友", "消息", "account"))
        reset.assert_not_called()
        self.assertEqual(self.input.press.call_count, 1)
        self.assertEqual(self.record()["status"], "pending")

    def test_failed_preflight_write_does_not_press_enter(self):
        with patch.object(tasks, "_persist_delivery_state", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                self.submit()
        self.input.press.assert_not_called()

    def test_internal_type_error_never_replays_a_keypress(self):
        self.input.press.side_effect = TypeError("failure after key was dispatched")
        with self.assertRaises(tasks.DeliveryUncertainError):
            self.submit()
        self.assertEqual(self.input.press.call_count, 1)
        self.assertEqual(self.record()["status"], "pending")

    def test_existing_draft_is_preserved(self):
        self.snapshot.return_value["editor_text"] = "未发送的草稿"
        with self.assertRaisesRegex(RuntimeError, "已有草稿"):
            self.submit()
        self.input.type.assert_not_called()
        self.input.press.assert_not_called()

    def test_failed_typing_cannot_be_reported_as_a_submission(self):
        self.input.type.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "内容与本次消息不一致"):
            self.submit()
        self.input.press.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_changing_chat_during_typing_prevents_enter(self):
        with patch.object(tasks, "_chat_target_match", side_effect=[(True, ["好友"]), (False, ["其他好友"])]):
            with self.assertRaisesRegex(RuntimeError, "当前聊天已改变"):
                self.submit()
        self.input.press.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_state_remains_account_specific_and_accepts_legacy_sent_records(self):
        today = tasks.date.today().isoformat()
        self.path.write_text(json.dumps({"days": {today: {
            "account": {"好友": {"sent_at": "2026-09-13T01:00:00+00:00"}},
            "other": {"好友": {"status": "pending", "attempted_at": "2026-09-13T01:01:00+00:00"}},
        }}}))
        self.assertEqual(tasks._completed_targets_for_today("account", ["好友"]), {"好友"})
        self.assertEqual(tasks._completed_targets_for_today("other", ["好友"]), set())

    def test_chat_editor_waits_for_target_identity_to_switch(self):
        with patch.object(tasks, "_chat_target_match", side_effect=[(False, ["旧好友"]), (True, ["好友"])]):
            self.assertTrue(tasks.wait_for_chat_editor(self.page, "账号", "好友", timeout=1000))

    def test_live_diagnostic_probes_search_without_typing_or_creating_state(self):
        self.page.evaluate.return_value = {"inputs": [], "chat": []}
        search_input = Mock()
        with patch.object(tasks, "collect_friend_titles", return_value=["列表好友"]), \
             patch.object(tasks, "wait_for_chat_ready", return_value=True), \
             patch.object(tasks, "_open_chat_page_for_retry"), \
             patch.object(tasks, "click_matching_visible_user", side_effect=["列表好友", None, None]), \
             patch.object(tasks, "find_search_input", return_value=search_input), \
             patch.object(tasks, "fill_search_input") as fill, \
             patch.object(tasks, "click_visible_text_result", return_value="搜索好友") as search, \
             patch.object(tasks, "_find_chat_input", return_value=self.input):
            matched, unmatched = tasks.diagnose_friend_matching(self.page, "账号", ["列表好友", "搜索好友"])
        self.assertEqual(matched, {"列表好友": "列表好友"})
        self.assertEqual(unmatched, ["搜索好友"])
        fill.assert_called_once_with(search_input, "搜索好友")
        search.assert_called_once_with(self.page, "账号", "搜索好友", ["搜索好友"])
        self.input.type.assert_not_called()
        self.input.press.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_account_resume_sends_only_unattempted_targets_and_reports_pending(self):
        tasks._mark_target_pending_today("account", "待核验好友", "消息")
        tasks._unconfirmed_submissions.clear()
        browser = Mock()
        context = browser.new_context.return_value
        context.new_page.return_value = self.page
        selected = []

        def selections(page, username, targets):
            selected.extend(targets)
            return iter(targets)

        with patch.object(tasks, "wait_for_chat_ready", return_value=True), patch.object(
            tasks, "scroll_and_select_user", side_effect=selections
        ), patch.object(tasks, "_persist_cookie_snapshot"):
            with self.assertRaisesRegex(RuntimeError, "1/2"):
                tasks.do_user_task(browser, "显示名", [], ["待核验好友", "未尝试好友"], "account")
        self.assertEqual(selected, ["未尝试好友"])
        self.assertEqual(self.input.press.call_count, 1)
        self.assertEqual(tasks._target_delivery_statuses("account", selected), {"未尝试好友": "submitted"})
        context.close.assert_called_once()

    def test_unselected_target_is_in_failure_report(self):
        browser = Mock()
        browser.new_context.return_value.new_page.return_value = self.page
        with patch.object(tasks, "wait_for_chat_ready", return_value=True), patch.object(
            tasks, "scroll_and_select_user", return_value=iter(())
        ), patch.object(tasks, "_persist_cookie_snapshot"), patch.object(tasks.logger, "warning") as warning:
            with self.assertRaisesRegex(RuntimeError, "0/1"):
                tasks.do_user_task(browser, "显示名", [], ["找不到的好友"], "account")
        self.assertIn("找不到的好友", str(warning.call_args))
        self.input.press.assert_not_called()

    def test_account_counts_id_and_name_as_complete_after_one_submission(self):
        browser = Mock()
        browser.new_context.return_value.new_page.return_value = self.page

        def hydrated(page, username):
            record = ["12345", "friend-id", "sec-friend", "好友", "好友"]
            for value in record:
                tasks.userIDDict[value] = record
            return True

        with patch.object(tasks, "wait_for_chat_ready", side_effect=hydrated), patch.object(
            tasks, "scroll_and_select_user", return_value=iter(["friend-id"])
        ), patch.object(tasks, "_persist_cookie_snapshot"):
            tasks.do_user_task(browser, "显示名", [], ["friend-id", "好友"], "account")
        self.assertEqual(self.input.press.call_count, 1)
        self.assertEqual(tasks._completed_targets_for_today("account", ["friend-id", "好友"]), {"friend-id", "好友"})

    def test_proxy_failure_never_replays_an_accepted_upstream_request(self):
        self.submit()
        browser = Mock()
        browser.new_context.return_value.new_page.return_value = self.page
        with patch.object(tasks, "wait_for_chat_ready", return_value=True), patch.object(
            tasks, "_persist_cookie_snapshot"
        ):
            tasks.do_user_task(browser, "显示名", [], ["好友"], "account")
        callback = self.page.route.call_args.args[1]
        for request_error in (None, ConnectionError("response lost")):
            with self.subTest(request_error=request_error):
                route = Mock()
                route.request.url = "https://imapi.douyin.com/v1/message/send_message"
                route.request.method = "POST"
                route.request.all_headers.return_value = {}
                route.request.post_data_buffer = b"fixture"
                route.fulfill.side_effect = RuntimeError("page closed after upstream response")
                with patch.object(tasks.requests, "request", side_effect=request_error, return_value=Mock(
                    status_code=200, headers={}, content=b"fixture response"
                )) as upstream:
                    callback(route)
                upstream.assert_called_once()
                route.continue_.assert_not_called()
                route.abort.assert_called_once_with("failed")


@unittest.skipUnless(os.getenv("HUOHUA_BROWSER_TESTS") == "1", "设置 HUOHUA_BROWSER_TESTS=1 执行离线浏览器测试")
class ChatDomConfirmationTests(unittest.TestCase):
    """Run the actual page JavaScript on a local fixture, with network blocked."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.playwright = sync_playwright().start()
        root = Path(__file__).resolve().parents[1]
        candidates = sorted((root / "chrome").glob(
            "chromium-*/chrome-mac-*/**/*.app/Contents/MacOS/*"
        ))
        try:
            options = {"headless": True}
            system_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            if system_chrome.is_file():
                options["executable_path"] = str(system_chrome)
            elif candidates:
                options["executable_path"] = str(candidates[-1])
            cls.browser = cls.playwright.chromium.launch(timeout=15000, **options)
        except Exception as error:
            cls.playwright.stop()
            raise unittest.SkipTest(f"离线浏览器不可用: {error}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1200, "height": 700})
        self.context.route("**/*", lambda route: route.abort())
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_content('''
            <div class="conversationConversationListwrapper"
                 style="position:absolute;left:0;top:0;width:300px;height:600px"></div>
            <header class="messageHeaderimChatHeaderContainer"
                    style="position:absolute;left:320px;top:20px;width:600px;height:45px">
                <span id="name">乙</span>
            </header>
            <div id="messages" style="position:absolute;left:320px;top:100px;width:600px">
                <p>今天遇到了甲</p>
            </div>
            <div contenteditable="true" data-placeholder="发送消息" id="editor"
                 style="position:absolute;left:320px;top:500px;width:600px;height:80px"></div>
        ''')
        self.user_ids = patch.object(tasks, "userIDDict", {})
        self.user_ids.start()
        self.addCleanup(self.user_ids.stop)

    def test_message_body_cannot_impersonate_chat_header(self):
        self.assertFalse(tasks._chat_target_match(self.page, "甲")[0])
        self.assertTrue(tasks._chat_target_match(self.page, "乙")[0])

    def test_header_match_is_exact_not_a_substring(self):
        self.page.locator("#name").evaluate("element => element.textContent = '甲乙'")
        self.assertFalse(tasks._chat_target_match(self.page, "甲")[0])
        self.assertTrue(tasks._chat_target_match(self.page, "甲乙")[0])

    def test_existing_message_and_cleared_input_are_not_a_new_submission(self):
        before = tasks._chat_submission_snapshot(self.page, "今天遇到了甲")
        self.assertEqual(before["message_count"], 1)
        with self.assertRaises(tasks.DeliveryUncertainError):
            tasks._wait_for_submission_confirmation(self.page, "乙", "今天遇到了甲", before, 1000)

    def test_new_message_and_cleared_input_confirm_only_the_current_chat(self):
        message = "本次测试消息"
        before = tasks._chat_submission_snapshot(self.page, message)
        self.page.evaluate("text => { const p = document.createElement('p'); p.textContent = text; document.querySelector('#messages').append(p); }", message)
        tasks._wait_for_submission_confirmation(self.page, "乙", message, before, 3000)

    def test_new_failure_indicator_prevents_confirmation(self):
        before = tasks._chat_submission_snapshot(self.page, "消息")
        self.page.evaluate("() => { const p = document.createElement('p'); p.textContent = '发送失败'; document.querySelector('#messages').append(p); }")
        with self.assertRaisesRegex(tasks.DeliveryUncertainError, "发送失败"):
            tasks._wait_for_submission_confirmation(self.page, "乙", "消息", before, 1000)

    def test_rendered_emoji_and_line_breaks_preserve_message_identity(self):
        self.page.locator("#messages").evaluate('''element => {
            element.innerHTML = '<p><img alt="[火花]">甲<br>乙</p>';
        }''')
        snapshot = tasks._chat_submission_snapshot(self.page, "[火花]甲\n乙")
        self.assertEqual(snapshot["message_count"], 1)

    def test_actual_typing_enter_confirmation_and_restart_submit_once(self):
        self.page.evaluate('''() => {
            window.submissions = 0;
            document.querySelector('#editor').addEventListener('keydown', event => {
                if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    window.submissions++;
                    const p = document.createElement('p');
                    p.textContent = event.currentTarget.innerText;
                    document.querySelector('#messages').append(p);
                    event.currentTarget.textContent = '';
                }
            });
        }''')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "DELIVERY_STATE_FILE": str(Path(directory) / "state.json")
        }), patch.object(tasks, "_unconfirmed_submissions", set()):
            for _ in range(2):
                self.assertEqual(tasks._submit_chat_message(self.page, "账号", "乙", "浏览器测试消息", "account"), "乙")
                tasks._unconfirmed_submissions.clear()
            self.assertEqual(self.page.evaluate("window.submissions"), 1)
            self.assertEqual(tasks._target_delivery_statuses("account", ["乙"]), {"乙": "submitted"})

    def test_noop_enter_stays_pending_across_retry_without_a_second_keypress(self):
        self.page.evaluate('''() => {
            window.submissions = 0;
            document.querySelector('#editor').addEventListener('keydown', event => {
                if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    window.submissions++;
                }
            });
        }''')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "DELIVERY_STATE_FILE": str(Path(directory) / "state.json")
        }), patch.object(tasks, "_unconfirmed_submissions", set()), patch.dict(
            tasks.config, {"chatSendActionTimeout": 1000}
        ), patch.object(tasks, "_open_chat_page_for_retry") as reset:
            for _ in range(2):
                self.assertIsNone(tasks._send_target_with_retries(self.page, "账号", "乙", "浏览器测试消息", "account"))
                tasks._unconfirmed_submissions.clear()
            self.assertEqual(self.page.evaluate("window.submissions"), 1)
            self.assertEqual(tasks._completed_targets_for_today("account", ["乙"]), set())
            reset.assert_not_called()

    def test_multiline_submit_survives_placeholder_removal_on_input(self):
        self.page.evaluate('''() => {
            const editor = document.querySelector('#editor');
            editor.className = 'messageEditorimChatEditorContainer';
            editor.addEventListener('input', () => editor.removeAttribute('data-placeholder'));
            window.submissions = 0;
            editor.addEventListener('keydown', event => {
                if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    window.submissions++;
                    const p = document.createElement('p');
                    p.textContent = editor.innerText;
                    document.querySelector('#messages').append(p);
                    editor.textContent = '';
                }
            });
        }''')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "DELIVERY_STATE_FILE": str(Path(directory) / "state.json")
        }), patch.object(tasks, "_unconfirmed_submissions", set()), patch.dict(
            tasks.config, {"chatSendActionTimeout": 2000}
        ):
            self.assertEqual(tasks._submit_chat_message(self.page, "账号", "乙", "甲\n乙", "account"), "乙")
            self.assertEqual(self.page.evaluate("window.submissions"), 1)
            self.assertEqual(tasks._target_delivery_statuses("account", ["乙"]), {"乙": "submitted"})

    def test_hidden_conversation_is_never_clicked_during_scroll(self):
        from playwright.sync_api import Locator
        self.page.locator('.conversationConversationListwrapper').evaluate('''element => {
            element.innerHTML = '<div class="conversationConversationItemwrapper" style="display:none">'
                + '<span class="conversationConversationItemtitle">乙</span></div>';
        }''')
        with patch.object(Locator, "click", side_effect=RuntimeError("hidden conversation clicked")) as click, \
             patch.object(tasks.time, "sleep"), \
             patch.object(tasks, "search_remaining_targets", return_value=iter(())):
            self.assertEqual(list(tasks.scroll_and_select_user(self.page, "账号", ["乙"])), [])
        click.assert_not_called()

    def test_failed_conversation_click_is_bounded_and_not_replayed_in_the_list(self):
        from playwright.sync_api import Locator
        self.page.locator('.conversationConversationListwrapper').evaluate('''element => {
            element.innerHTML = '<div class="conversationConversationItemwrapper">'
                + '<span class="conversationConversationItemtitle">乙</span></div>';
        }''')
        with patch.object(Locator, "click", side_effect=RuntimeError("conversation became detached")) as click, \
             patch.object(tasks.time, "sleep"), \
             patch.object(tasks.traceback, "print_exc"), \
             patch.object(tasks, "search_remaining_targets", return_value=iter(())):
            self.assertEqual(list(tasks.scroll_and_select_user(self.page, "账号", ["乙"])), [])
        self.assertEqual(click.call_count, 1)
        self.assertLessEqual(click.call_args.kwargs["timeout"], 5000)


if __name__ == "__main__":
    unittest.main()
