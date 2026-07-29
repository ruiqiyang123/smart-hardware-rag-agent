import re
from dataclasses import dataclass
from typing import List, Tuple
from urllib.parse import urlparse

from agent.security.secrets import (
    contains_unredacted_secret,
    redact_unredacted_secrets,
)
from agent.security.trusted_sources import TrustedSourcePolicy


MINIMAL_FAILURE_NOTICE = (
    "系统暂时无法安全处理此请求。请不要继续分享助记词、私钥、PIN 或 "
    "Passphrase；请仅从设备厂商官方网站进入支持渠道。"
)

_DOMAIN_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_URL_PATTERN = re.compile(
    r"[a-z][a-z0-9+.-]*://[^\s<>()\[\]{}\"']+", re.IGNORECASE
)
_TRAILING_URL_PUNCTUATION = ".,，。;；:：!！?？"

_RISK_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    (
        "phishing",
        re.compile(
            r"钓鱼|假官网|仿冒官网|陌生客服|假客服|冒充客服|"
            r"phishing|fake\s+(?:support|website)",
            re.IGNORECASE,
        ),
    ),
    (
        "remote_control",
        re.compile(
            r"远程控制|共享屏幕|remote\s+control|screen\s+shar(?:e|ing)",
            re.IGNORECASE,
        ),
    ),
    (
        "unofficial_firmware",
        re.compile(
            r"非官方固件|第三方固件|unofficial\s+firmware|"
            r"third[- ]party\s+firmware",
            re.IGNORECASE,
        ),
    ),
    (
        "address_mismatch",
        re.compile(
            r"地址不一致|地址被替换|address\s+mismatch|wrong\s+address",
            re.IGNORECASE,
        ),
    ),
    (
        "suspicious_signature",
        re.compile(
            r"可疑签名|陌生签名|未知签名|签名.{0,8}(?:不一致|异常)|"
            r"suspicious\s+sign(?:ature|ing)",
            re.IGNORECASE,
        ),
    ),
    (
        "asset_loss",
        re.compile(
            r"资产(?:已)?(?:丢失|转出|被盗)|盗币|资金被盗|"
            r"asset\s+loss|funds?\s+(?:were\s+)?stolen|unauthorized\s+transfer",
            re.IGNORECASE,
        ),
    ),
)

_CRITICAL_FLAGS = frozenset({"secret_exposure", "phishing", "asset_loss"})
_HIGH_FLAGS = frozenset(
    {
        "unofficial_firmware",
        "address_mismatch",
        "suspicious_signature",
        "remote_control",
    }
)

_UNSAFE_ACTION_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(
        r"(?:请|把|将|需要|向我|给我|可以)(?:你)?(?:发送|提供|告诉|提交|输入|上传)"
        r".{0,12}(?:助记词|私钥|PIN|Passphrase|密码|seed\s+phrase|private\s+key)|"
        r"(?:send|provide|share|tell|enter|upload).{0,20}"
        r"(?:seed\s+phrase|private\s+key|PIN|passphrase|password)",
        re.IGNORECASE,
    ),
    re.compile(r"破解\s*PIN|bypass\s+(?:the\s+)?PIN", re.IGNORECASE),
    re.compile(
        r"保证(?:追回|找回|赔付|退款)|一定(?:追回|找回|赔付|退款)|"
        r"guarantee.{0,16}(?:recover|refund|compensat)",
        re.IGNORECASE,
    ),
    re.compile(
        r"远程控制(?:你的|用户的)?设备|替你(?:签名|转账|授权)|"
        r"remote(?:ly)?\s+control|sign\s+(?:for|on\s+behalf)|"
        r"transfer\s+(?:funds|assets)\s+for\s+you",
        re.IGNORECASE,
    ),
    re.compile(
        r"第三方(?:恢复工具|资产恢复服务)|非官方固件|"
        r"third[- ]party\s+(?:recovery|firmware)",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class SanitizationResult:
    sanitized_input: str
    risk_level: str
    risk_flags: List[str]
    critical_notice: str


@dataclass(frozen=True)
class PolicyDecision:
    passed: bool
    reason_codes: List[str]


def _validate_policy(policy: object) -> Tuple[Tuple[str, ...], str]:
    if not isinstance(policy, dict):
        raise TypeError("安全策略必须是对象")
    if "policy_version" not in policy:
        raise ValueError("安全策略缺少 policy_version")
    if "official_domains" not in policy:
        raise ValueError("安全策略缺少 official_domains")
    if "critical_response_template_zh" not in policy:
        raise ValueError("安全策略缺少 critical_response_template_zh")

    policy_version = policy["policy_version"]
    if not isinstance(policy_version, str):
        raise TypeError("policy_version 必须是字符串")
    if not policy_version.strip():
        raise ValueError("policy_version 不能为空")

    domains = policy["official_domains"]
    if not isinstance(domains, list):
        raise TypeError("official_domains 必须是字符串列表")
    if not domains:
        raise ValueError("official_domains 不能为空")
    validated_domains: List[str] = []
    for domain in domains:
        if not isinstance(domain, str):
            raise TypeError("official_domains 必须是字符串列表")
        if not domain or _DOMAIN_PATTERN.fullmatch(domain) is None:
            raise ValueError("official_domains 包含非法域名")
        validated_domains.append(domain)
    if len(validated_domains) != len(set(validated_domains)):
        raise ValueError("official_domains 不得包含重复域名")

    template = policy["critical_response_template_zh"]
    if not isinstance(template, str):
        raise TypeError("critical_response_template_zh 必须是字符串")
    template = template.strip()
    if not template:
        raise ValueError("critical_response_template_zh 不能为空")
    if contains_unredacted_secret(template):
        raise ValueError("critical_response_template_zh 包含敏感信息")
    return tuple(validated_domains), template


class IngressGuard:
    def __init__(self, policy: dict):
        _, self.critical_notice = _validate_policy(policy)

    def sanitize(self, raw_input: str) -> SanitizationResult:
        if not isinstance(raw_input, str) or not raw_input.strip():
            raise ValueError("输入必须是非空字符串")

        flags: List[str] = []
        if contains_unredacted_secret(raw_input):
            flags.append("secret_exposure")
        flags.extend(
            flag for flag, pattern in _RISK_PATTERNS if pattern.search(raw_input)
        )
        flags = list(dict.fromkeys(flags))

        sanitized = redact_unredacted_secrets(raw_input).strip()
        if contains_unredacted_secret(sanitized):
            raise ValueError("输入无法完成安全脱敏")

        flag_set = set(flags)
        if flag_set & _CRITICAL_FLAGS:
            risk_level = "critical"
        elif flag_set & _HIGH_FLAGS:
            risk_level = "high"
        else:
            risk_level = "low"
        return SanitizationResult(
            sanitized_input=sanitized,
            risk_level=risk_level,
            risk_flags=flags,
            critical_notice=self.critical_notice if risk_level == "critical" else "",
        )


class PolicyGuard:
    def __init__(
        self,
        policy: dict,
        trusted_source_policy: TrustedSourcePolicy | None = None,
    ):
        domains, _ = _validate_policy(policy)
        if trusted_source_policy is not None and not isinstance(
            trusted_source_policy, TrustedSourcePolicy
        ):
            raise TypeError("trusted_source_policy 类型非法")
        self.official_domains = frozenset(domains)
        self.trusted_sources = trusted_source_policy or TrustedSourcePolicy()

    @staticmethod
    def _clean_url(url: str) -> str:
        return url.rstrip(_TRAILING_URL_PUNCTUATION)

    @staticmethod
    def _safe_https_url(url: str) -> bool:
        return TrustedSourcePolicy.is_safe_https_url(url)

    def _official_url(self, url: str) -> bool:
        if not self._safe_https_url(url):
            return False
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        return any(
            host == domain or host.endswith("." + domain)
            for domain in self.official_domains
        )

    def evaluate(self, text: str, citation_urls: List[str]) -> PolicyDecision:
        if (
            not isinstance(text, str)
            or not text.strip()
            or not isinstance(citation_urls, list)
            or any(not isinstance(url, str) or not url for url in citation_urls)
        ):
            return PolicyDecision(passed=False, reason_codes=["invalid_input"])

        reasons: List[str] = []
        if contains_unredacted_secret(text):
            reasons.append("secret_exposure")
        if any(pattern.search(text) for pattern in _UNSAFE_ACTION_PATTERNS):
            reasons.append("unsafe_action")

        safe_citations = {
            url for url in citation_urls if self.trusted_sources.is_trusted(url)
        }
        if len(safe_citations) != len(citation_urls):
            reasons.append("official_source_violation")
        for match in _URL_PATTERN.finditer(text):
            url = self._clean_url(match.group(0))
            if url in safe_citations or self._official_url(url):
                continue
            reasons.append("official_source_violation")

        reason_codes = list(dict.fromkeys(reasons))
        return PolicyDecision(passed=not reason_codes, reason_codes=reason_codes)
