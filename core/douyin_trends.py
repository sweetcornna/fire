"""获取抖音实时热榜，作为 AI 写消息时的当天素材。"""

from functools import lru_cache

import requests

from utils.logger import setup_logger


logger = setup_logger()

DOUYIN_HOT_URL = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"
DEFAULT_TOPIC_LIMIT = 30


@lru_cache(maxsize=1)
def fetch_douyin_hot_topics(limit: int = DEFAULT_TOPIC_LIMIT):
    """返回 ``(榜单时间, 热词元组)``；失败时返回空值供普通消息兜底。"""
    try:
        response = requests.get(
            DOUYIN_HOT_URL,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.douyin.com/",
            },
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
        topics = tuple(
            item["word"].strip()
            for item in payload.get("word_list", [])
            if isinstance(item, dict)
            and isinstance(item.get("word"), str)
            and item["word"].strip()
        )[:limit]
        return str(payload.get("active_time", "")).strip(), topics
    except Exception as exc:
        logger.warning(f"获取抖音实时热榜失败，将生成不带热梗的普通消息：{exc}")
        return "", ()
