"""Central provenance policy for evidence and citation URLs."""

import re
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import unquote, urlsplit


DEFAULT_TRUSTED_EVIDENCE_DOMAINS = frozenset(
    {
        "ledger.com",
        "trezor.io",
        "ethereum.org",
        "eips.ethereum.org",
        "bitcoincore.org",
        "docs.walletconnect.network",
        "support.metamask.io",
    }
)
DEFAULT_TRUSTED_EVIDENCE_URL_PREFIXES = (
    "https://github.com/bitcoin/bitcoin/",
    "https://github.com/bitcoin/bips/",
)
_HOSTNAME_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_ENCODED_CONTROL_PATTERN = re.compile(
    r"%(?:00|09|0a|0d|1[0-9a-f]|7f)", re.IGNORECASE
)
_PATH_SCOPED_HOSTS = frozenset({"github.com"})


@dataclass(frozen=True)
class _ParsedHttpsUrl:
    hostname: str
    path: str


def _validated_collection(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise TypeError(f"{field_name} 必须是字符串集合")
    items = tuple(value)
    if any(not isinstance(item, str) for item in items):
        raise TypeError(f"{field_name} 必须是字符串集合")
    if len(items) != len(set(items)):
        raise ValueError(f"{field_name} 不得重复")
    return items


def _parse_safe_https_url(url: object) -> _ParsedHttpsUrl | None:
    if (
        not isinstance(url, str)
        or not url
        or any(character.isspace() for character in url)
        or "\\" in url
        or _ENCODED_CONTROL_PATTERN.search(url)
    ):
        return None
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    try:
        ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if _HOSTNAME_PATTERN.fullmatch(ascii_hostname) is None:
        return None
    try:
        ip_address(ascii_hostname)
    except ValueError:
        pass
    else:
        return None
    decoded_path = unquote(parsed.path)
    if (
        "\\" in decoded_path
        or any(ord(character) < 0x20 for character in decoded_path)
        or any(part in {".", ".."} for part in decoded_path.split("/"))
    ):
        return None
    return _ParsedHttpsUrl(hostname=ascii_hostname, path=decoded_path or "/")


class TrustedSourcePolicy:
    """Validate HTTPS URLs against narrow hosts and repository URL prefixes."""

    def __init__(
        self,
        trusted_domains: object | None = None,
        trusted_url_prefixes: object | None = None,
    ) -> None:
        raw_domains = (
            DEFAULT_TRUSTED_EVIDENCE_DOMAINS
            if trusted_domains is None
            else trusted_domains
        )
        domains = _validated_collection(raw_domains, "trusted_domains")
        if not domains:
            raise ValueError("trusted_domains 不能为空")
        if any(_HOSTNAME_PATTERN.fullmatch(domain) is None for domain in domains):
            raise ValueError("trusted_domains 包含非规范 hostname")
        if any(
            domain == host or domain.endswith("." + host)
            for domain in domains
            for host in _PATH_SCOPED_HOSTS
        ):
            raise ValueError("多租户域必须使用 trusted_url_prefixes")

        raw_prefixes = (
            DEFAULT_TRUSTED_EVIDENCE_URL_PREFIXES
            if trusted_url_prefixes is None
            else trusted_url_prefixes
        )
        prefixes = _validated_collection(raw_prefixes, "trusted_url_prefixes")
        parsed_prefixes: list[tuple[str, str]] = []
        for prefix in prefixes:
            parsed = _parse_safe_https_url(prefix)
            split = urlsplit(prefix)
            if (
                parsed is None
                or split.query
                or split.fragment
                or not parsed.path.endswith("/")
            ):
                raise ValueError("trusted_url_prefixes 包含非法 URL prefix")
            parsed_prefixes.append((parsed.hostname, parsed.path))

        self.trusted_domains = frozenset(domains)
        self.trusted_url_prefixes = tuple(parsed_prefixes)

    @staticmethod
    def is_safe_https_url(url: object) -> bool:
        return _parse_safe_https_url(url) is not None

    def is_trusted(self, url: object) -> bool:
        parsed = _parse_safe_https_url(url)
        if parsed is None:
            return False
        if any(
            parsed.hostname == domain
            or parsed.hostname.endswith("." + domain)
            for domain in self.trusted_domains
        ):
            return True
        return any(
            parsed.hostname == hostname and parsed.path.startswith(path_prefix)
            for hostname, path_prefix in self.trusted_url_prefixes
        )
