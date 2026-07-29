import re
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Iterable, List, Sequence, Tuple


_REDACTED_PATTERN = re.compile(r"\[REDACTED_[A-Z0-9_]+\]", re.IGNORECASE)
_WORD_PATTERN = re.compile(r"[A-Za-z]+", re.ASCII)
_HEX_PRIVATE_KEY_PATTERN = re.compile(
    r"(?<![0-9A-Fa-f])(?:0[xX])?[0-9A-Fa-f]{64}(?![0-9A-Fa-f])"
)
_TRANSACTION_HASH_PATTERN = re.compile(r"(?:0[xX])?[0-9A-Fa-f]{64}")
_LABELED_TRANSACTION_HASH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:transaction hash|txid|交易哈希|交易ID)"
    r"\s*(?::|：)?\s*(?:0[xX])?[0-9A-Fa-f]{64}(?![0-9A-Fa-f])",
    re.IGNORECASE,
)
_DANGEROUS_SECRET_CONTEXT_PATTERN = re.compile(
    r"\b(?:private[ _-]?key|seed[ _-]?phrase|mnemonic|pass[ _-]?phrase|"
    r"pin|password)\b|"
    r"私钥|助记词|密码|口令",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY_PATTERN = re.compile(r"[.!?。！？;；]")
_TRANSACTION_CONTEXT_RADIUS = 128
_WIF_PATTERN = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])[5KL][1-9A-HJ-NP-Za-km-z]{50,51}"
    r"(?![1-9A-HJ-NP-Za-km-z])"
)
_PIN_PATTERN = re.compile(
    r"\bpin\s*(?:(?::|=|：|是|为)\s*)?\d{4,8}\b",
    re.IGNORECASE,
)
_PASSPHRASE_PATTERN = re.compile(
    r"(?:passphrase|password|seed[_ -]?phrase|mnemonic|助记词|密码|口令)"
    r"\s*(?::|=|：|是|为)\s*[^\s\n\r\x00][^\n\r\x00]{0,499}",
    re.IGNORECASE,
)
_LABELED_PRIVATE_KEY_PATTERN = re.compile(
    r"(?:private[_ -]?key|私钥)\s*(?::|=|：|是|为)\s*"
    r"[^\s,，;；\x00]{1,256}",
    re.IGNORECASE,
)
_BIP39_SEPARATOR_PATTERN = re.compile(r"[\s,，;；]+")
_BIP39_WORD_COUNTS = frozenset({12, 15, 18, 21, 24})
_DEFAULT_REDACTION = "[REDACTED_SECRET]"


def _load_bip39_words() -> FrozenSet[str]:
    # Vendored verbatim from bitcoin/bips bip-0039/english.txt (BSD-2-Clause):
    # https://github.com/bitcoin/bips/blob/master/bip-0039/english.txt
    path = Path(__file__).with_name("bip39_english.txt")
    try:
        words = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError("BIP-39 英文词表不可用") from error
    if len(words) != 2048 or len(set(words)) != 2048:
        raise RuntimeError("BIP-39 英文词表不完整")
    return frozenset(words)


_BIP39_WORDS = _load_bip39_words()


@dataclass(frozen=True)
class SanitizedText:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("sanitized text 必须是非空字符串")
        if contains_unredacted_secret(self.value):
            raise ValueError("检测到未脱敏的敏感信息")


@dataclass(frozen=True)
class TransactionHash:
    """Explicitly typed chain transaction hash for trusted persistence fields."""

    value: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, str)
            or _TRANSACTION_HASH_PATTERN.fullmatch(self.value) is None
        ):
            raise ValueError("transaction hash 必须是 64 位十六进制字符串")


def _bip39_spans(value: str) -> List[Tuple[int, int]]:
    """Return spans for BIP-39 word runs embedded in otherwise normal text."""
    matches = list(_WORD_PATTERN.finditer(value))
    spans: List[Tuple[int, int]] = []
    run: List[re.Match] = []

    def flush() -> None:
        nonlocal run
        while len(run) >= min(_BIP39_WORD_COUNTS):
            word_count = max(
                count for count in _BIP39_WORD_COUNTS if count <= len(run)
            )
            spans.append((run[0].start(), run[word_count - 1].end()))
            run = run[word_count:]
        run = []

    for match in matches:
        word = match.group(0).lower()
        if word not in _BIP39_WORDS:
            flush()
            continue
        if run:
            separator = value[run[-1].end() : match.start()]
            if _BIP39_SEPARATOR_PATTERN.fullmatch(separator) is None:
                flush()
        run.append(match)
    flush()
    return spans


def _merge_spans(spans: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    ordered = sorted(set(spans))
    merged: List[List[int]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _spans_overlap(left: Tuple[int, int], right: Tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _mask_spans(value: str, spans: Iterable[Tuple[int, int]]) -> str:
    characters = list(value)
    for start, end in spans:
        characters[start:end] = "\x00" * (end - start)
    return "".join(characters)


def _limit_local_context(value: str, start: int, end: int) -> str:
    """Bound context to 128 chars, one sentence, and at most one newline per side."""
    left = value[max(0, start - _TRANSACTION_CONTEXT_RADIUS) : start]
    right = value[end : min(len(value), end + _TRANSACTION_CONTEXT_RADIUS)]

    left_boundaries = list(_SENTENCE_BOUNDARY_PATTERN.finditer(left))
    if left_boundaries:
        left = left[left_boundaries[-1].end() :]
    right_boundary = _SENTENCE_BOUNDARY_PATTERN.search(right)
    if right_boundary:
        right = right[: right_boundary.start()]

    left_newlines = [index for index, character in enumerate(left) if character == "\n"]
    if len(left_newlines) > 1:
        left = left[left_newlines[-2] + 1 :]
    right_newlines = [
        index for index, character in enumerate(right) if character == "\n"
    ]
    if len(right_newlines) > 1:
        right = right[: right_newlines[1]]
    return left + value[start:end] + right


def _has_dangerous_transaction_context(
    value: str,
    span: Tuple[int, int],
) -> bool:
    context = _limit_local_context(value, *span)
    return _DANGEROUS_SECRET_CONTEXT_PATTERN.search(context) is not None


def _secret_spans(value: str) -> List[Tuple[int, int]]:
    candidate = _REDACTED_PATTERN.sub(
        lambda match: "\x00" * len(match.group(0)), value
    )
    spans: List[Tuple[int, int]] = []
    patterns: Sequence[re.Pattern] = (
        _WIF_PATTERN,
        _PIN_PATTERN,
        _PASSPHRASE_PATTERN,
        _LABELED_PRIVATE_KEY_PATTERN,
    )
    for pattern in patterns:
        spans.extend(
            (match.start(), match.end()) for match in pattern.finditer(candidate)
        )
    spans.extend(_bip39_spans(candidate))

    labeled_hash_spans = [
        (match.start(), match.end())
        for match in _LABELED_TRANSACTION_HASH_PATTERN.finditer(candidate)
    ]
    safe_hash_spans: List[Tuple[int, int]] = []
    for labeled_hash_span in labeled_hash_spans:
        if _has_dangerous_transaction_context(
            candidate, labeled_hash_span
        ) or any(_spans_overlap(labeled_hash_span, span) for span in spans):
            spans.append(labeled_hash_span)
        else:
            safe_hash_spans.append(labeled_hash_span)

    private_key_candidate = _mask_spans(candidate, safe_hash_spans)
    spans.extend(
        (match.start(), match.end())
        for match in _HEX_PRIVATE_KEY_PATTERN.finditer(private_key_candidate)
    )
    return _merge_spans(spans)


def is_fully_redacted(value: str) -> bool:
    return (
        isinstance(value, str)
        and _REDACTED_PATTERN.fullmatch(value.strip()) is not None
    )


def contains_unredacted_secret(value: str) -> bool:
    if not isinstance(value, str):
        return False
    return bool(_secret_spans(value))


def redact_unredacted_secrets(
    value: str,
    replacement: str = _DEFAULT_REDACTION,
) -> str:
    """Redact every secret recognized by ``contains_unredacted_secret``."""
    if not isinstance(value, str):
        raise TypeError("待脱敏内容必须是字符串")
    if not isinstance(replacement, str) or not replacement:
        raise ValueError("脱敏占位符不能为空")
    spans = _secret_spans(value)
    if not spans:
        return value
    parts: List[str] = []
    cursor = 0
    for start, end in spans:
        parts.extend((value[cursor:start], replacement))
        cursor = end
    parts.append(value[cursor:])
    return "".join(parts)
