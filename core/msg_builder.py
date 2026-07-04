"""
core/msg_builder.py
解析消息模板构建具体发送的消息内容
"""

from utils.config import get_config
from utils.hitokoto import request_hitokoto
from datetime import date


def _normalize_anthropic_base_url(base_url: str):
    """归一化 Anthropic 网关根地址：去掉结尾的 /v1（SDK 会自动补 /v1/messages）。"""
    if not base_url:
        return ""
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[: -len("/v1")]
    return base_url


def build_message_with_openai() -> str:
    """
    通过 Anthropic 协议生成续火花消息，内容丰富，不超过20字。
    （函数名沿用历史，实际走 Anthropic /v1/messages。）
    """
    from anthropic import Anthropic

    import os

    api_key = os.getenv("ANTHROPIC_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    base_url = _normalize_anthropic_base_url(
        os.getenv("ANTHROPIC_BASE_URL", os.getenv("OPENAI_BASE_URL", ""))
    )
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    if not api_key:
        return get_config().get("messageTemplate", "续火花")

    client = Anthropic(api_key=api_key, base_url=base_url) if base_url else Anthropic(api_key=api_key)

    response = client.messages.create(
        model=model,
        max_tokens=128,
        system="你是一个擅长写续火花消息的助手。用户需要你生成一段不超过20字的续火花消息，内容要温馨、有趣、适合发给聊天对象。请直接输出消息内容，不要加引号或其他修饰。",
        messages=[
            {"role": "user", "content": "生成一段续火花消息，直接输出内容不要思考过程"},
        ],
    )

    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


def build_message() -> str:
    message = get_config().get("messageTemplate", "续火花")
    if "[API]" in message:
        api_content = request_hitokoto()
        message = message.replace("[API]", api_content)

    return message.strip()
