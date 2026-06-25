"""
core/msg_builder.py
消息构建入口：决定今天的「形式」并渲染为最终发送文本。

具体的形式选择 / AI 生成 / 模板渲染见：
- core/forms.py            选择形式（AI 优先，模板兜底）
- core/content_providers.py  占位符内容源（一言/问候/日期/节日）
"""

from datetime import date

from utils.config import get_config
from core.forms import select_and_build


def build_message(today=None) -> str:
    """构建今天要发送的续火花消息。

    today 可选，默认今天；保留无参调用以兼容 core/tasks.py。
    """
    today = today or date.today()
    return select_and_build(today, get_config())
