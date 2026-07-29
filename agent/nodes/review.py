"""Independent, fail-closed review over bounded diagnosis evidence."""

import json
import math
import re
from pathlib import Path
from typing import Dict, List

from langchain_core.messages import HumanMessage, SystemMessage

from agent.nodes.prompt_loader import load_safe_prompt
from agent.orchestration.invoke import invoke_with_policy
from agent.orchestration.state import (
    Citation,
    DiagnosisAction,
    EvidenceItem,
    ReviewResult,
)
from agent.security.secrets import contains_unredacted_secret
from agent.security.trusted_sources import TrustedSourcePolicy


_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "review_prompt.txt"
_MAX_DRAFT_LENGTH = 2_000
_MAX_ACTIONS = 6
_MAX_EVIDENCE_ITEMS = 16
_MAX_EVIDENCE_CONTENT = 32 * 1024
_MAX_CITATIONS = 16
_MAX_JSON_BYTES = 128 * 1024
_MAX_REVIEW_ITEMS = 6
_SAFE_REASON_PATTERN = re.compile(r"[a-z_]{1,64}", re.ASCII)


class _PolicyGuardFailure(RuntimeError):
    """Internal marker for sanitized, fail-closed policy execution."""


def _has_forbidden_control(value: str) -> bool:
    return any(
        (ord(character) < 0x20 and character not in {"\n", "\t"})
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    )


def _normalized_text(
    value: object,
    field_name: str,
    *,
    max_length: int,
    allow_secret: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{field_name} 非法")
    if _has_forbidden_control(value) or (
        not allow_secret and contains_unredacted_secret(value)
    ):
        raise ValueError(f"{field_name} 非法")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} 非法")
    return normalized


def _evidence_refs(value: object) -> List[str]:
    if not isinstance(value, list) or not value or len(value) > _MAX_EVIDENCE_ITEMS:
        raise ValueError("evidence_refs 非法")
    refs: List[str] = []
    for raw in value:
        ref = _normalized_text(raw, "evidence_ref", max_length=128)
        if any(character.isspace() for character in ref):
            raise ValueError("evidence_ref 非法")
        refs.append(ref)
    if len(refs) != len(set(refs)):
        raise ValueError("evidence_refs 不得重复")
    return refs


def _actions(value: object) -> List[DiagnosisAction]:
    if not isinstance(value, list) or not value or len(value) > _MAX_ACTIONS:
        raise ValueError("recommended_actions 非法")
    actions: List[DiagnosisAction] = []
    for raw in value:
        if isinstance(raw, DiagnosisAction):
            raw = raw.model_dump(mode="python")
        if not isinstance(raw, dict):
            raise TypeError("recommended_actions 非法")
        action = DiagnosisAction.model_validate(raw)
        text = _normalized_text(
            action.text,
            "action.text",
            max_length=300,
            allow_secret=True,
        )
        actions.append(action.model_copy(update={"text": text}))
    return actions


def _evidence(value: object) -> List[EvidenceItem]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > _MAX_EVIDENCE_ITEMS
    ):
        raise ValueError("evidence 非法")
    items: List[EvidenceItem] = []
    total_content = 0
    for raw in value:
        if isinstance(raw, EvidenceItem):
            raw = raw.model_dump(mode="python")
        if not isinstance(raw, dict):
            raise TypeError("evidence 非法")
        item = EvidenceItem.model_validate(raw)
        evidence_id = _normalized_text(
            item.evidence_id, "evidence_id", max_length=128
        )
        if any(character.isspace() for character in evidence_id):
            raise ValueError("evidence_id 非法")
        content = item.content
        if (
            not content.strip()
            or len(content) > _MAX_EVIDENCE_CONTENT
            or _has_forbidden_control(content)
            or contains_unredacted_secret(content)
        ):
            raise ValueError("evidence.content 非法")
        total_content += len(content)
        if total_content > _MAX_EVIDENCE_CONTENT:
            raise ValueError("evidence.content 总量超限")
        source_title = _normalized_text(
            item.source_title, "evidence.source_title", max_length=500
        )
        source_url = item.source_url
        if source_url is not None:
            source_url = _normalized_text(
                source_url, "evidence.source_url", max_length=2_000
            )
            if not TrustedSourcePolicy.is_safe_https_url(source_url):
                raise ValueError("evidence.source_url 非法")
        items.append(
            item.model_copy(
                update={
                    "evidence_id": evidence_id,
                    "content": content,
                    "source_title": source_title,
                    "source_url": source_url,
                }
            )
        )
    ids = [item.evidence_id for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("evidence_id 不得重复")
    return items


def _citations(value: object) -> List[Citation]:
    if not isinstance(value, list) or len(value) > _MAX_CITATIONS:
        raise ValueError("citations 非法")
    citations: List[Citation] = []
    for raw in value:
        if isinstance(raw, Citation):
            raw = raw.model_dump(mode="python")
        if not isinstance(raw, dict):
            raise TypeError("citation 非法")
        citation = Citation.model_validate(raw)
        source_id = _normalized_text(
            citation.source_id, "citation.source_id", max_length=128
        )
        if any(character.isspace() for character in source_id):
            raise ValueError("citation.source_id 非法")
        source_title = _normalized_text(
            citation.source_title, "citation.source_title", max_length=500
        )
        source_url = _normalized_text(
            citation.source_url, "citation.source_url", max_length=2_000
        )
        if not TrustedSourcePolicy.is_safe_https_url(source_url):
            raise ValueError("citation.source_url 非法")
        citations.append(
            citation.model_copy(
                update={
                    "source_id": source_id,
                    "source_title": source_title,
                    "source_url": source_url,
                }
            )
        )
    ids = [citation.source_id for citation in citations]
    if len(ids) != len(set(ids)):
        raise ValueError("citation.source_id 不得重复")
    return citations


def _validated_state(state: object) -> dict:
    try:
        return _validated_state_inner(state)
    except (TypeError, ValueError):
        raise ValueError("Review 输入结构非法") from None


def _validated_state_inner(state: object) -> dict:
    if not isinstance(state, dict):
        raise TypeError("state 必须是对象")
    required = (
        "draft_answer",
        "recommended_actions",
        "evidence_refs",
        "evidence",
        "citations",
    )
    if any(field not in state for field in required):
        raise ValueError("state 缺少审核字段")
    draft_answer = _normalized_text(
        state["draft_answer"],
        "draft_answer",
        max_length=_MAX_DRAFT_LENGTH,
        allow_secret=True,
    )
    actions = _actions(state["recommended_actions"])
    evidence_refs = _evidence_refs(state["evidence_refs"])
    evidence = _evidence(state["evidence"])
    citations = _citations(state["citations"])

    evidence_by_id: Dict[str, EvidenceItem] = {
        item.evidence_id: item for item in evidence
    }
    actual_ids = set(evidence_by_id)
    top_refs = set(evidence_refs)
    if not top_refs.issubset(actual_ids):
        raise ValueError("evidence_refs 包含未知证据")
    for action in actions:
        if not set(action.evidence_refs).issubset(top_refs):
            raise ValueError("action 包含未知证据")

    citation_by_id = {citation.source_id: citation for citation in citations}
    for citation in citations:
        source = evidence_by_id.get(citation.source_id)
        if (
            source is None
            or citation.source_id not in top_refs
            or source.source_url is None
            or citation.source_title != source.source_title
            or citation.source_url != source.source_url
        ):
            raise ValueError("citation 与证据不匹配")
    for evidence_id in evidence_refs:
        source = evidence_by_id[evidence_id]
        if (
            source.kind == "knowledge"
            and source.source_url is not None
            and evidence_id not in citation_by_id
        ):
            raise ValueError("知识证据缺少 citation")

    projected_evidence = [
        evidence_by_id[evidence_id] for evidence_id in evidence_refs
    ]
    payload = {
        "draft_answer": draft_answer,
        "recommended_actions": [
            action.model_dump(mode="json") for action in actions
        ],
        "evidence_refs": evidence_refs,
        "evidence": [
            item.model_dump(mode="json") for item in projected_evidence
        ],
        "citations": [citation.model_dump(mode="json") for citation in citations],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("Review 输入总量超限")
    return {"payload": payload, "encoded": encoded}


def _policy_decision(policy_guard: object, payload: dict) -> tuple[bool, List[str]]:
    policy_text = "\n".join(
        [payload["draft_answer"]]
        + [action["text"] for action in payload["recommended_actions"]]
    )
    try:
        trusted_sources = policy_guard.trusted_sources
        if any(
            item["source_url"] is not None
            and not trusted_sources.is_trusted(item["source_url"])
            for item in payload["evidence"]
        ):
            return False, ["official_source_violation"]
        urls = list(
            dict.fromkeys(
                citation["source_url"] for citation in payload["citations"]
            )
        )
        decision = policy_guard.evaluate(policy_text, urls)
    except Exception:
        raise _PolicyGuardFailure("Policy Guard 调用失败") from None
    passed = getattr(decision, "passed", None)
    reasons = getattr(decision, "reason_codes", None)
    if (
        not isinstance(passed, bool)
        or not isinstance(reasons, list)
        or len(reasons) > 10
        or any(
            not isinstance(reason, str)
            or _SAFE_REASON_PATTERN.fullmatch(reason) is None
            for reason in reasons
        )
        or len(reasons) != len(set(reasons))
        or (passed and reasons)
        or (not passed and not reasons)
    ):
        raise _PolicyGuardFailure("Policy Guard 输出非法")
    return passed, reasons


def _review_result(raw: object) -> ReviewResult:
    if isinstance(raw, ReviewResult):
        raw = raw.model_dump(mode="python")
    if not isinstance(raw, dict):
        raise TypeError("Review 输出类型非法")
    result = ReviewResult.model_validate(raw)
    if len(result.issues) > _MAX_REVIEW_ITEMS:
        raise ValueError("Review issues 超限")
    issues = [
        _normalized_text(issue, "review.issue", max_length=300)
        for issue in result.issues
    ]
    changes = [
        _normalized_text(change, "review.required_change", max_length=300)
        for change in result.required_changes
    ]
    return result.model_copy(
        update={"issues": issues, "required_changes": changes}
    )


class ReviewAgent:
    """Apply deterministic policy before one independent structured review."""

    def __init__(
        self,
        model: object | None = None,
        runner: object | None = None,
        *,
        policy_guard: object | None = None,
        timeout_seconds: float = 20,
    ) -> None:
        if (model is None) == (runner is None):
            raise ValueError("ReviewAgent 必须且只能提供 model 或 runner")
        if (
            policy_guard is None
            or not callable(getattr(policy_guard, "evaluate", None))
            or not callable(
                getattr(
                    getattr(policy_guard, "trusted_sources", None),
                    "is_trusted",
                    None,
                )
            )
        ):
            raise TypeError("policy_guard 接口非法")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds 必须是有限正数")
        if model is not None:
            factory = getattr(model, "with_structured_output", None)
            if not callable(factory):
                raise TypeError("model 不支持结构化输出")
            runner = factory(ReviewResult, method="function_calling")
        if not callable(getattr(runner, "invoke", None)):
            raise TypeError("runner 必须提供 invoke")

        self.runner = runner
        self.policy_guard = policy_guard
        self.timeout_seconds = timeout_seconds
        self.prompt = load_safe_prompt(_PROMPT_PATH, "Review")

    def run(self, state: dict) -> dict:
        validated = _validated_state(state)
        payload = validated["payload"]
        try:
            policy_passed, policy_reasons = _policy_decision(
                self.policy_guard, payload
            )
        except _PolicyGuardFailure:
            return {
                "review_decision": "escalate",
                "review_reasons": ["policy_guard_failure"],
                "review_issues": ["确定性 Policy Guard 执行失败"],
                "required_changes": [],
            }
        if not policy_passed:
            return {
                "review_decision": "escalate",
                "review_reasons": policy_reasons,
                "review_issues": ["确定性 Policy Guard 未通过"],
                "required_changes": [],
            }

        messages = [
            SystemMessage(content=self.prompt),
            HumanMessage(content=validated["encoded"]),
        ]
        result = invoke_with_policy(
            lambda: _review_result(self.runner.invoke(messages)),
            timeout_seconds=self.timeout_seconds,
            retries=0,
        )
        return {
            "review_decision": result.decision,
            "review_reasons": [reason for reason in result.reason_codes],
            "review_issues": list(result.issues),
            "required_changes": list(result.required_changes),
        }
