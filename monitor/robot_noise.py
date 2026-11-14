"""Shared ASR noise transcript rules for every voice entry point."""

from __future__ import annotations

import re


_NOISE_STRIP = re.compile(
    r"[\s，。！？、：；,.!?~～…·“”‘’\"'()（）【】《》<>]+"
)


NOISE_TRANSCRIPTS = frozenset({
    "对", "嗯", "啊", "哦", "噢", "哈", "呃", "诶", "哎", "呀",
    "哇", "唉", "哼", "切", "啧", "嘿", "喂", "嘟",
    "好的", "好", "行", "行吧", "好吧", "嗯嗯", "哦哦", "呃呃",
    "啊哈", "嗯哼", "对呀", "对哦", "好哦", "好呀", "system",
    "ok", "哦可", "好的好的", "嗯哈", "嗯呐", "嗯嗯嗯", "哦哦哦",
    "嗯啊", "啊嗯",
})


def is_noise_transcript(
    text: str,
    *,
    allow_short_ack: bool = False,
) -> bool:
    value = _NOISE_STRIP.sub("", str(text or "")).lower()
    if not value:
        return True
    if value not in NOISE_TRANSCRIPTS:
        return False
    if allow_short_ack and value in {"对", "嗯", "好", "好的"}:
        return False
    return True
