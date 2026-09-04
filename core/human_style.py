"""从近期公开抖音视频评论中整理的短句声纹样本。

只供模型学习句长、停顿、语义错位和调侃力度；生成结果不得照抄原句。
"""


HUMAN_STYLE_SAMPLES = (
    "你的胆子真是肥嘟嘟的",
    "不是说钱能养人吗",
    "检测到你的直播风格为路边，建议改成户外",
    "原来AI短剧也是要拍摄的吗",
    "删了就是没攀，看不见就是没有攀",
    "孩子行，能处，有东西是真往外掏",
    "你这个镜子建议贴个防爆膜",
    "除了爱迟到了一点，还有哪里不好",
)


def format_human_style_samples() -> str:
    return "\n".join(f"- {sample}" for sample in HUMAN_STYLE_SAMPLES)


def copies_human_style_sample(message: str, fragment_size: int = 6) -> bool:
    """至少连续照抄六个字符时判定为复制，避免把公开评论原样发出去。"""
    for sample in HUMAN_STYLE_SAMPLES:
        compact = sample.replace("，", "").replace("。", "").replace("？", "")
        for start in range(max(0, len(compact) - fragment_size + 1)):
            if compact[start : start + fragment_size] in message:
                return True
    return False
