from enum import Enum
from operator import add
from typing import Annotated, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import TypedDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Status(str, Enum):
    NEW = "new"
    TRIAGED = "triaged"
    DIAGNOSING = "diagnosing"
    REVIEWING = "reviewing"
    PENDING_USER = "pending_user"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


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
    evidence_id: str = Field(min_length=1)
    kind: Literal["knowledge", "profile", "device", "warranty", "chain"]
    content: str = Field(min_length=1)
    source_title: str = Field(min_length=1)
    source_url: Optional[str] = None


class Citation(StrictModel):
    source_id: str = Field(min_length=1)
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
    missing_fields: List[MissingField]
    suggested_route: Literal["ask_user", "diagnose", "escalate"]
    summary: str = Field(min_length=1, max_length=300)


class DiagnosisAction(StrictModel):
    action_code: Literal[
        "generic_troubleshooting",
        "device_reset",
        "bootloader_recovery",
        "wallet_recovery",
        "warranty_decision",
        "transaction_check",
    ]
    text: str = Field(min_length=1, max_length=300)
    evidence_refs: List[str] = Field(min_length=1)


class DiagnosisResult(StrictModel):
    outcome: Literal["draft", "need_user", "escalate"]
    diagnosis_summary: str = Field(min_length=1, max_length=500)
    recommended_actions: List[DiagnosisAction] = Field(max_length=6)
    evidence_refs: List[str]
    citations: List[Citation]
    draft_answer: str = Field(max_length=2000)
    remaining_unknowns: List[MissingField]

    @model_validator(mode="after")
    def validate_outcome(self):
        if self.outcome == "draft":
            if not self.draft_answer or not self.recommended_actions or not self.evidence_refs:
                raise ValueError("draft 必须包含回答、动作和证据")
            if self.remaining_unknowns:
                raise ValueError("draft 不得保留必要未知字段")
            known = set(self.evidence_refs)
            for action in self.recommended_actions:
                if not set(action.evidence_refs).issubset(known):
                    raise ValueError("action 引用了不存在的 evidence_id")
        if self.outcome == "need_user":
            if not self.remaining_unknowns or self.draft_answer:
                raise ValueError("need_user 必须有未知字段且不能生成草稿")
        if self.outcome == "escalate" and self.draft_answer:
            raise ValueError("escalate 草稿不得自动发送")
        return self


class ReviewResult(StrictModel):
    decision: Literal["approve", "revise", "escalate"]
    issues: List[str]
    required_changes: List[str] = Field(max_length=6)
    safety_flags: List[RiskFlag]
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
    ]

    @model_validator(mode="after")
    def validate_decision(self):
        if self.decision == "approve":
            if self.reason_codes != ["passed"]:
                raise ValueError("approve 只能包含 passed")
            if self.issues or self.required_changes or self.safety_flags:
                raise ValueError("approve 不得包含问题")
        if self.decision == "revise" and (not self.issues or not self.required_changes):
            raise ValueError("revise 必须包含问题和修改项")
        if self.decision == "escalate":
            if not self.reason_codes or "passed" in self.reason_codes:
                raise ValueError("escalate 必须包含非 passed 原因")
        return self


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
    missing_fields: List[str]
    suggested_route: str
    customer_context: Dict[str, str]
    evidence: List[Dict[str, object]]
    citations: List[Dict[str, str]]
    tool_errors: List[str]
    draft_answer: str
    review_decision: str
    review_reasons: List[str]
    revision_count: int
    response_version: int
    status: str
    requires_human: bool
    manual_gate_reason: str
    human_decision: str
    final_answer: str
    status_events: Annotated[List[Dict[str, object]], add]
    last_error: str
