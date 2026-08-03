"""Deterministic clarification-choice construction and validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

from agent.orchestration.state import (
    ClarificationCandidate,
    ClarificationChoice,
)


def _choice_id(payload: Mapping[str, object], index: int) -> str:
    canonical = json.dumps(
        {"index": index, "choice": dict(payload)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"choice_{hashlib.sha256(canonical).hexdigest()[:16]}"


def build_clarification_choices(
    candidates: Iterable[ClarificationCandidate | Mapping[str, object]],
) -> list[dict[str, object]]:
    """Validate candidates and assign stable server-owned choice IDs."""

    choices: list[ClarificationChoice] = []
    labels: set[str] = set()
    ids: set[str] = set()
    for index, raw_candidate in enumerate(candidates):
        candidate = ClarificationCandidate.model_validate(raw_candidate)
        payload = candidate.model_dump(mode="json")
        choice = ClarificationChoice(
            choice_id=_choice_id(payload, index),
            legacy=False,
            **payload,
        )
        normalized_label = choice.label.casefold()
        if normalized_label in labels or choice.choice_id in ids:
            raise ValueError("clarification choices 必须具有唯一 label 和 choice_id")
        labels.add(normalized_label)
        ids.add(choice.choice_id)
        choices.append(choice)
    if not 2 <= len(choices) <= 5:
        raise ValueError("clarification choices 必须包含 2-5 个选项")
    return [choice.model_dump(mode="json") for choice in choices]


def build_legacy_choice(label: str, index: int) -> dict[str, object]:
    """Convert a v8 string option into an identifiable untrusted legacy choice."""

    normalized = " ".join(label.split()) if isinstance(label, str) else ""
    if not normalized or len(normalized) > 160:
        raise ValueError("legacy clarification label 非法")
    payload = ClarificationCandidate(
        label=normalized,
        intent="other",
        category="other",
        risk_level="low",
        risk_flags=[],
        missing_fields=[],
        suggested_route="diagnose",
    ).model_dump(mode="json")
    choice = ClarificationChoice(
        choice_id=_choice_id({"legacy": True, **payload}, index),
        legacy=True,
        **payload,
    )
    return choice.model_dump(mode="json")


def validate_clarification_choices(value: object) -> list[dict[str, object]]:
    """Validate a persisted/current structured choice list."""

    if not isinstance(value, list):
        raise TypeError("clarification choices 必须是列表")
    choices = [ClarificationChoice.model_validate(item) for item in value]
    if choices and not 2 <= len(choices) <= 5:
        raise ValueError("clarification choices 必须包含 2-5 个选项")
    ids = [choice.choice_id for choice in choices]
    labels = [choice.label.casefold() for choice in choices]
    if len(ids) != len(set(ids)) or len(labels) != len(set(labels)):
        raise ValueError("clarification choices 不得重复")
    return [choice.model_dump(mode="json") for choice in choices]
