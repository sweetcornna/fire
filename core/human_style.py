"""从近期公开抖音视频评论中整理的短句声纹样本。

只供模型学习句长、停顿、语义错位和调侃力度；生成结果不得照抄原句。
"""


HUMAN_STYLE_SAMPLES = (
    "你的胆子真是肥嘟嘟的",
    "摸鱼还挑时间吗，我想摸就摸",
    "这别致的系统成功留住了我",
    "你敢买我都不敢用",
    "同事突然疯了该怎么办",
    "排队给蜜蜂蛰吗，有意思",
    "我勒个不败刚们",
    "快收一收吧，一会该挨揍了",
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
