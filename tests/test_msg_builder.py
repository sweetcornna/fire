import unittest
from datetime import date
from unittest.mock import patch

import core.content_providers as cp
import core.forms as forms


# 2026-06-24 是普通日（非春节文案库覆盖范围），2026-02-17 为正月初一
NORMAL_DAY = date(2026, 6, 24)
FESTIVAL_DAY = date(2026, 2, 17)


class RenderPlaceholdersTest(unittest.TestCase):
    def test_known_keys_replaced_and_emoji_preserved(self):
        with patch.object(cp, "request_hitokoto", return_value="一言内容"):
            out = cp.render_placeholders("[盖瑞]今日火花[加一]\\n[一言]", NORMAL_DAY)
        # 未知占位符（抖音 emoji 短码）原样保留
        self.assertIn("[盖瑞]", out)
        self.assertIn("[加一]", out)
        # 已知占位符被替换
        self.assertIn("一言内容", out)
        self.assertNotIn("[一言]", out)

    def test_api_legacy_alias(self):
        with patch.object(cp, "request_hitokoto", return_value="Q"):
            out = cp.render_placeholders("[API]", NORMAL_DAY)
        self.assertEqual(out, "Q")

    def test_empty_festival_line_dropped(self):
        # 非节日时 [节日] 渲染为空，对应行被丢弃
        out = cp.render_placeholders("火花继续\\n[节日]", NORMAL_DAY)
        self.assertEqual(out, "火花继续")

    def test_festival_hit(self):
        out = cp.render_placeholders("[节日]", FESTIVAL_DAY)
        self.assertTrue(out)  # 命中节日文案库，非空
        self.assertNotIn("[节日]", out)

    def test_festival_quote_stable_same_day(self):
        self.assertEqual(
            cp.festival_quote(FESTIVAL_DAY), cp.festival_quote(FESTIVAL_DAY)
        )

    def test_date_and_greeting(self):
        self.assertEqual(cp.date_text(NORMAL_DAY), "6月24日 周三")
        self.assertIn("周三", cp.greeting(NORMAL_DAY))


class PickTemplateTest(unittest.TestCase):
    TEMPLATES = ["A", "B", "C"]

    def test_daily_rotate_changes_each_day(self):
        a = forms.pick_template(self.TEMPLATES, NORMAL_DAY, "daily-rotate")
        b = forms.pick_template(self.TEMPLATES, date(2026, 6, 25), "daily-rotate")
        self.assertNotEqual(a, b)

    def test_daily_rotate_stable_same_day(self):
        a = forms.pick_template(self.TEMPLATES, NORMAL_DAY, "daily-rotate")
        b = forms.pick_template(self.TEMPLATES, NORMAL_DAY, "daily-rotate")
        self.assertEqual(a, b)


class ResolveTemplatesTest(unittest.TestCase):
    def test_templates_list_priority(self):
        self.assertEqual(forms.resolve_templates({"messageTemplates": ["X"]}), ["X"])

    def test_legacy_single(self):
        self.assertEqual(
            forms.resolve_templates({"messageTemplates": None, "messageTemplate": "Y"}),
            ["Y"],
        )

    def test_defaults_when_unset(self):
        out = forms.resolve_templates({"messageTemplates": None, "messageTemplate": ""})
        self.assertEqual(out, forms.DEFAULT_TEMPLATES)


class BuildAiPromptTest(unittest.TestCase):
    def test_system_prompt_sets_blessing_tone(self):
        # 祝福语方向：暖心的单向祝愿，而不是聊天提问
        system, _user = forms.build_ai_prompt("日常祝福", None)
        self.assertIn("祝", system)  # 明确是祝福/祝愿
        self.assertIn("续火花", system)  # 仍然是续火花用途

    def test_blessing_does_not_interrogate(self):
        # 单向祝福，不要查户口式提问
        system, _user = forms.build_ai_prompt("日常祝福", None)
        self.assertTrue("提问" in system or "查户口" in system)

    def test_system_prompt_forbids_cliche_and_copywriting(self):
        # 仍然禁止鸡汤 / 情话 / 广告文案 / AI 味套话
        system, _user = forms.build_ai_prompt("日常祝福", None)
        self.assertIn("鸡汤", system)
        self.assertIn("情话", system)
        self.assertIn("文案", system)
        self.assertTrue(any(word in system for word in forms.AI_CLICHE_WORDS))

    def test_yuanni_is_allowed_blessing_opener(self):
        # “愿你”是正常祝福开头，不应再被当成 AI 味禁用词
        self.assertNotIn("愿你", forms.AI_CLICHE_WORDS)

    def test_persona_included_in_prompt(self):
        system, _user = forms.build_ai_prompt("关心叮嘱", None)
        self.assertIn("关心叮嘱", system)

    def test_festival_injected_only_when_present(self):
        _s1, user_plain = forms.build_ai_prompt("日常祝福", None)
        self.assertNotIn("节日", user_plain)

        _s2, user_festival = forms.build_ai_prompt("日常祝福", "新年快乐")
        self.assertIn("新年快乐", user_festival)

    def test_default_personas_are_blessing_style(self):
        # persona 池是祝福角度，且每个都是有区分度的说明
        self.assertTrue(len(forms.DEFAULT_PERSONAS) >= 3)
        for persona in forms.DEFAULT_PERSONAS:
            self.assertGreaterEqual(len(persona), 4, persona)
        # 不再是“随手聊天/欠欠”那套
        joined = "".join(forms.DEFAULT_PERSONAS)
        self.assertNotIn("欠", joined)
        self.assertNotIn("在忙", joined)

    def test_prompt_pushes_variety(self):
        # 别每天都同一句祝福
        system, _user = forms.build_ai_prompt("日常祝福", None)
        self.assertTrue("换" in system or "别每天" in system or "别总" in system)

    def test_prompt_forbids_fabricated_context(self):
        # 祝福也不能编造具体事件 / 假装共同经历
        system, _user = forms.build_ai_prompt("日常祝福", None)
        self.assertIn("编造", system)
        self.assertIn("上次", system)

    def test_prompt_forbids_pretend_observation(self):
        # 不能假装注意到对方的具体变化
        system, _user = forms.build_ai_prompt("日常祝福", None)
        self.assertIn("头像", system)
        self.assertIn("动态", system)


class BuildAiMessageTest(unittest.TestCase):
    def test_root_openai_compatible_base_url_adds_v1_prefix(self):
        cfg = {
            "aiPersonas": ["温馨亲切"],
            "openai": {
                "api_key": "fake-test-key",
                "base_url": "https://api.cornna.xyz/",
                "model": "gemini-3.5-flash-low",
            },
        }

        with patch("openai.OpenAI") as OpenAI:
            client = OpenAI.return_value
            response = client.chat.completions.create.return_value
            response.choices = [unittest.mock.Mock()]
            response.choices[0].message.content = "火花继续呀"

            out = forms.build_ai_message(NORMAL_DAY, cfg)

        self.assertEqual(out, "火花继续呀")
        OpenAI.assert_called_once_with(
            api_key="fake-test-key", base_url="https://api.cornna.xyz/v1"
        )


class SelectAndBuildTest(unittest.TestCase):
    def _config(self, **over):
        cfg = {
            "messageTemplate": "",
            "messageTemplates": ["[一言]"],
            "messageSelectionMode": "daily-rotate",
            "aiEnable": "0",
            "openai": {"api_key": ""},
        }
        cfg.update(over)
        return cfg

    def test_ai_disabled_uses_template(self):
        with patch.object(cp, "request_hitokoto", return_value="HELLO"):
            out = forms.select_and_build(NORMAL_DAY, self._config())
        self.assertEqual(out, "HELLO")

    def test_ai_failure_falls_back_to_template(self):
        cfg = self._config(aiEnable="1", openai={"api_key": "x"})
        with patch.object(forms, "build_ai_message", side_effect=RuntimeError("boom")), \
                patch.object(cp, "request_hitokoto", return_value="FB"):
            out = forms.select_and_build(NORMAL_DAY, cfg)
        self.assertEqual(out, "FB")

    def test_ai_success_used(self):
        cfg = self._config(aiEnable="1", openai={"api_key": "x"})
        with patch.object(forms, "build_ai_message", return_value="AI写的火花"):
            out = forms.select_and_build(NORMAL_DAY, cfg)
        self.assertEqual(out, "AI写的火花")

    def test_legacy_single_template_still_renders(self):
        # 向后兼容：仅设旧版单一模板，[API] 仍被一言替换
        cfg = self._config(
            messageTemplate="今日火花[加一] [API]", messageTemplates=None
        )
        with patch.object(cp, "request_hitokoto", return_value="名言"):
            out = forms.select_and_build(NORMAL_DAY, cfg)
        self.assertIn("名言", out)
        self.assertIn("[加一]", out)  # emoji 短码保留


if __name__ == "__main__":
    unittest.main()
