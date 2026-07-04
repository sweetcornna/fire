"""
core/forms.py
决定今天发哪种「形式」的消息：AI 优先，模板兜底。

- 配置了 AI（有 key 且未被关闭）时，每天按日期轮换一个 persona 调用 OpenAI 现写一句，
  节日当天把节日氛围注入提示词；任何失败（无 key / 网络 / 空返回）都回落模板路径。
- 模板路径从模板池里按 daily-rotate（date.toordinal() % len，保证今天≠昨天，
  无需外部状态，契合 GitHub Actions 无状态运行）选一套，再用内容 providers 渲染。
"""

from datetime import date
from random import choice

from utils.logger import setup_logger
from core.content_providers import render_placeholders, festival_quote

logger = setup_logger()

# 开箱即用的默认模板池：结构/开头/内容来源各不相同，仅复用原版已验证的抖音 emoji 短码
# （[盖瑞] [加一] [右边] [左边]），避免使用未经验证的短码导致收到方看到字面文本。
DEFAULT_TEMPLATES = [
    "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[一言]",
    "今日火花[加一]\\n[问候]\\n[一言]",
    "[盖瑞]又是一起续火花的一天\\n[日期]\\n[一言]",
    "火花不能断哦[加一]\\n[问候]，今天也要开开心心~",
    "[右边] 每日分享 [左边]\\n[一言]\\n火花继续[盖瑞]",
    "[盖瑞]火花继续[加一]\\n[节日]\\n[问候]",
]

# 走「祝福语」方向：暖心、单向的日常祝愿，用来续火花。
# persona 给不同的祝福角度，避免每天都同一句。
DEFAULT_PERSONAS = [
    "日常祝福，祝今天顺利、开心",
    "关心叮嘱，提醒对方好好吃饭、早点休息",
    "元气打气，给对方鼓鼓劲",
    "轻松愉快，祝对方放松一点、别太累",
    "暖心祝愿，盼对方平平安安、心情好",
]

# 这些是模型写祝福时最爱堆的「AI 味」高频词，写进禁用清单逼它落地一点。
# 注意：「愿你」是正常祝福开头，保留可用，不进禁用清单。
AI_CLICHE_WORDS = [
    "星辰",
    "星空",
    "星星",
    "奔赴",
    "宇宙",
    "光芒",
    "彼岸",
    "山海",
    "热爱",
    "治愈",
]

DEFAULT_SELECTION_MODE = "daily-rotate"


def build_ai_prompt(persona: str, festival):
    """构造续火花「祝福语」的 (system, user) 提示词。

    目标：一句暖心、自然、不端着的日常祝福，谁收到都合适。返回纯文本，便于单测。
    """
    cliche = "、".join(AI_CLICHE_WORDS)
    system = (
        "你在给微信/抖音上的朋友发一句暖心的日常祝福，用来『续火花』。"
        f"今天的祝福角度：{persona}。\n"
        "重要前提：你和对方不一定很熟，也没有任何真实的共同经历，"
        "这是一句单向的祝福，对方收到不该觉得莫名其妙，所以只发『谁收到都合适』的通用祝愿。\n"
        "要求：\n"
        "- 中文，短一点，大概 6～18 个字，自然真诚\n"
        "- 像朋友顺手送上的祝福，口语一点，别端着\n"
        "- 最多 1 个 emoji，没有也行\n"
        "禁止：\n"
        "- 这是单向祝福，不要提问、不要查户口，别问对方在不在/忙不忙/吃了没\n"
        "- 不要鸡汤、不要情话、不要广告文案\n"
        "- 不要过度文艺、不要排比、不要押韵堆砌\n"
        f"- 不要用这些 AI 味的词：{cliche}\n"
        "- 换着角度写，别每天都同一句『今天也要开开心心』\n"
        "- 不要编造具体事件，不要假装有共同记忆，别提『上次 / 那件事 / 你说的那个』\n"
        "- 不要假装注意到对方的具体变化，"
        "别说『你头像换了 / 看到你的动态 / 你最近状态』这种你根本不知道的事\n"
        "- 不要解释、不要引号、不要书名号，直接把那句祝福发出来"
    )

    user = "送一句暖心的续火花祝福"
    if festival:
        user += f"。今天是 {festival}，让祝福自然贴合这个节日"

    return system, user


def resolve_templates(config) -> list:
    """模板池优先级：MESSAGE_TEMPLATES > 显式设置的旧 MESSAGE_TEMPLATE > 默认模板池。"""
    templates = config.get("messageTemplates")
    if templates:
        return templates
    single = config.get("messageTemplate")
    if single:
        return [single]
    return DEFAULT_TEMPLATES


def pick_template(templates: list, today: date, mode: str) -> str:
    if not templates:
        return ""
    if mode == "random":
        return choice(templates)
    # 默认 daily-rotate：按日期轮换，保证逐日不同
    return templates[today.toordinal() % len(templates)]


def ai_enabled(config) -> bool:
    flag = config.get("aiEnable", "")
    if flag == "0":
        return False
    if flag == "1":
        return True  # 强制开启：即便无 key 也会尝试 -> 失败 -> 兜底
    return bool(config.get("anthropic", {}).get("api_key"))


def normalize_anthropic_base_url(base_url: str):
    """归一化 Anthropic 网关根地址：去掉结尾的 /v1（SDK 会自动补 /v1/messages）。"""
    if not base_url:
        return None
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[: -len("/v1")]
    return base_url


def build_ai_message(today: date, config) -> str:
    """通过 Anthropic 协议现写一句续火花消息。失败/空返回时抛异常以触发兜底。"""
    from anthropic import Anthropic

    ai_cfg = config.get("anthropic", {})
    api_key = ai_cfg.get("api_key", "")
    if not api_key:
        raise ValueError("未配置 ANTHROPIC_API_KEY")

    base_url = normalize_anthropic_base_url(ai_cfg.get("base_url", ""))
    model = ai_cfg.get("model", "claude-sonnet-4-6")

    personas = config.get("aiPersonas") or DEFAULT_PERSONAS
    persona = personas[today.toordinal() % len(personas)]

    festival = festival_quote(today)
    system_prompt, user_prompt = build_ai_prompt(persona, festival)

    client = Anthropic(api_key=api_key, base_url=base_url) if base_url else Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=128,  # 写人味短句不需要长输出
        # 适度提温，避免每天同一句（Sonnet 4.6 支持 temperature）
        temperature=1.0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    content = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    if not content:
        raise ValueError("AI 返回空内容")
    return content


def select_and_build(today: date, config) -> str:
    """选定今天的形式并返回最终发送文本。"""
    if ai_enabled(config):
        try:
            message = build_ai_message(today, config)
            if message:
                logger.debug(f"AI 生成今日消息: {message}")
                return message
        except Exception as exc:  # 无 key / 网络 / 空返回 -> 兜底模板
            logger.warning(f"AI 生成消息失败，回落模板路径：{exc}")

    mode = config.get("messageSelectionMode", DEFAULT_SELECTION_MODE)
    templates = resolve_templates(config)

    # 节日当天优先选用含 [节日] 占位符的模板，让节日真正「应景」
    if festival_quote(today):
        festival_templates = [t for t in templates if "[节日]" in t]
        if festival_templates:
            templates = festival_templates

    template = pick_template(templates, today, mode)
    return render_placeholders(template, today)
