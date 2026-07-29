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
    r"[^\s,，;；\x00]{8,256}",
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


def _secret_spans(value: str) -> List[Tuple[int, int]]:
    candidate = _REDACTED_PATTERN.sub(
        lambda match: "\x00" * len(match.group(0)), value
    )
    spans: List[Tuple[int, int]] = []
    patterns: Sequence[re.Pattern] = (
        _HEX_PRIVATE_KEY_PATTERN,
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
