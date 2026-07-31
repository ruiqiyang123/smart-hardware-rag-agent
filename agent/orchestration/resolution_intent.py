"""Deterministic recognition of explicit customer resolution confirmations."""

from __future__ import annotations

import re


_PUNCTUATION = re.compile(r"[\s,，。.!！;；:：、~～]+")
_QUESTION_MARKERS = ("?", "？", "吗", "么", "呢", "是不是")
_CONTINUATION_MARKERS = (
    "没解决",
    "没有解决",
    "未解决",
    "还没",
    "不行",
    "不可以",
    "还是",
    "仍然",
    "但是",
    "不过",
    "可是",
    "然而",
    "继续",
    "再问",
    "还有",
    "另外",
    "怎么办",
    "怎么",
    "为什么",
)
_EXACT_CONFIRMATIONS = frozenset(
    {
        "谢谢",
        "感谢",
        "谢谢你",
        "多谢",
        "解决了",
        "已经解决了",
        "问题解决了",
        "问题已解决",
        "好了",
        "已经好了",
        "可以了",
        "没问题了",
        "搞定了",
        "好的谢谢",
        "谢谢解决了",
        "谢谢已经解决了",
        "ok",
        "okay",
    }
)
_STRONG_CONFIRMATIONS = (
    "已经解决了",
    "问题解决了",
    "问题已解决",
    "已经好了",
    "没问题了",
    "搞定了",
)


def is_resolution_confirmation(value: object) -> bool:
    """Return True only for a short, standalone confirmation.

    Unknown, questioning, or mixed messages deliberately fall through to the
    normal follow-up workflow. This prevents text such as "谢谢，但是还是不行"
    from accidentally closing the ticket.
    """

    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    if not text or len(text) > 80:
        return False
    compact = _PUNCTUATION.sub("", text)
    if not compact:
        return False
    if compact in _EXACT_CONFIRMATIONS:
        return True
    if any(marker in text for marker in _QUESTION_MARKERS):
        return False
    if any(marker in compact for marker in _CONTINUATION_MARKERS):
        return False
    return any(marker in compact for marker in _STRONG_CONFIRMATIONS)
