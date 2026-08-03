import math
import tempfile
import threading
import time
import traceback
import unittest
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from agent.orchestration.events import make_event, to_repository_event
from agent.orchestration.invoke import (
    _EXECUTION_CAPACITY,
    _active_execution_count,
    _active_execution_threads,
    ExecutionCapacityError,
    OutputValidationError,
    invoke_with_policy,
)
from agent.orchestration.routes import (
    ALLOWED_TRANSITIONS,
    IllegalRoute,
    IllegalTransition,
    assert_transition,
    route_after_diagnosis,
    route_after_entry,
    route_after_human,
    route_after_review,
    route_after_triage,
)
from agent.orchestration.state import RiskLevel, Status
from agent.security.secrets import TransactionHash
from database.ticket_db import TicketRepository


MNEMONIC = (
    "abandon ability able about above absent absorb abstract absurd abuse "
    "access accident"
)
TRANSACTION_HASH = "0x" + "a" * 64


class OrchestrationRoutesTest(unittest.TestCase):
    expected_transitions = {
        "new": {"triaged", "escalated"},
        "triaged": {"pending_user", "diagnosing", "escalated"},
        "pending_user": {"triaged", "escalated", "closed", "resolved"},
        "diagnosing": {"pending_user", "reviewing", "escalated"},
        "reviewing": {"pending_user", "resolved", "diagnosing", "escalated"},
        "escalated": {"resolved", "pending_user", "escalated"},
        "closed": set(),
        "resolved": set(),
    }

    def test_allowed_transition_matrix_is_exact_and_enforced(self):
        normalized = {
            str(current.value if isinstance(current, Status) else current): {
                target.value if isinstance(target, Status) else target
                for target in targets
            }
            for current, targets in ALLOWED_TRANSITIONS.items()
        }
        self.assertEqual(normalized, self.expected_transitions)

        for current in Status:
            for target in Status:
                with self.subTest(current=current.value, target=target.value):
                    if target.value in self.expected_transitions[current.value]:
                        self.assertIsNone(assert_transition(current, target))
                    else:
                        with self.assertRaises(IllegalTransition):
                            assert_transition(current, target)

    def test_transition_validation_rejects_unknown_non_string_and_same_state(self):
        invalid_pairs = (
            ("unknown", "triaged"),
            ("new", "unknown"),
            (1, "triaged"),
            ("new", None),
            (True, "triaged"),
            ("new", "new"),
            ("resolved", "resolved"),
        )
        for current, target in invalid_pairs:
            with self.subTest(
                current=type(current).__name__, target=type(target).__name__
            ):
                with self.assertRaises(IllegalTransition):
                    assert_transition(current, target)

        self.assertIsNone(assert_transition("escalated", "escalated"))

    def test_transition_error_does_not_echo_supplied_payload(self):
        secret_payload = "PIN 123456"
        with self.assertRaises(IllegalTransition) as captured:
            assert_transition(secret_payload, "resolved")
        self.assertNotIn(secret_payload, str(captured.exception))
        self.assertNotIn("123456", str(captured.exception))

    def test_entry_routes_high_risk_or_manual_gate_to_human(self):
        for risk_level, expected in (
            (RiskLevel.LOW, "triage"),
            (RiskLevel.MEDIUM, "triage"),
            (RiskLevel.HIGH, "human_review"),
            (RiskLevel.CRITICAL, "human_review"),
        ):
            with self.subTest(risk_level=risk_level.value):
                self.assertEqual(
                    route_after_entry({"risk_level": risk_level}), expected
                )
        self.assertEqual(
            route_after_entry({"risk_level": "low", "requires_human": True}),
            "human_review",
        )
        self.assertEqual(
            route_after_entry({"risk_level": "low", "status": "escalated"}),
            "human_review",
        )
        with self.assertRaises(IllegalRoute):
            route_after_entry({"risk_level": "low", "status": "reviewing"})

    def _triage_state(self, **overrides):
        state = {
            "status": "triaged",
            "category": "usb_connection",
            "risk_level": "low",
            "clarity": "clear",
            "clarification_question": "",
            "clarification_options": [],
            "suggested_route": "diagnose",
            "missing_fields": [],
            "requires_human": False,
        }
        state.update(overrides)
        return state

    def test_triage_routes_normal_missing_and_security_cases(self):
        self.assertEqual(route_after_triage(self._triage_state()), "start_diagnosis")
        self.assertEqual(
            route_after_triage(
                self._triage_state(
                    clarity="ambiguous",
                    suggested_route="clarify",
                    clarification_question="你遇到的是哪一类问题？",
                    clarification_options=["无法开机", "无法连接"],
                )
            ),
            "pending_user",
        )
        self.assertEqual(
            route_after_triage(
                self._triage_state(
                    category="firmware_repair",
                    clarity="partial",
                    missing_fields=["device_model"],
                )
            ),
            "start_diagnosis",
        )
        self.assertEqual(
            route_after_triage(self._triage_state(category="security_report")),
            "escalate",
        )
        self.assertEqual(
            route_after_triage(self._triage_state(suggested_route="escalate")),
            "escalate",
        )

    def test_triage_never_downgrades_high_risk_or_manual_gate(self):
        for changes in (
            {"risk_level": "high"},
            {"risk_level": "critical"},
            {"requires_human": True},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    route_after_triage(self._triage_state(**changes)), "escalate"
                )

    def test_triage_rejects_missing_unknown_and_contradictory_values(self):
        invalid_states = (
            {},
            self._triage_state(status="new"),
            self._triage_state(risk_level="severe"),
            self._triage_state(category="unknown"),
            self._triage_state(suggested_route="unknown"),
            self._triage_state(clarity="unknown"),
            self._triage_state(missing_fields="device_model"),
            self._triage_state(missing_fields=["unknown"]),
            self._triage_state(requires_human=1),
            self._triage_state(
                clarity="ambiguous",
                suggested_route="clarify",
                clarification_question="",
                clarification_options=[],
            ),
            self._triage_state(
                clarity="clear",
                suggested_route="diagnose",
                missing_fields=["device_model"],
            ),
            {"status": "escalated", "category": "unknown"},
            {"status": "escalated", "suggested_route": "unknown"},
            {"status": "escalated", "missing_fields": "device_model"},
        )
        for index, state in enumerate(invalid_states):
            with self.subTest(case=index), self.assertRaises(IllegalRoute):
                route_after_triage(state)

    def test_diagnosis_route_validates_status_and_preserves_risk_gate(self):
        self.assertEqual(
            route_after_diagnosis({"status": "pending_user"}), "await_user"
        )
        self.assertEqual(route_after_diagnosis({"status": "reviewing"}), "review")
        self.assertEqual(
            route_after_diagnosis({"status": "escalated"}), "human_review"
        )
        self.assertEqual(
            route_after_diagnosis(
                {"status": "reviewing", "risk_level": "high"}
            ),
            "human_review",
        )
        self.assertEqual(
            route_after_diagnosis(
                {"status": "reviewing", "requires_human": True}
            ),
            "human_review",
        )
        for state in (
            {},
            {"status": "new"},
            {"status": "reviewing", "risk_level": "unknown"},
            {"status": "reviewing", "requires_human": "yes"},
            {"status": "new", "risk_level": "high"},
            {"status": "resolved", "requires_human": True},
        ):
            with self.subTest(state_keys=sorted(state)), self.assertRaises(
                IllegalRoute
            ):
                route_after_diagnosis(state)

    def test_review_revision_boundary_and_manual_gate(self):
        self.assertEqual(
            route_after_review(
                {
                    "review_decision": "approve",
                    "revision_count": 0,
                    "requires_human": False,
                }
            ),
            "finalize",
        )
        self.assertEqual(
            route_after_review(
                {
                    "review_decision": "approve",
                    "revision_count": 0,
                    "requires_human": True,
                }
            ),
            "escalate",
        )

    def test_review_low_risk_fallback_routes_back_to_customer(self):
        self.assertEqual(
            route_after_review(
                {
                    "status": "pending_user",
                    "waiting_reason": "clarification",
                    "requires_human": False,
                    "final_answer": "",
                }
            ),
            "await_user",
        )
        for changes in (
            {"waiting_reason": "resolution_confirmation"},
            {"requires_human": True},
            {"final_answer": "未审核答复"},
        ):
            with self.subTest(changes=changes), self.assertRaises(IllegalRoute):
                route_after_review(
                    {
                        "status": "pending_user",
                        "waiting_reason": "clarification",
                        "requires_human": False,
                        "final_answer": "",
                        **changes,
                    }
                )
        self.assertEqual(
            route_after_review(
                {
                    "review_decision": "revise",
                    "revision_count": 0,
                    "requires_human": False,
                }
            ),
            "revision",
        )
        for revision_count in (1, 2, 99):
            with self.subTest(revision_count=revision_count):
                self.assertEqual(
                    route_after_review(
                        {
                            "review_decision": "revise",
                            "revision_count": revision_count,
                            "requires_human": False,
                        }
                    ),
                    "escalate",
                )
        self.assertEqual(
            route_after_review(
                {
                    "review_decision": "escalate",
                    "revision_count": 0,
                    "requires_human": False,
                }
            ),
            "escalate",
        )

    def test_review_rejects_unknown_or_malformed_state(self):
        base = {
            "review_decision": "approve",
            "revision_count": 0,
            "requires_human": False,
        }
        cases = (
            {},
            {**base, "review_decision": "unknown"},
            {**base, "revision_count": -1},
            {**base, "revision_count": True},
            {**base, "requires_human": 0},
            {**base, "risk_level": "unknown"},
            {**base, "status": "new", "risk_level": "high"},
            {**base, "status": "resolved", "requires_human": True},
        )
        for index, state in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(IllegalRoute):
                route_after_review(state)

    def test_review_high_risk_is_sticky_even_when_approved(self):
        self.assertEqual(
            route_after_review(
                {
                    "review_decision": "approve",
                    "revision_count": 0,
                    "requires_human": False,
                    "risk_level": "critical",
                }
            ),
            "escalate",
        )

    def test_review_revise_is_allowed_exactly_once_from_reviewing(self):
        base = {
            "status": "reviewing",
            "review_decision": "revise",
            "requires_human": False,
        }
        self.assertEqual(
            route_after_review({**base, "revision_count": 0}), "revision"
        )
        for revision_count in (1, 2, 100):
            with self.subTest(revision_count=revision_count):
                self.assertEqual(
                    route_after_review(
                        {**base, "revision_count": revision_count}
                    ),
                    "escalate",
                )
        self.assertEqual(
            route_after_review(
                {
                    **base,
                    "status": "escalated",
                    "revision_count": 0,
                }
            ),
            "escalate",
        )

    def test_human_routes_only_supported_statuses(self):
        self.assertEqual(route_after_human({"status": "resolved"}), "end")
        self.assertEqual(
            route_after_human({"status": "pending_user"}), "await_user"
        )
        self.assertEqual(
            route_after_human({"status": "escalated"}), "human_review"
        )
        for state in ({}, {"status": "reviewing"}, {"status": "unknown"}):
            with self.subTest(state=state), self.assertRaises(IllegalRoute):
                route_after_human(state)


class OrchestrationEventTest(unittest.TestCase):
    def _event(self, **overrides):
        values = {
            "command_id": "request:route-test",
            "step_index": 1,
            "node_name": "triage",
            "event_type": "ticket.triaged",
            "summary": "完成安全分诊",
            "from_status": Status.NEW,
            "to_status": Status.TRIAGED,
            "metadata": {"priority": "P2", "scores": [0.25, 1, None]},
        }
        values.update(overrides)
        return make_event(**values)

    def test_make_event_returns_exact_contract_and_deep_copies_metadata(self):
        metadata = {"nested": {"labels": ["safe"]}}
        event = self._event(metadata=metadata)

        self.assertEqual(
            set(event),
            {
                "command_id",
                "step_index",
                "node_name",
                "event_type",
                "summary",
                "from_status",
                "to_status",
                "metadata",
            },
        )
        self.assertEqual(event["from_status"], "new")
        self.assertEqual(event["to_status"], "triaged")
        metadata["nested"]["labels"].append("mutated")
        self.assertEqual(event["metadata"], {"nested": {"labels": ["safe"]}})

    def test_make_event_defaults_only_none_metadata(self):
        self.assertEqual(self._event(metadata=None)["metadata"], {})
        for metadata in ([], "", 0, False):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self._event(metadata=metadata)

    def test_make_event_rejects_invalid_fields_status_and_json(self):
        cases = (
            {"command_id": ""},
            {"node_name": "   "},
            {"event_type": None},
            {"summary": ""},
            {"step_index": 0},
            {"step_index": True},
            {"step_index": 1.0},
            {"from_status": "unknown"},
            {"to_status": 1},
            {"metadata": {1: "bad"}},
            {"metadata": {"value": math.nan}},
            {"metadata": {"value": math.inf}},
            {"metadata": {"value": (1, 2)}},
            {"metadata": {"value": object()}},
            {"metadata": {MNEMONIC: "unsafe key"}},
        )
        for index, changes in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                self._event(**changes)

        cyclic = {}
        cyclic["self"] = cyclic
        with self.assertRaises(ValueError):
            self._event(metadata=cyclic)

    def test_make_event_rejects_unredacted_secrets(self):
        cases = (
            {"summary": f"用户助记词是 {MNEMONIC}"},
            {"metadata": {"note": f"助记词: {MNEMONIC}"}},
            {"metadata": {"password": "plain-text-password"}},
        )
        for index, changes in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                self._event(**changes)

    def test_transaction_hash_requires_explicit_type_at_event_ingress(self):
        typed = self._event(
            metadata={"transaction_hash": TransactionHash(TRANSACTION_HASH)}
        )

        self.assertEqual(typed["metadata"]["transaction_hash"], TRANSACTION_HASH)
        for metadata in (
            {"transaction_hash": TRANSACTION_HASH},
            {"transaction_hash": "not-a-transaction-hash"},
            {"transaction_hash": None},
            {"transaction_hash": 1},
            {"transaction_hash": False},
            {"transaction_hash": []},
            {"nested": {"transaction_hash": TransactionHash(TRANSACTION_HASH)}},
            {"hashes": [TransactionHash(TRANSACTION_HASH)]},
        ):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self._event(metadata=metadata)

    def test_event_round_trips_without_pickle(self):
        event = self._event()
        serializer = JsonPlusSerializer(
            pickle_fallback=False,
            allowed_json_modules=None,
            allowed_msgpack_modules=None,
        )

        encoded = serializer.dumps_typed(event)

        self.assertNotEqual(encoded[0], "pickle")
        self.assertEqual(serializer.loads_typed(encoded), event)

    def test_repository_adapter_removes_command_id_and_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = TicketRepository(str(Path(directory) / "tickets.db"))
            ticket = repository.create_ticket(
                "event-adapter-request", "user-1", "USB 连接失败", []
            )
            command_id = "request:event-adapter"
            decision = repository.begin_command(
                ticket["ticket_id"], command_id, "user_input", 30
            )
            event = self._event(command_id=command_id)

            payload = to_repository_event(event)
            event_id = repository.append_event(
                ticket["ticket_id"],
                command_id,
                decision.lease_version,
                **payload,
            )

        self.assertNotIn("command_id", payload)
        self.assertIsInstance(event_id, int)
        self.assertGreater(event_id, 0)

    def test_typed_transaction_hash_round_trips_and_repository_accepts_it(self):
        event = self._event(
            metadata={"transaction_hash": TransactionHash(TRANSACTION_HASH)}
        )
        serializer = JsonPlusSerializer(
            pickle_fallback=False,
            allowed_json_modules=None,
            allowed_msgpack_modules=None,
        )
        restored = serializer.loads_typed(serializer.dumps_typed(event))

        self.assertEqual(restored["metadata"]["transaction_hash"], TRANSACTION_HASH)
        transaction_hash = TransactionHash(TRANSACTION_HASH)
        payload = to_repository_event(restored, transaction_hash=transaction_hash)
        self.assertIsInstance(
            payload["metadata"]["transaction_hash"], TransactionHash
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = TicketRepository(str(Path(directory) / "tickets.db"))
            ticket = repository.create_ticket(
                "typed-hash-event", "user-1", "查询交易状态", []
            )
            command_id = event["command_id"]
            decision = repository.begin_command(
                ticket["ticket_id"], command_id, "user_input", 30
            )
            event_id = repository.append_event(
                ticket["ticket_id"],
                command_id,
                decision.lease_version,
                **payload,
            )

        self.assertGreater(event_id, 0)

    def test_repository_adapter_requires_matching_transaction_hash_capability(self):
        transaction_hash = TransactionHash(TRANSACTION_HASH)
        event = self._event(metadata={"transaction_hash": transaction_hash})
        altered_hash = TransactionHash("0x" + "b" * 64)

        with self.assertRaises(ValueError):
            to_repository_event(event)
        with self.assertRaises(ValueError):
            to_repository_event(event, transaction_hash=altered_hash)
        with self.assertRaises(ValueError):
            to_repository_event(self._event(), transaction_hash=transaction_hash)
        with self.assertRaises(ValueError):
            to_repository_event(event, transaction_hash=TRANSACTION_HASH)

        tampered = dict(event)
        tampered["metadata"] = {"transaction_hash": altered_hash.value}
        with self.assertRaises(ValueError):
            to_repository_event(tampered, transaction_hash=transaction_hash)

    def test_repository_adapter_revalidates_tampered_events(self):
        safe_event = self._event()
        tampered_events = []
        for field, value in (
            ("command_id", ""),
            ("step_index", 0),
            ("node_name", " "),
            ("from_status", "unknown"),
        ):
            tampered = dict(safe_event)
            tampered[field] = value
            tampered_events.append(tampered)
        invalid_hash = dict(safe_event)
        invalid_hash["metadata"] = {"transaction_hash": "not-a-hash"}
        tampered_events.append(invalid_hash)
        extra_field = dict(safe_event)
        extra_field["raw_payload"] = "must-not-pass"
        tampered_events.append(extra_field)

        for index, tampered in enumerate(tampered_events):
            with self.subTest(case=index), self.assertRaises(ValueError):
                to_repository_event(tampered)


class InvokeWithPolicyTest(unittest.TestCase):
    def test_success_returns_immediately_without_retry(self):
        calls = []

        def succeed():
            calls.append(1)
            return "ok"

        self.assertEqual(invoke_with_policy(succeed, 0.1, 3), "ok")
        self.assertEqual(len(calls), 1)

    def test_retries_only_declared_retryable_errors(self):
        retryable_types = (ValueError, TypeError, OutputValidationError)
        for error_type in retryable_types:
            calls = []

            def flaky(error_type=error_type):
                calls.append(1)
                if len(calls) < 3:
                    raise error_type("payload must not escape")
                return "ok"

            with self.subTest(error_type=error_type.__name__):
                self.assertEqual(invoke_with_policy(flaky, 0.1, 2), "ok")
                self.assertEqual(len(calls), 3)

    def test_attempt_count_is_exact_and_final_error_is_sanitized_and_chained(self):
        calls = []

        def fail():
            calls.append(1)
            raise ValueError("private payload 123456")

        with self.assertRaises(ValueError) as captured:
            invoke_with_policy(fail, 0.1, 2)

        self.assertEqual(len(calls), 3)
        self.assertNotIn("payload", str(captured.exception))
        self.assertNotIn("123456", str(captured.exception))
        self.assertIsNone(captured.exception.__cause__)
        rendered = "".join(
            traceback.format_exception(
                type(captured.exception),
                captured.exception,
                captured.exception.__traceback__,
            )
        )
        self.assertNotIn("private payload 123456", rendered)

    def test_non_retryable_exception_is_not_retried_and_is_sanitized(self):
        calls = []

        def fail():
            calls.append(1)
            raise RuntimeError("secret payload 123456")

        with self.assertRaises(RuntimeError) as captured:
            invoke_with_policy(fail, 0.1, 3)

        self.assertEqual(len(calls), 1)
        self.assertNotIn("payload", str(captured.exception))
        self.assertNotIn("123456", str(captured.exception))
        self.assertIsNone(captured.exception.__cause__)
        self.assertNotIn("secret", repr(vars(captured.exception)))
        rendered = "".join(
            traceback.format_exception(
                type(captured.exception),
                captured.exception,
                captured.exception.__traceback__,
            )
        )
        self.assertNotIn("secret payload 123456", rendered)

    def test_custom_exception_is_recreated_without_original_object_or_traceback(self):
        secret_value = "CUSTOM_PAYLOAD_8675309"

        class PayloadError(Exception):
            def __init__(self, payload):
                self.payload = payload
                super().__init__(payload)

        def fail_in_worker():
            raise PayloadError(secret_value)

        with self.assertRaises(PayloadError) as captured:
            invoke_with_policy(fail_in_worker, 0.1, 0)

        error = captured.exception
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(secret_value, str(error))
        self.assertNotIn(secret_value, repr(vars(error)))
        frames = traceback.extract_tb(error.__traceback__)
        self.assertNotIn("fail_in_worker", {frame.name for frame in frames})

    def test_completed_timeout_error_is_not_retried(self):
        calls = []

        def timeout_from_worker():
            calls.append(1)
            raise TimeoutError("upstream timeout payload")

        with self.assertRaises(TimeoutError) as captured:
            invoke_with_policy(timeout_from_worker, 0.1, 4)

        self.assertEqual(len(calls), 1)
        self.assertIsNone(captured.exception.__cause__)
        self.assertNotIn("payload", str(captured.exception))

    def test_timeout_returns_quickly_and_never_retries(self):
        calls = []
        started = threading.Event()
        release = threading.Event()

        def slow():
            calls.append(1)
            started.set()
            release.wait()

        before = time.monotonic()
        try:
            with self.assertRaises(TimeoutError) as captured:
                invoke_with_policy(slow, 0.01, 3)
            elapsed = time.monotonic() - before

            self.assertTrue(started.is_set())
            self.assertEqual(len(calls), 1)
            self.assertLess(elapsed, 0.08)
            self.assertIsNone(captured.exception.__cause__)
        finally:
            release.set()
            deadline = time.monotonic() + 1
            while _active_execution_count() and time.monotonic() < deadline:
                time.sleep(0.005)

    def test_worker_capacity_is_bounded_daemon_and_fails_fast(self):
        release = threading.Event()

        def blocked():
            release.wait()

        try:
            for _ in range(_EXECUTION_CAPACITY):
                with self.assertRaises(TimeoutError):
                    invoke_with_policy(blocked, 0.01, 9)

            self.assertEqual(_active_execution_count(), _EXECUTION_CAPACITY)
            active_threads = _active_execution_threads()
            self.assertEqual(len(active_threads), _EXECUTION_CAPACITY)
            self.assertTrue(all(thread.daemon for thread in active_threads))
            before = time.monotonic()
            with self.assertRaises(ExecutionCapacityError) as captured:
                invoke_with_policy(lambda: "never submitted", 1, 0)
            self.assertLess(time.monotonic() - before, 0.05)
            self.assertIsNone(captured.exception.__cause__)
        finally:
            release.set()
            deadline = time.monotonic() + 1
            while _active_execution_count() and time.monotonic() < deadline:
                time.sleep(0.005)
        self.assertEqual(_active_execution_count(), 0)

    def test_rejects_invalid_policy_parameters(self):
        invalid_timeouts = (0, -1, True, "1", math.nan, math.inf)
        for timeout in invalid_timeouts:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                invoke_with_policy(lambda: None, timeout, 0)

        invalid_retries = (-1, True, 1.5, "1")
        for retries in invalid_retries:
            with self.subTest(retries=retries), self.assertRaises(ValueError):
                invoke_with_policy(lambda: None, 1, retries)

        with self.assertRaises(ValueError):
            invoke_with_policy(None, 1, 0)


if __name__ == "__main__":
    unittest.main()
