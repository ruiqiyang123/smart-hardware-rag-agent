"""Small deterministic classifier for explicit customer human-handoff requests."""

import re


_HANDOFF_PATTERN = re.compile(
    r"(?:转|接|找|联系|呼叫|换)(?:一下|到|个|一位)?(?:人工|真人)(?:客服|服务|处理)?"
    r"|(?:人工|真人)(?:客服|服务|处理)"
)
_NEGATED_PATTERN = re.compile(
    r"(?:不要|不用|无需|不想|先不|别)(?:给我)?(?:转|接|找|联系)?(?:人工|真人)"
)


def is_human_handoff_request(text: object) -> bool:
    if not isinstance(text, str):
        return False
    normalized = "".join(text.strip().split())
    if not normalized or _NEGATED_PATTERN.search(normalized):
        return False
    return _HANDOFF_PATTERN.search(normalized) is not None
