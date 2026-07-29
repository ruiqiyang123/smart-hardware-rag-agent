"""Run the fixed KeyGuard V2 orchestration evaluation.

The evaluator contains no demo/fake runtime.  A real or offline runner must be
injected by the caller, or explicitly configured as ``module:attribute`` in
``KEYGUARD_ORCHESTRATION_EVAL_RUNNER``.  This prevents an unconfigured CLI run
from publishing fabricated measurements.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.security.secrets import contains_unredacted_secret
from eval.orchestration_scorers import aggregate_metrics, score_case, validate_cases


DEFAULT_CASES = Path(__file__).with_name("multi_agent_cases.json")
DEFAULT_V1_CASES = Path(__file__).with_name("eval_cases.json")
DEFAULT_OUTPUT_DIR = Path(__file__).with_name("eval_results")
RUNNER_ENV = "KEYGUARD_ORCHESTRATION_EVAL_RUNNER"
_RUNNER_SPEC = re.compile(
    r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII
)
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)
_STATUSES = frozenset(
    {"new", "triaged", "diagnosing", "reviewing", "pending_user", "escalated", "resolved"}
)
_INTENTS = frozenset(
    {
        "troubleshoot",
        "recovery",
        "warranty",
        "transaction_boundary",
        "security_incident",
        "security_report",
        "other",
    }
)
_PRIORITIES = frozenset({"P0", "P1", "P2"})
_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
_MISSING_FIELDS = frozenset(
    {
        "device_model",
        "app_os",
        "connection_type",
        "firmware_version",
        "error_state",
        "serial_last4",
        "purchase_date",
        "transaction_hash",
        "chain_name",
    }
)
_ALLOWED_TRANSITIONS = {
    "new": {"triaged", "escalated"},
    "triaged": {"pending_user", "diagnosing", "escalated"},
    "pending_user": {"triaged", "escalated"},
    "diagnosing": {"pending_user", "reviewing", "escalated"},
    "reviewing": {"resolved", "diagnosing", "escalated"},
    "escalated": {"resolved", "pending_user", "escalated"},
    "resolved": set(),
}
_ACTUAL_FIELDS = frozenset(
    {
        "intent",
        "priority",
        "risk_level",
        "status",
        "answer",
        "citations",
        "evidence_refs",
        "missing_fields",
        "status_trace",
        "turn_results",
        "model_calls",
        "tool_calls",
        "tool_successes",
        "persistence_texts",
        "secret_found_in_persistence",
        "draft_sent",
        "retry_count",
        "observed_behaviors",
    }
)
_REPORT_SCORE_FIELDS = frozenset(
    {
        "intent_passed",
        "priority_passed",
        "risk_passed",
        "route_passed",
        "turns_passed",
        "trace_requirements_passed",
        "transition_passed",
        "missing_fields_passed",
        "required_behavior_passed",
        "citation_required",
        "citation_passed",
        "forbidden_behavior_passed",
        "expected_high_risk",
        "secret_case",
        "secret_passed",
        "escalated",
        "fault_case",
        "retry_passed",
        "safe_fallback_passed",
    }
)


class RunnerConfigurationError(RuntimeError):
    """No explicit, usable evaluation runner was configured."""


class RunnerOutputError(ValueError):
    """A runner returned an incomplete or unsafe observation payload."""


class RunnerExecutionError(RuntimeError):
    """A runner failed; its original exception is deliberately not retained."""


@dataclass(frozen=True, slots=True)
class FaultInjector:
    """Instance-local fault hooks for runner dependency boundaries.

    A runner applies ``before_call`` immediately before its named dependency
    and ``after_call`` to that dependency's return value.  No production
    module, singleton, or environment variable is mutated.
    """

    kind: str | None
    _before_counts: dict[str, int] = field(
        default_factory=dict, init=False, repr=False
    )
    _after_counts: dict[str, int] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        allowed = {
            None,
            "triage_timeout",
            "rag_empty",
            "warranty_tool_exception",
            "reviewer_validation_error",
        }
        if self.kind not in allowed:
            raise ValueError("fault kind 非法")

    def before_call(self, boundary: str) -> None:
        if boundary not in {"triage", "knowledge_search", "warranty", "reviewer"}:
            raise ValueError("fault boundary 非法")
        self._before_counts[boundary] = self._before_counts.get(boundary, 0) + 1
        if self.kind == "triage_timeout" and boundary == "triage":
            raise TimeoutError("INJECTED_TRIAGE_TIMEOUT")
        if self.kind == "warranty_tool_exception" and boundary == "warranty":
            raise RuntimeError("INJECTED_WARRANTY_TOOL_EXCEPTION")

    def after_call(self, boundary: str, value: object) -> object:
        if boundary not in {"triage", "knowledge_search", "warranty", "reviewer"}:
            raise ValueError("fault boundary 非法")
        self._after_counts[boundary] = self._after_counts.get(boundary, 0) + 1
        if self.kind == "rag_empty" and boundary == "knowledge_search":
            return []
        if self.kind == "reviewer_validation_error" and boundary == "reviewer":
            return {"injected_invalid_result": True}
        return value

    def audit_snapshot(self) -> dict[str, dict[str, int]]:
        return {
            "before": dict(self._before_counts),
            "after": dict(self._after_counts),
        }

    def verify_expected_usage(self, expected_retry_count: int) -> int | None:
        if self.kind is None:
            return None
        targets = {
            "triage_timeout": ("before", "triage"),
            "rag_empty": ("after", "knowledge_search"),
            "warranty_tool_exception": ("before", "warranty"),
            "reviewer_validation_error": ("after", "reviewer"),
        }
        hook, boundary = targets[self.kind]
        counts = self._before_counts if hook == "before" else self._after_counts
        expected_calls = expected_retry_count + 1
        if counts.get(boundary, 0) != expected_calls:
            raise RunnerOutputError("fault hook 审计失败")
        return expected_calls - 1


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取评测 JSON: {path}") from error


def load_cases(path: str | Path = DEFAULT_CASES) -> list[dict[str, object]]:
    return validate_cases(_read_json(Path(path)))


def _validate_v1_cases(raw_cases: object) -> list[dict[str, object]]:
    if not isinstance(raw_cases, list) or len(raw_cases) != 30:
        raise ValueError("V1 兼容基线必须是原始 30 条")
    validated: list[dict[str, object]] = []
    questions: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict) or set(raw) != {
            "question",
            "expected_keywords",
            "category",
        }:
            raise ValueError("V1 case schema 非法")
        question = raw["question"]
        keywords = raw["expected_keywords"]
        category = raw["category"]
        if (
            not isinstance(question, str)
            or not question.strip()
            or question in questions
            or not isinstance(category, str)
            or not category.strip()
            or not isinstance(keywords, list)
            or not keywords
            or any(not isinstance(item, str) or not item.strip() for item in keywords)
            or len(keywords) != len(set(keywords))
        ):
            raise ValueError("V1 case 内容非法")
        questions.add(question)
        validated.append(copy.deepcopy(raw))
    return validated


def load_v1_cases(path: str | Path = DEFAULT_V1_CASES) -> list[dict[str, object]]:
    return _validate_v1_cases(_read_json(Path(path)))


def _runner_interface(runner: object) -> bool:
    return callable(getattr(runner, "run_v2_case", None)) and callable(
        getattr(runner, "run_v1_compatibility", None)
    )


def load_configured_runner(environ: Mapping[str, str] | None = None) -> object:
    environment = os.environ if environ is None else environ
    spec = environment.get(RUNNER_ENV, "")
    if not isinstance(spec, str) or _RUNNER_SPEC.fullmatch(spec) is None:
        raise RunnerConfigurationError(
            f"未配置真实评测 runner；请设置 {RUNNER_ENV}=module:attribute"
        )
    module_name, attribute_name = spec.split(":", 1)
    try:
        candidate = getattr(importlib.import_module(module_name), attribute_name)
        if not _runner_interface(candidate) and callable(candidate):
            candidate = candidate()
    except Exception:
        raise RunnerConfigurationError("评测 runner 加载失败") from None
    if not _runner_interface(candidate):
        raise RunnerConfigurationError("评测 runner 接口非法")
    return candidate


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunnerOutputError(f"{field} 必须是非负整数")
    return value


def _string_list(
    value: object,
    field: str,
    *,
    maximum: int = 128,
    item_maximum: int = 2_000,
    total_maximum: int = 64_000,
    unique: bool = True,
    allow_secret: bool = False,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise RunnerOutputError(f"{field} 非法")
    total_length = 0
    for item in value:
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item) > item_maximum
            or any(
                (ord(character) < 0x20 and character not in {"\n", "\t"})
                or 0x7F <= ord(character) <= 0x9F
                for character in item
            )
            or (not allow_secret and contains_unredacted_secret(item))
        ):
            raise RunnerOutputError(f"{field} 非法")
        total_length += len(item)
    if total_length > total_maximum or (unique and len(value) != len(set(value))):
        raise RunnerOutputError(f"{field} 非法")
    return list(value)


def _safe_report_text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(
            (ord(character) < 0x20 and character not in {"\n", "\t"})
            or 0x7F <= ord(character) <= 0x9F
            for character in value
        )
        or contains_unredacted_secret(value)
    ):
        raise RunnerOutputError(f"{field} 内容非法")
    return value


def _citation_list(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 32:
        raise RunnerOutputError("citations 非法")
    citations: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if (
            not isinstance(raw, Mapping)
            or not {"source_id", "source_url"}.issubset(raw)
            or not set(raw).issubset({"source_id", "source_title", "source_url"})
        ):
            raise RunnerOutputError("citation schema 非法")
        source_id = _safe_report_text(raw["source_id"], "citation.source_id", 128)
        source_url = _safe_report_text(raw["source_url"], "citation.source_url", 2_000)
        parsed_url = urlparse(source_url)
        if parsed_url.scheme.lower() != "https" or not parsed_url.hostname:
            raise RunnerOutputError("citation.source_url 非法")
        raw_title = raw.get("source_title", "")
        if not isinstance(raw_title, str):
            raise RunnerOutputError("citation 内容非法")
        source_title = (
            _safe_report_text(raw_title, "citation.source_title", 500)
            if raw_title
            else ""
        )
        if source_id in seen:
            raise RunnerOutputError("citation source_id 不得重复")
        seen.add(source_id)
        citation = {"source_id": source_id, "source_url": source_url}
        if source_title:
            citation["source_title"] = source_title
        citations.append(citation)
    return citations


def _illegal_transition_count(trace: list[str]) -> int:
    return sum(
        target != current and target not in _ALLOWED_TRANSITIONS[current]
        for current, target in zip(trace, trace[1:])
    )


def _normalize_actual(raw: object, expected_turn_count: int) -> dict[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != _ACTUAL_FIELDS:
        raise RunnerOutputError("runner observation schema 非法")
    actual = copy.deepcopy(dict(raw))
    for field in ("intent", "priority", "risk_level", "status"):
        if not isinstance(actual[field], str) or not actual[field]:
            raise RunnerOutputError(f"{field} 非法")
    if actual["intent"] not in _INTENTS:
        raise RunnerOutputError("intent 非法")
    if actual["priority"] not in _PRIORITIES:
        raise RunnerOutputError("priority 非法")
    if actual["risk_level"] not in _RISK_LEVELS:
        raise RunnerOutputError("risk_level 非法")
    if actual["status"] not in _STATUSES:
        raise RunnerOutputError("status 非法")
    answer = actual["answer"]
    if not isinstance(answer, str) or len(answer) > 32_000:
        raise RunnerOutputError("answer 非法")
    actual["citations"] = _citation_list(actual["citations"])
    actual["evidence_refs"] = _string_list(
        actual["evidence_refs"], "evidence_refs", maximum=64, item_maximum=128
    )
    actual["missing_fields"] = _string_list(
        actual["missing_fields"], "missing_fields", maximum=16, item_maximum=64
    )
    if not set(actual["missing_fields"]).issubset(_MISSING_FIELDS):
        raise RunnerOutputError("missing_fields 包含未知值")
    actual["observed_behaviors"] = _string_list(
        actual["observed_behaviors"],
        "observed_behaviors",
        maximum=64,
        item_maximum=300,
    )
    trace = _string_list(
        actual["status_trace"],
        "status_trace",
        maximum=128,
        item_maximum=32,
        unique=False,
    )
    if (
        not trace
        or trace[0] != "new"
        or any(status not in _STATUSES for status in trace)
        or trace[-1] != actual["status"]
    ):
        raise RunnerOutputError("status_trace 非法")
    actual["status_trace"] = trace
    turn_results = actual["turn_results"]
    if not isinstance(turn_results, list) or len(turn_results) != expected_turn_count:
        raise RunnerOutputError("turn_results 数量非法")
    normalized_turns = []
    previous_trace_index = -1
    for index, turn in enumerate(turn_results, 1):
        trace_index = turn.get("trace_index") if isinstance(turn, Mapping) else None
        if (
            not isinstance(turn, Mapping)
            or set(turn) != {"turn_index", "status", "trace_index"}
            or turn.get("turn_index") != index
            or turn.get("status") not in _STATUSES
            or isinstance(trace_index, bool)
            or not isinstance(trace_index, int)
            or trace_index <= previous_trace_index
            or trace_index >= len(trace)
            or trace[trace_index] != turn.get("status")
        ):
            raise RunnerOutputError("turn_results schema 非法")
        normalized_turns.append(
            {
                "turn_index": index,
                "status": turn["status"],
                "trace_index": trace_index,
            }
        )
        previous_trace_index = trace_index
    actual["turn_results"] = normalized_turns
    for field in ("model_calls", "tool_calls", "tool_successes", "retry_count"):
        actual[field] = _nonnegative_int(actual[field], field)
    if actual["tool_successes"] > actual["tool_calls"]:
        raise RunnerOutputError("tool_successes 不得大于 tool_calls")
    for field in ("secret_found_in_persistence", "draft_sent"):
        if not isinstance(actual[field], bool):
            raise RunnerOutputError(f"{field} 必须是布尔值")
    actual["persistence_texts"] = _string_list(
        actual["persistence_texts"],
        "persistence_texts",
        maximum=128,
        item_maximum=32_000,
        total_maximum=256_000,
        unique=False,
        allow_secret=True,
    )
    actual["illegal_transition_count"] = _illegal_transition_count(trace)
    return actual


def _score_v1_observations(
    value: object, expected_cases: list[dict[str, object]]
) -> dict[str, object]:
    if not isinstance(value, list) or len(value) != len(expected_cases):
        raise RunnerOutputError("V1 兼容结果数量非法")
    coverages: list[float] = []
    cited_cases = 0
    for index, (raw, expected) in enumerate(zip(value, expected_cases), 1):
        expected_id = f"KG-V1-{index:03d}"
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"case_id", "answer", "citations"}
            or raw.get("case_id") != expected_id
        ):
            raise RunnerOutputError("V1 兼容结果 schema 非法")
        answer = raw.get("answer")
        if (
            not isinstance(answer, str)
            or len(answer) > 32_000
            or contains_unredacted_secret(answer)
        ):
            raise RunnerOutputError("V1 answer 非法")
        citations = _citation_list(raw.get("citations"))
        keywords = expected["expected_keywords"]
        coverages.append(
            sum(keyword in answer for keyword in keywords) / len(keywords)
        )
        cited_cases += bool(citations)
    total = len(expected_cases)
    return {
        "total_cases": total,
        "overall_coverage": sum(coverages) / total,
        "citation_rate": cited_cases / total,
    }


def _percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _project_report_scores(scored: Mapping[str, object]) -> dict[str, bool]:
    if not _REPORT_SCORE_FIELDS.issubset(scored):
        raise RunnerOutputError("scorer 输出缺少固定字段")
    projected = {field: scored[field] for field in sorted(_REPORT_SCORE_FIELDS)}
    if any(not isinstance(value, bool) for value in projected.values()):
        raise RunnerOutputError("scorer 报告字段必须是布尔值")
    return projected


def _project_runner_case(case: Mapping[str, object]) -> dict[str, object]:
    return {
        "case_id": case["case_id"],
        "scope": case["scope"],
        "turns": [
            {"turn_index": turn["turn_index"], "input": turn["input"]}
            for turn in case["turns"]
        ],
    }


def _project_v1_runner_cases(
    cases: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "case_id": f"KG-V1-{index:03d}",
            "scope": "v1_compatibility",
            "turns": [{"turn_index": 1, "input": case["question"]}],
        }
        for index, case in enumerate(cases, 1)
    ]


def run_evaluation(
    cases: object,
    *,
    runner: object,
    v1_cases: object,
    tag: str,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, object]:
    """Execute injected V2/V1 runners and return a secret-free report."""

    if not _runner_interface(runner):
        raise RunnerConfigurationError("评测 runner 接口非法")
    if not isinstance(tag, str) or _TAG.fullmatch(tag) is None:
        raise ValueError("tag 只能包含字母、数字、点、下划线和连字符")
    checked_cases = validate_cases(cases)
    checked_v1_cases = _validate_v1_cases(v1_cases)

    scored_results: list[dict[str, object]] = []
    report_results: list[dict[str, object]] = []
    latencies: list[float] = []
    total_model_calls = 0
    total_tool_calls = 0
    total_tool_successes = 0
    for case in checked_cases:
        injector = FaultInjector(case["fault"])
        started = clock()
        try:
            raw_actual = runner.run_v2_case(
                _project_runner_case(case), fault_injector=injector
            )
        except Exception:
            raise RunnerExecutionError(
                f"{case['case_id']} V2 runner 执行失败"
            ) from None
        finished = clock()
        if (
            isinstance(started, bool)
            or isinstance(finished, bool)
            or not isinstance(started, (int, float))
            or not isinstance(finished, (int, float))
            or not math.isfinite(started)
            or not math.isfinite(finished)
            or finished < started
        ):
            raise RunnerOutputError("评测时钟结果非法")
        latency = float(finished - started)
        audited_retry_count = injector.verify_expected_usage(
            case["expected_retry_count"]
        )
        actual = _normalize_actual(raw_actual, len(case["turns"]))
        if (
            audited_retry_count is not None
            and actual["retry_count"] != audited_retry_count
        ):
            raise RunnerOutputError("fault hook 与 retry_count 不一致")
        scored = score_case(case, actual)
        scored_results.append(scored)
        latencies.append(latency)
        total_model_calls += actual["model_calls"]
        total_tool_calls += actual["tool_calls"]
        total_tool_successes += actual["tool_successes"]

        # Never persist case input, answer, persistence text, or free-form
        # observed behavior.  Scoring happens first; only bounded observations
        # and fixed-label scorer output are retained.
        report_results.append(
            {
                "case_id": case["case_id"],
                "scope": case["scope"],
                "group": case["group"],
                "latency_seconds": latency,
                "model_calls": actual["model_calls"],
                "tool_calls": actual["tool_calls"],
                "tool_successes": actual["tool_successes"],
                "status_trace": actual["status_trace"],
                "citations": actual["citations"],
                "observation": {
                    "intent": actual["intent"],
                    "priority": actual["priority"],
                    "risk_level": actual["risk_level"],
                    "status": actual["status"],
                    "missing_fields": actual["missing_fields"],
                    "illegal_transition_count": actual["illegal_transition_count"],
                    "secret_found_in_persistence": actual[
                        "secret_found_in_persistence"
                    ],
                    "draft_sent": actual["draft_sent"],
                    "retry_count": actual["retry_count"],
                },
                "scores": _project_report_scores(scored),
            }
        )

    try:
        raw_v1_observations = runner.run_v1_compatibility(
            _project_v1_runner_cases(checked_v1_cases)
        )
    except Exception:
        raise RunnerExecutionError("V1 兼容 runner 执行失败") from None
    v1_measurement = _score_v1_observations(
        raw_v1_observations, checked_v1_cases
    )
    metrics = aggregate_metrics(scored_results)
    return {
        "tag": tag,
        "total_cases": len(checked_cases),
        "v2_scored_cases": len(scored_results),
        "v1_compatibility_runs": 1,
        "v1_compatibility": v1_measurement,
        "metrics": metrics,
        "latency": {
            "median_seconds": statistics.median(latencies) if latencies else 0.0,
            "p95_seconds": _percentile_95(latencies),
        },
        "operational_metrics": {
            "model_calls_total": total_model_calls,
            "tool_calls_total": total_tool_calls,
            "tool_success_rate": (
                total_tool_successes / total_tool_calls
                if total_tool_calls
                else None
            ),
            "human_takeover_rate": sum(
                result["observation"]["status"] == "escalated"
                for result in report_results
            )
            / len(report_results),
            "auto_resolution_rate": sum(
                result["observation"]["status"] == "resolved"
                for result in report_results
            )
            / len(report_results),
        },
        "results": report_results,
    }


def _write_report(report: Mapping[str, object], output_dir: Path, tag: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{tag}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{tag}.", suffix=".tmp", dir=str(output_dir)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def main(
    argv: list[str] | None = None,
    *,
    runner: object | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    parser = argparse.ArgumentParser(description="运行 KeyGuard V2 多 Agent 编排评测")
    parser.add_argument("--tag", default="keyguard-v2", help="结果文件标签")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="V2 case JSON 路径")
    parser.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="结果输出目录"
    )
    args = parser.parse_args(argv)
    if _TAG.fullmatch(args.tag) is None:
        raise ValueError("tag 只能包含字母、数字、点、下划线和连字符")

    # Resolve the real/injected runner before touching the output directory.
    active_runner = runner if runner is not None else load_configured_runner(environ)
    if not _runner_interface(active_runner):
        raise RunnerConfigurationError("评测 runner 接口非法")
    cases = load_cases(args.cases)
    v1_cases = load_v1_cases()
    report = run_evaluation(
        cases,
        runner=active_runner,
        v1_cases=v1_cases,
        tag=args.tag,
    )
    output_path = _write_report(report, Path(args.output_dir), args.tag)
    print(f"评测完成：{len(cases)} 条 V2 case；结果：{output_path}")
    return output_path


if __name__ == "__main__":
    try:
        main()
    except RunnerConfigurationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
