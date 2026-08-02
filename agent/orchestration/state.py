from enum import Enum
from operator import add
from typing import Annotated, Dict, List, Literal, Optional, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import TypedDict


EvidenceRef = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
ReviewItem = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]
JSONScalar: TypeAlias = Union[str, int, float, bool, None]
JSONValue: TypeAlias = Union[
    JSONScalar,
    List["JSONValue"],
    Dict[str, "JSONValue"],
]


def _reject_duplicates(values: list, field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} 不得包含重复值")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Status(str, Enum):
    NEW = "new"
    TRIAGED = "triaged"
    DIAGNOSING = "diagnosing"
    REVIEWING = "reviewing"
    PENDING_USER = "pending_user"
    ESCALATED = "escalated"
    CLOSED = "closed"
    RESOLVED = "resolved"


class WaitingReason(str, Enum):
    CLARIFICATION = "clarification"
    MISSING_INFORMATION = "missing_information"
    RESOLUTION_CONFIRMATION = "resolution_confirmation"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskFlag(str, Enum):
    SECRET_EXPOSURE = "secret_exposure"
    PHISHING = "phishing"
    ASSET_LOSS = "asset_loss"
    UNOFFICIAL_FIRMWARE = "unofficial_firmware"
    ADDRESS_MISMATCH = "address_mismatch"
    SUSPICIOUS_SIGNATURE = "suspicious_signature"
    DEVICE_AUTH_FAILURE = "device_auth_failure"
    REMOTE_CONTROL = "remote_control"


class MissingField(str, Enum):
    DEVICE_MODEL = "device_model"
    APP_OS = "app_os"
    CONNECTION_TYPE = "connection_type"
    FIRMWARE_VERSION = "firmware_version"
    ERROR_STATE = "error_state"
    SERIAL_LAST4 = "serial_last4"
    PURCHASE_DATE = "purchase_date"
    TRANSACTION_HASH = "transaction_hash"
    CHAIN_NAME = "chain_name"


class EvidenceItem(StrictModel):
    evidence_id: EvidenceRef
    kind: Literal["knowledge", "profile", "device", "warranty", "chain"]
    content: str = Field(min_length=1)
    source_title: str = Field(min_length=1)
    source_url: Optional[str] = None


class Citation(StrictModel):
    source_id: EvidenceRef
    source_title: str = Field(min_length=1)
    source_url: str = Field(min_length=1)


class TriageResult(StrictModel):
    intent: Literal[
        "troubleshoot",
        "recovery",
        "warranty",
        "transaction_boundary",
        "security_incident",
        "security_report",
        "other",
    ]
    category: Literal[
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
    ]
    priority: Literal["P0", "P1", "P2"]
    risk_level: RiskLevel
    risk_flags: List[RiskFlag]
    clarity: Literal["ambiguous", "partial", "clear"] = Field(
        description=(
            "无法确定唯一问题方向时为 ambiguous；意图明确但缺少精确诊断字段时为 "
            "partial；问题足以直接诊断时为 clear。"
        )
    )
    clarification_question: str = Field(max_length=300)
    clarification_options: List[str] = Field(max_length=5)
    missing_fields: List[MissingField] = Field(
        description=(
            "只能包含输入 required_fields 中当前 category 对应列表里的字段；"
            "如果当前分类未出现在 required_fields 映射中，必须返回空数组。"
        )
    )
    suggested_route: Literal["clarify", "diagnose", "escalate"] = Field(
        description=(
            "ambiguous 使用 clarify；partial 和 clear 的低风险问题使用 diagnose；"
            "高风险问题使用 escalate。"
        )
    )
    summary: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_risk_consistency(self):
        _reject_duplicates(self.risk_flags, "risk_flags")
        _reject_duplicates(self.missing_fields, "missing_fields")
        _reject_duplicates(self.clarification_options, "clarification_options")

        if self.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            if self.suggested_route != "escalate":
                raise ValueError("high/critical 风险必须升级人工处理")
        elif self.category in {"security_incident", "security_report"}:
            if (
                self.suggested_route not in {"diagnose", "escalate"}
                or self.clarity != "clear"
                or self.missing_fields
                or self.clarification_question
                or self.clarification_options
            ):
                raise ValueError("安全事件不得等待澄清")
        elif self.clarity == "ambiguous":
            if (
                self.suggested_route != "clarify"
                or self.missing_fields
                or not self.clarification_question
                or not 2 <= len(self.clarification_options) <= 5
            ):
                raise ValueError("ambiguous 必须提供单个澄清问题和 2-5 个选项")
        elif self.clarity == "partial":
            if (
                self.suggested_route != "diagnose"
                or not self.missing_fields
                or self.clarification_question
                or self.clarification_options
            ):
                raise ValueError("partial 必须带缺失字段并继续诊断")
        elif (
            self.suggested_route != "diagnose"
            or self.missing_fields
            or self.clarification_question
            or self.clarification_options
        ):
            raise ValueError("clear 必须直接诊断且不得携带澄清字段")
        if self.risk_level == RiskLevel.CRITICAL and self.priority != "P0":
            raise ValueError("critical 风险必须使用 P0 优先级")
        if self.risk_level == RiskLevel.HIGH and self.priority not in {"P0", "P1"}:
            raise ValueError("high 风险只能使用 P0/P1 优先级")

        flags = set(self.risk_flags)
        critical_risk_flags = {
            RiskFlag.SECRET_EXPOSURE,
            RiskFlag.PHISHING,
            RiskFlag.ASSET_LOSS,
        }
        if flags & critical_risk_flags:
            if (
                self.risk_level != RiskLevel.CRITICAL
                or self.priority != "P0"
                or self.suggested_route != "escalate"
            ):
                raise ValueError("critical 风险 flag 必须是 critical/P0/escalate")

        high_risk_flags = {
            RiskFlag.ADDRESS_MISMATCH,
            RiskFlag.SUSPICIOUS_SIGNATURE,
        }
        if flags & high_risk_flags and self.risk_level not in {
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        }:
            raise ValueError("高风险 flag 的 risk_level 不得低于 high")
        return self


class DiagnosisAction(StrictModel):
    action_code: Literal[
        "generic_troubleshooting",
        "device_reset",
        "bootloader_recovery",
        "wallet_recovery",
        "warranty_decision",
        "transaction_check",
    ] = Field(
        description=(
            "按 triage category 选择：warranty_decision 仅用于 warranty_service；"
            "transaction_check 仅用于 transaction_boundary；wallet_recovery 仅用于"
            "backup_recovery 或 device_loss_damage；device_reset 和 bootloader_recovery"
            "仅用于 firmware_repair。其他普通分类只能使用 generic_troubleshooting。"
        )
    )
    text: str = Field(min_length=1, max_length=300)
    evidence_refs: List[EvidenceRef] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_refs(self):
        _reject_duplicates(self.evidence_refs, "action.evidence_refs")
        return self


class DiagnosisResult(StrictModel):
    outcome: Literal["draft", "need_user", "escalate"] = Field(
        description=(
            "有充分证据生成回答时使用 draft，且 remaining_unknowns 必须为空；"
            "意图明确但仍需客户补充时使用 need_user，并可携带经证据支持的基础回答；"
            "无法安全支持时使用 escalate。"
        )
    )
    diagnosis_summary: str = Field(min_length=1, max_length=500)
    recommended_actions: List[DiagnosisAction] = Field(max_length=6)
    evidence_refs: List[EvidenceRef]
    citations: List[Citation]
    draft_answer: str = Field(max_length=2000)
    remaining_unknowns: List[MissingField] = Field(
        description=(
            "回答生成阶段表示必要字段已经齐全；outcome 为 draft 或 escalate 时必须"
            "返回空数组。只有 outcome=need_user 时才可列出必要未知字段。"
        )
    )

    @model_validator(mode="after")
    def validate_outcome(self):
        _reject_duplicates(self.evidence_refs, "evidence_refs")
        known = set(self.evidence_refs)
        for action in self.recommended_actions:
            if not set(action.evidence_refs).issubset(known):
                raise ValueError("action 引用了不存在的 evidence_id")

        citation_ids = [citation.source_id for citation in self.citations]
        _reject_duplicates(citation_ids, "citation.source_id")
        if not set(citation_ids).issubset(known):
            raise ValueError("citation 引用了不存在的 evidence_id")

        if self.outcome == "draft":
            if not self.draft_answer or not self.recommended_actions or not self.evidence_refs:
                raise ValueError("draft 必须包含回答、动作和证据")
            if self.remaining_unknowns:
                raise ValueError("draft 不得保留必要未知字段")
        if self.outcome == "need_user":
            if not self.remaining_unknowns:
                raise ValueError("need_user 必须有未知字段")
            if self.draft_answer and (
                not self.recommended_actions or not self.evidence_refs
            ):
                raise ValueError("带基础回答的 need_user 必须包含动作和证据")
        if self.outcome == "escalate" and self.draft_answer:
            raise ValueError("escalate 草稿不得自动发送")
        return self


class ReviewResult(StrictModel):
    decision: Literal["approve", "revise", "escalate"]
    issues: List[ReviewItem] = Field(max_length=6)
    required_changes: List[ReviewItem] = Field(max_length=6)
    safety_flags: List[RiskFlag] = Field(max_length=8)
    reason_codes: List[
        Literal[
            "passed",
            "secret_exposure",
            "unsafe_action",
            "unsupported_claim",
            "missing_evidence",
            "invalid_citation",
            "incomplete_steps",
            "overpromise",
            "official_source_violation",
        ]
    ] = Field(max_length=9)

    @model_validator(mode="after")
    def validate_decision(self):
        _reject_duplicates(self.issues, "issues")
        _reject_duplicates(self.required_changes, "required_changes")
        _reject_duplicates(self.safety_flags, "safety_flags")
        _reject_duplicates(self.reason_codes, "reason_codes")

        if self.safety_flags and self.decision != "escalate":
            raise ValueError("存在 safety_flags 时必须升级人工处理")
        if self.decision == "approve":
            if self.reason_codes != ["passed"]:
                raise ValueError("approve 只能包含 passed")
            if self.issues or self.required_changes or self.safety_flags:
                raise ValueError("approve 不得包含问题")
        if self.decision == "revise":
            if not self.issues or not self.required_changes:
                raise ValueError("revise 必须包含问题和修改项")
            if not self.reason_codes or "passed" in self.reason_codes:
                raise ValueError("revise 必须包含非 passed 原因")
        if self.decision == "escalate":
            if not self.reason_codes or "passed" in self.reason_codes:
                raise ValueError("escalate 必须包含非 passed 原因")
        return self


class EvidenceState(TypedDict, total=False):
    evidence_id: str
    kind: str
    content: str
    source_title: str
    source_url: Optional[str]
    metadata: Dict[str, JSONValue]


class DiagnosisActionState(TypedDict):
    action_code: str
    text: str
    evidence_refs: List[str]


class StatusEventState(TypedDict):
    command_id: str
    step_index: int
    node_name: str
    event_type: str
    summary: str
    from_status: Optional[str]
    to_status: Optional[str]
    metadata: Dict[str, JSONValue]


class TicketState(TypedDict, total=False):
    ticket_id: str
    request_id: str
    command_id: str
    event_step: int
    user_id: str
    sanitized_input: str
    safe_history: List[Dict[str, str]]
    sensitive_flags: List[str]
    intent: str
    category: str
    priority: str
    risk_level: str
    risk_flags: List[str]
    clarity: str
    clarification_question: str
    clarification_options: List[str]
    missing_fields: List[str]
    suggested_route: str
    summary: str
    customer_context: Dict[str, str]
    outcome: str
    diagnosis_summary: str
    recommended_actions: List[DiagnosisActionState]
    evidence_refs: List[str]
    remaining_unknowns: List[str]
    evidence: List[EvidenceState]
    citations: List[Dict[str, str]]
    tool_errors: List[str]
    draft_answer: str
    review_decision: str
    review_reasons: List[str]
    review_issues: List[str]
    required_changes: List[str]
    revision_count: int
    response_version: int
    status: str
    waiting_reason: str
    resolution_confirmed: bool
    requires_human: bool
    manual_gate_reason: str
    idle_expires_at: str
    closed_at: str
    close_reason: str
    human_decision: str
    final_answer: str
    status_events: Annotated[List[StatusEventState], add]
    last_error: str
