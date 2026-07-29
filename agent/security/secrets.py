import re
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, List


_REDACTED_PATTERN = re.compile(r"\[REDACTED_[A-Z0-9_]+\]", re.IGNORECASE)
_WORD_PATTERN = re.compile(r"[A-Za-z]+", re.ASCII)
_HEX_PRIVATE_KEY_PATTERN = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])"
)
_WIF_PATTERN = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])[5KL][1-9A-HJ-NP-Za-km-z]{50,51}"
    r"(?![1-9A-HJ-NP-Za-km-z])"
)
_PIN_PATTERN = re.compile(r"\bpin\s*[:=：]\s*\d{4,8}\b", re.IGNORECASE)
_PASSPHRASE_PATTERN = re.compile(
    r"(?:passphrase|password|seed[_ -]?phrase|mnemonic|助记词|密码|口令)"
    r"\s*[:=：]\s*\S+",
    re.IGNORECASE,
)
_MNEMONIC_LABEL_PATTERN = re.compile(
    r"(?:seed[_ -]?phrase|recovery[_ -]?phrase|mnemonic|助记词)\s*[:=：]"
    r"(?P<phrase>[^\n\r]+)",
    re.IGNORECASE,
)


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


def _words(value: str) -> List[str]:
    return [match.group(0).lower() for match in _WORD_PATTERN.finditer(value)]


def _is_bip39_phrase(value: str) -> bool:
    words = _words(value)
    return len(words) in {12, 24} and all(word in _BIP39_WORDS for word in words)


def is_fully_redacted(value: str) -> bool:
    return (
        isinstance(value, str)
        and _REDACTED_PATTERN.fullmatch(value.strip()) is not None
    )


def contains_unredacted_secret(value: str) -> bool:
    if not isinstance(value, str):
        return False
    candidate = _REDACTED_PATTERN.sub("", value)
    if any(
        pattern.search(candidate)
        for pattern in (
            _HEX_PRIVATE_KEY_PATTERN,
            _WIF_PATTERN,
            _PIN_PATTERN,
            _PASSPHRASE_PATTERN,
        )
    ):
        return True
    if _is_bip39_phrase(candidate.strip(" \t\r\n,;，；")):
        return True
    return any(
        _is_bip39_phrase(match.group("phrase"))
        for match in _MNEMONIC_LABEL_PATTERN.finditer(candidate)
    )
