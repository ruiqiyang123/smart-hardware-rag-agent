"""Evidence-bound, read-only diagnosis orchestration."""

import copy
import json
import math
import re
from ipaddress import ip_address
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Dict, List, Literal, Set
from urllib.parse import urlsplit

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field, field_validator, model_validator

from agent.orchestration.invoke import invoke_with_policy
from agent.nodes.prompt_loader import load_safe_prompt
from agent.orchestration.state import (
    DiagnosisResult,
    EvidenceItem,
    MissingField,
    RiskFlag,
    RiskLevel,
    StrictModel,
)
from agent.security.secrets import contains_unredacted_secret


_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "diagnosis_prompt.txt"
_MAX_INPUT_LENGTH = 10_000
_MAX_EVIDENCE_ITEMS = 16
_MAX_EVIDENCE_CONTENT = 32 * 1024
_TOOL_NAMES = frozenset(
    {"knowledge_search", "profile", "warranty", "chain_status"}
)
_CATEGORY_VALUES = frozenset(
    {
        "power",
        "usb_connection",
        "mobile_connection",
        "bluetooth_connection",
        "screen_buttons",
        "pin_lock",
        "firmware_repair",
        "backup_recovery",
        "device_loss_damage",
        "warranty_service",
        "transaction_boundary",
        "security_incident",
        "security_report",
        "other",
    }
)
_SECURITY_CATEGORIES = frozenset({"security_incident", "security_report"})
_CRITICAL_FLAGS = frozenset(
    {RiskFlag.SECRET_EXPOSURE, RiskFlag.PHISHING, RiskFlag.ASSET_LOSS}
)
_HIGH_FLAGS = frozenset(
    {
        RiskFlag.UNOFFICIAL_FIRMWARE,
        RiskFlag.ADDRESS_MISMATCH,
        RiskFlag.SUSPICIOUS_SIGNATURE,
        RiskFlag.DEVICE_AUTH_FAILURE,
        RiskFlag.REMOTE_CONTROL,
    }
)
_WARRANTY_QUERY = re.compile(r"[A-Za-z0-9]{4}", re.ASCII)
_CHAIN_ALIASES = {
    "BTC": "BTC",
    "BITCOIN": "BTC",
    "比特币": "BTC",
    "ETH": "ETH",
    "ETHEREUM": "ETH",
    "以太坊": "ETH",
    "SOL": "SOL",
    "SOLANA": "SOL",
    "BSC": "BSC",
    "BNB": "BSC",
    "BINANCE": "BSC",
    "币安": "BSC",
    "TRX": "TRX",
    "TRON": "TRX",
    "波场": "TRX",
    "ARB": "ARB",
    "ARBITRUM": "ARB",
    "MATIC": "MATIC",
    "POLYGON": "MATIC",
}

TOOLS_BY_CATEGORY = MappingProxyType(
    {
        "warranty_service": frozenset(
            {"knowledge_search", "profile", "warranty"}
        ),
        "transaction_boundary": frozenset(
            {"knowledge_search", "profile", "chain_status"}
        ),
        "security_incident": frozenset(),
        "security_report": frozenset(),
    }
)
DEFAULT_TOOLS = frozenset({"knowledge_search", "profile"})


def _has_forbidden_control(value: str, *, allow_formatting: bool = True) -> bool:
    return any(
        (ord(character) < 0x20 and (not allow_formatting or character not in {"\n", "\t"}))
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    )


def _safe_text(value: object, field_name: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{field_name} 非法")
    if _has_forbidden_control(value) or contains_unredacted_secret(value):
        raise ValueError(f"{field_name} 非法")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} 非法")
    return normalized


def _load_prompt(path: Path) -> str:
    return load_safe_prompt(path, "Diagnosis")


def _validate_execution_policy(timeout_seconds: object, retries: object) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds 必须是有限正数")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError("retries 必须是非负整数")


class ToolRequest(StrictModel):
    name: Literal["knowledge_search", "profile", "warranty", "chain_status"]
    query: str = Field(min_length=1, max_length=300)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if _has_forbidden_control(value, allow_formatting=False):
            raise ValueError("工具 query 非法")
        if contains_unredacted_secret(value):
            raise ValueError("工具 query 包含未脱敏内容")
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("工具 query 不能为空")
        return normalized


class DiagnosisPlan(StrictModel):
    requests: List[ToolRequest] = Field(max_length=4)

    @model_validator(mode="after")
    def validate_requests(self):
        names = [request.name for request in self.requests]
        pairs = [(request.name, request.query) for request in self.requests]
        if len(names) != len(set(names)) or len(pairs) != len(set(pairs)):
            raise ValueError("工具计划不得包含重复请求")
        return self


def allowed_tools(category: str) -> Set[str]:
    if not isinstance(category, str) or category not in _CATEGORY_VALUES:
        raise ValueError("未知 category")
    return set(TOOLS_BY_CATEGORY.get(category, DEFAULT_TOOLS))


def _risk_level(value: object) -> RiskLevel:
    if isinstance(value, RiskLevel):
        return value
    if not isinstance(value, str):
        raise TypeError("risk_level 必须是已知枚举")
    try:
        return RiskLevel(value)
    except ValueError:
        raise ValueError("risk_level 必须是已知枚举") from None


def _risk_flags(value: object) -> List[RiskFlag]:
    if not isinstance(value, list):
        raise TypeError("risk_flags 必须是列表")
    result: List[RiskFlag] = []
    for raw in value:
        if isinstance(raw, RiskFlag):
            flag = raw
        elif isinstance(raw, str):
            try:
                flag = RiskFlag(raw)
            except ValueError:
                raise ValueError("risk_flags 包含未知枚举") from None
        else:
            raise TypeError("risk_flags 包含非法值")
        if flag in result:
            raise ValueError("risk_flags 不得重复")
        result.append(flag)
    return result


def _missing_fields(value: object) -> List[MissingField]:
    if not isinstance(value, list):
        raise TypeError("missing_fields 必须是列表")
    result: List[MissingField] = []
    for raw in value:
        if isinstance(raw, MissingField):
            field = raw
        elif isinstance(raw, str):
            try:
                field = MissingField(raw)
            except ValueError:
                raise ValueError("missing_fields 包含未知枚举") from None
        else:
            raise TypeError("missing_fields 包含非法值")
        if field in result:
            raise ValueError("missing_fields 不得重复")
        result.append(field)
    return result


def _normalize_query(request: ToolRequest) -> ToolRequest:
    query = request.query
    if request.name == "warranty":
        if _WARRANTY_QUERY.fullmatch(query) is None:
            raise ValueError("warranty query 非法")
        query = query.upper()
    elif request.name == "chain_status":
        lookup = query.upper() if query.isascii() else query
        try:
            query = _CHAIN_ALIASES[lookup]
        except KeyError:
            raise ValueError("chain_status query 非法") from None
    return request.model_copy(update={"query": query})


def _validate_plan(raw: object) -> DiagnosisPlan:
    if isinstance(raw, DiagnosisPlan):
        raw = raw.model_dump(mode="python")
    if not isinstance(raw, dict):
        raise TypeError("Diagnosis plan 类型非法")
    return DiagnosisPlan.model_validate(raw)


def _terminal_result(
    outcome: Literal["need_user", "escalate"],
    summary: str,
    *,
    remaining_unknowns: List[MissingField] | None = None,
    tool_errors: List[str] | None = None,
) -> dict:
    result = DiagnosisResult(
        outcome=outcome,
        diagnosis_summary=summary,
        recommended_actions=[],
        evidence_refs=[],
        citations=[],
        draft_answer="",
        remaining_unknowns=remaining_unknowns or [],
    ).model_dump(mode="json")
    result["evidence"] = []
    result["tool_errors"] = list(tool_errors or [])
    return result


def _validate_evidence_item(raw: object) -> EvidenceItem:
    if isinstance(raw, EvidenceItem):
        raw = raw.model_dump(mode="python")
    if not isinstance(raw, dict):
        raise TypeError("工具证据类型非法")
    item = EvidenceItem.model_validate(raw)
    evidence_id = _safe_text(item.evidence_id, "evidence_id", max_length=128)
    if any(character.isspace() for character in evidence_id):
        raise ValueError("evidence_id 非法")
    content = item.content
    if (
        len(content) > _MAX_EVIDENCE_CONTENT
        or _has_forbidden_control(content)
        or contains_unredacted_secret(content)
    ):
        raise ValueError("evidence content 非法")
    source_title = _safe_text(item.source_title, "source_title", max_length=500)
    source_url = item.source_url
    if source_url is not None:
        source_url = _safe_text(source_url, "source_url", max_length=2_000)
        _validate_source_url(source_url)
    return item.model_copy(
        update={
            "evidence_id": evidence_id,
            "content": content,
            "source_title": source_title,
            "source_url": source_url,
        }
    )


def _validate_source_url(source_url: str) -> None:
    if any(character.isspace() for character in source_url) or "\\" in source_url:
        raise ValueError("source_url 非法")
    if re.search(r"%(?:00|09|0a|0d|1[0-9a-f]|7f)", source_url, re.IGNORECASE):
        raise ValueError("source_url 非法")
    try:
        parsed = urlsplit(source_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("source_url 非法") from None
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ValueError("source_url 非法")
    try:
        ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("source_url 非法")
    try:
        ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("source_url 非法") from None
    labels = ascii_hostname.split(".")
    if len(ascii_hostname) > 253 or len(labels) < 2 or any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("source_url 非法")


def _validate_answer(raw: object, evidence_by_id: Dict[str, EvidenceItem]) -> DiagnosisResult:
    if isinstance(raw, DiagnosisResult):
        raw = raw.model_dump(mode="python")
    if not isinstance(raw, dict):
        raise TypeError("Diagnosis 输出类型非法")
    result = DiagnosisResult.model_validate(raw)
    summary = _safe_text(result.diagnosis_summary, "diagnosis_summary", max_length=500)
    draft = result.draft_answer
    if draft:
        draft = _safe_text(draft, "draft_answer", max_length=2_000)
    actions = [
        action.model_copy(
            update={
                "text": _safe_text(action.text, "action text", max_length=300)
            }
        )
        for action in result.recommended_actions
    ]

    actual_ids = set(evidence_by_id)
    if not set(result.evidence_refs).issubset(actual_ids):
        raise ValueError("Diagnosis 引用了未知证据")
    citation_by_id = {citation.source_id: citation for citation in result.citations}
    for citation in result.citations:
        source = evidence_by_id.get(citation.source_id)
        if (
            source is None
            or source.source_url is None
            or citation.source_title != source.source_title
            or citation.source_url != source.source_url
        ):
            raise ValueError("Citation metadata 不匹配")
    for evidence_id in result.evidence_refs:
        source = evidence_by_id[evidence_id]
        if source.kind == "knowledge" and source.source_url is not None:
            if evidence_id not in citation_by_id:
                raise ValueError("知识证据缺少 citation")
    return result.model_copy(
        update={
            "diagnosis_summary": summary,
            "recommended_actions": actions,
            "draft_answer": draft,
        }
    )


class DiagnosisAgent:
    """Plan bounded read-only tools and draft only from validated evidence."""

    def __init__(
        self,
        model: object | None = None,
        tool_registry: Dict[str, Callable[[dict, str], List[object]]] | None = None,
        *,
        plan_runner: object | None = None,
        answer_runner: object | None = None,
        timeout_seconds: float = 20,
        retries: int = 1,
    ) -> None:
        model_mode = model is not None and plan_runner is None and answer_runner is None
        runners_mode = model is None and plan_runner is not None and answer_runner is not None
        if not (model_mode or runners_mode):
            raise ValueError("DiagnosisAgent 必须提供 model 或一对 runners")
        _validate_execution_policy(timeout_seconds, retries)
        if model_mode:
            factory = getattr(model, "with_structured_output", None)
            if not callable(factory):
                raise TypeError("model 不支持结构化输出")
            plan_runner = factory(DiagnosisPlan, method="function_calling")
            answer_runner = factory(DiagnosisResult, method="function_calling")
        if not callable(getattr(plan_runner, "invoke", None)) or not callable(
            getattr(answer_runner, "invoke", None)
        ):
            raise TypeError("runner 必须提供 invoke")
        if not isinstance(tool_registry, dict) or not tool_registry:
            raise ValueError("tool_registry 必须是非空映射")
        if any(
            not isinstance(name, str)
            or name not in _TOOL_NAMES
            or not callable(tool)
            for name, tool in tool_registry.items()
        ):
            raise ValueError("tool_registry 非法")

        self.plan_runner = plan_runner
        self.answer_runner = answer_runner
        self.tool_registry = dict(tool_registry)
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.prompt = _load_prompt(_PROMPT_PATH)

    def run(self, state: dict) -> dict:
        if not isinstance(state, dict):
            raise TypeError("state 必须是对象")
        required = (
            "sanitized_input",
            "category",
            "risk_level",
            "risk_flags",
            "missing_fields",
            "requires_human",
        )
        if any(field not in state for field in required):
            raise ValueError("state 缺少 Diagnosis 必要字段")
        sanitized_input = _safe_text(
            state["sanitized_input"], "sanitized_input", max_length=_MAX_INPUT_LENGTH
        )
        category = state["category"]
        allowed = allowed_tools(category)
        risk_level = _risk_level(state["risk_level"])
        risk_flags = _risk_flags(state["risk_flags"])
        missing_fields = _missing_fields(state["missing_fields"])
        requires_human = state["requires_human"]
        if not isinstance(requires_human, bool):
            raise TypeError("requires_human 必须是布尔值")

        flags = set(risk_flags)
        unsafe = (
            risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}
            or bool(flags & (_CRITICAL_FLAGS | _HIGH_FLAGS))
            or requires_human
            or category in _SECURITY_CATEGORIES
        )
        if unsafe:
            return _terminal_result("escalate", "该工单需要人工安全处理")
        if missing_fields:
            return _terminal_result(
                "need_user",
                "需要补充必要信息",
                remaining_unknowns=missing_fields,
            )

        plan_payload = {
            "allowed_tools": sorted(allowed),
            "category": category,
            "sanitized_input": sanitized_input,
            "missing_fields": [],
        }
        plan_messages = [
            SystemMessage(content=self.prompt),
            HumanMessage(
                content=json.dumps(
                    plan_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            ),
        ]
        plan = invoke_with_policy(
            lambda: _validate_plan(self.plan_runner.invoke(plan_messages)),
            self.timeout_seconds,
            self.retries,
        )

        requests: List[ToolRequest] = []
        for request in plan.requests:
            if request.name not in allowed:
                raise ValueError("工具计划违反 category 白名单")
            if request.name not in self.tool_registry:
                raise ValueError("工具计划请求未注册工具")
            requests.append(_normalize_query(request))

        evidence_by_id: Dict[str, EvidenceItem] = {}
        candidate_count = 0
        total_content = 0
        for request in requests:
            tool = self.tool_registry[request.name]
            try:
                raw_items = invoke_with_policy(
                    lambda tool=tool, request=request: tool(
                        copy.deepcopy(state), request.query
                    ),
                    self.timeout_seconds,
                    self.retries,
                )
            except Exception:
                return _terminal_result(
                    "escalate",
                    "只读取证失败，需要人工处理",
                    tool_errors=[f"tool_failure:{request.name}"],
                )
            if not isinstance(raw_items, list):
                raise ValueError("工具证据必须是列表")
            candidate_count += len(raw_items)
            if candidate_count > _MAX_EVIDENCE_ITEMS:
                raise ValueError("工具证据数量超限")
            for raw_item in raw_items:
                item = _validate_evidence_item(raw_item)
                total_content += len(item.content)
                if total_content > _MAX_EVIDENCE_CONTENT:
                    raise ValueError("工具证据内容超限")
                existing = evidence_by_id.get(item.evidence_id)
                if existing is None:
                    evidence_by_id[item.evidence_id] = item
                elif existing != item:
                    raise ValueError("工具证据 ID 冲突")

        if not evidence_by_id:
            return _terminal_result(
                "escalate",
                "没有可验证证据，需要人工处理",
                tool_errors=["no_evidence"],
            )
        if len(evidence_by_id) > _MAX_EVIDENCE_ITEMS:
            raise ValueError("工具证据数量超限")

        evidence_json = [
            item.model_dump(mode="json") for item in evidence_by_id.values()
        ]
        answer_payload = {
            "sanitized_input": sanitized_input,
            "triage": {
                "category": category,
                "risk_level": risk_level.value,
                "risk_flags": [flag.value for flag in risk_flags],
            },
            "evidence": evidence_json,
        }
        answer_messages = [
            SystemMessage(content=self.prompt),
            HumanMessage(
                content=json.dumps(
                    answer_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            ),
        ]
        result = invoke_with_policy(
            lambda: _validate_answer(
                self.answer_runner.invoke(answer_messages), evidence_by_id
            ),
            self.timeout_seconds,
            self.retries,
        )
        output = result.model_dump(mode="json")
        output["evidence"] = evidence_json
        output["tool_errors"] = []
        return output
