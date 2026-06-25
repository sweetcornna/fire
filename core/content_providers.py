"""
core/content_providers.py
内容源注册表：把消息模板中的「已知占位符」[key] 替换为动态内容。

未注册的 [...]（例如抖音 emoji 短码 [盖瑞]、[加一]、[右边]）会原样保留，
与旧版只替换 [API] 的行为一致，只是从单一 key 推广为可扩展的注册表。

行分隔约定：模板使用字面量 \\n（反斜杠 + n 两个字符）作为换行，
core/tasks.py 会把每个 \\n 转成 Shift+Enter 发送，因此本模块同样按字面量
\\n 拆分/拼接，并在渲染后丢弃因空占位符产生的空行。
"""

import re
from datetime import date
from random import Random

from utils.hitokoto import request_hitokoto
from utils.chinese_new_year_2026_mare import SPRING_FESTIVAL_QUOTES

# 字面量换行符（两个字符：反斜杠 + n），与模板及 tasks.py 的拆分方式保持一致
LINE_SEP = "\\n"

WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]

_PLACEHOLDER_RE = re.compile(r"\[([^\[\]]+)\]")

_WEEKDAY_GREETINGS = {
    0: "新的一周，周一加油",
    1: "周二好，稳稳向前",
    2: "周三啦，一周过半",
    3: "周四好，再坚持一下",
    4: "周五啦，周末在招手",
    5: "周六好好放松",
    6: "周日愉快，养精蓄锐",
}


def hitokoto_quote(today: date) -> str:
    """一言内容（忽略 today，保留统一签名）。"""
    return request_hitokoto()


def greeting(today: date) -> str:
    """按星期给出的问候语。"""
    return _WEEKDAY_GREETINGS[today.weekday()]


def date_text(today: date) -> str:
    """今天是几月几日 周几。"""
    return f"{today.month}月{today.day}日 周{WEEKDAY_CN[today.weekday()]}"


def festival_quote(today: date) -> str:
    """节日文案：命中春节文案库则返回一条，否则返回空串。

    用 Random(today.toordinal()) 做种子，保证「当天稳定、逐日不同、可测试」。
    """
    quotes = SPRING_FESTIVAL_QUOTES.get(today)
    if not quotes:
        return ""
    return Random(today.toordinal()).choice(quotes)


# 占位符 key -> provider 函数，签名统一为 fn(today: date) -> str
PROVIDERS = {
    "一言": hitokoto_quote,
    "API": hitokoto_quote,  # 兼容旧模板里的 [API]
    "问候": greeting,
    "日期": date_text,
    "节日": festival_quote,
}


def render_placeholders(template: str, today: date) -> str:
    """渲染模板：替换已知占位符，保留未知 [...]，并丢弃空行。"""

    def _repl(match: "re.Match") -> str:
        key = match.group(1)
        provider = PROVIDERS.get(key)
        if provider is None:
            return match.group(0)  # 未知占位符（emoji 短码等）原样保留
        return provider(today)

    rendered = _PLACEHOLDER_RE.sub(_repl, template)
    # 丢弃因空占位符（如非节日时的 [节日]）产生的空行
    segments = [seg for seg in rendered.split(LINE_SEP) if seg.strip()]
    return LINE_SEP.join(segments).strip()
