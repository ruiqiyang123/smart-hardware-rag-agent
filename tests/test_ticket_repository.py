import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.security.secrets import (
    SanitizedText,
    TransactionHash,
    contains_unredacted_secret,
)
from database.ticket_db import (
    CommandDisposition,
    CommandType,
    IdempotencyConflictError,
    TicketRepository,
)


MNEMONIC = (
    "abandon ability able about above absent absorb abstract absurd abuse access accident"
)
TRANSACTION_HASH = "0x" + "a" * 64


class TicketRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "tickets.db"
        self.repo = TicketRepository(str(self.db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def _ticket(self, request_id="req-1", user_id="1001"):
        return self.repo.create_ticket(
            request_id, user_id, "蓝牙连接失败", ["secret_exposure"]
        )

    def _started(self, request_id="req-1", command_type="user_input"):
        ticket = self._ticket(request_id)
        command_id = f"request:{request_id}"
        decision = self.repo.begin_command(
            ticket["ticket_id"], command_id, command_type, 130
        )
        return ticket, command_id, decision

    def _event(self, ticket, command_id, lease_version, step_index=1, **overrides):
        values = {
            "ticket_id": ticket["ticket_id"],
            "command_id": command_id,
            "lease_version": lease_version,
            "step_index": step_index,
            "node_name": "triage",
            "event_type": "ticket.triaged",
            "from_status": "new",
            "to_status": "triaged",
            "summary": "完成分类",
            "metadata": {"priority": "P2"},
        }
        values.update(overrides)
        return values

    def _command_row(self, command_id):
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM ticket_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        return dict(row)

    def test_create_ticket_is_idempotent_by_request_id(self):
        first = self.repo.create_ticket("req-1", "1001", "蓝牙连接失败", [])
        second = self.repo.create_ticket("req-1", "1001", "蓝牙连接失败", [])
        self.assertEqual(first["ticket_id"], second["ticket_id"])
        self.assertEqual(len(self.repo.list_tickets()), 1)

    def test_create_ticket_rejects_request_id_payload_conflicts(self):
        self._ticket()
        conflicts = [
            ("2002", "蓝牙连接失败", ["secret_exposure"]),
            ("1001", "USB 连接失败", ["secret_exposure"]),
            ("1001", "蓝牙连接失败", ["phishing"]),
        ]
        for user_id, sanitized_input, risk_flags in conflicts:
            with self.subTest(
                user_id=user_id,
                sanitized_input=sanitized_input,
                risk_flags=risk_flags,
            ), self.assertRaises(IdempotencyConflictError):
                self.repo.create_ticket(
                    "req-1", user_id, sanitized_input, risk_flags
                )

    def test_concurrent_create_ticket_writes_one_row(self):
        workers = 6
        barrier = threading.Barrier(workers)
        ticket_ids = []
        errors = []
        lock = threading.Lock()

        def create():
            try:
                barrier.wait()
                ticket = self.repo.create_ticket(
                    "req-shared", "1001", "蓝牙连接失败", []
                )
                with lock:
                    ticket_ids.append(ticket["ticket_id"])
            except Exception as error:  # pragma: no cover - asserted below
                with lock:
                    errors.append(error)

        threads = [threading.Thread(target=create) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ticket_ids)), 1)
        self.assertEqual(len(self.repo.list_tickets()), 1)

    def test_command_completed_returns_saved_result_and_fencing_token(self):
        ticket, command_id, started = self._started("req-2")
        self.assertEqual(started.disposition, CommandDisposition.START)
        self.assertEqual(started.lease_version, 1)
        completed = self.repo.commit_command_result(
            ticket["ticket_id"],
            command_id,
            started.lease_version,
            {"status": "triaged"},
            [
                {
                    key: value
                    for key, value in self._event(
                        ticket, command_id, started.lease_version
                    ).items()
                    if key not in {"ticket_id", "command_id", "lease_version"}
                }
            ],
        )
        duplicate = self.repo.begin_command(
            ticket["ticket_id"], command_id, "user_input", lease_seconds=130
        )
        self.assertEqual(duplicate.disposition, CommandDisposition.COMPLETED)
        self.assertEqual(duplicate.lease_version, 1)
        self.assertEqual(duplicate.result_status, "triaged")
        self.assertEqual(duplicate.result_event_id, completed.result_event_id)

    def test_repository_exposes_only_atomic_workflow_completion_api(self):
        self.assertFalse(hasattr(self.repo, "complete_command"))
        self.assertFalse(hasattr(self.repo, "update_ticket"))
        self.assertTrue(hasattr(self.repo, "commit_command_result"))
        self.assertTrue(hasattr(self.repo, "admin_update_ticket"))

    def test_begin_command_rejects_non_positive_or_non_integer_lease(self):
        ticket = self._ticket()
        for lease_seconds in (0, -1, 1.5, True):
            with self.subTest(lease_seconds=lease_seconds), self.assertRaises(
                ValueError
            ):
                self.repo.begin_command(
                    ticket["ticket_id"],
                    f"request:invalid:{lease_seconds}",
                    "user_input",
                    lease_seconds,
                )

    def test_persistence_boundaries_reject_blank_identifiers_and_event_fields(self):
        for values in [
            ("", "1001", "安全文本", []),
            ("req-1", " ", "安全文本", []),
            ("req-1", "1001", "", []),
            ("req-1", "1001", "安全文本", "[]"),
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.repo.create_ticket(*values)

        ticket = self._ticket()
        for command_id, command_type in [("", "user_input"), ("command", " ")]:
            with self.subTest(
                command_id=command_id, command_type=command_type
            ), self.assertRaises(ValueError):
                self.repo.begin_command(
                    ticket["ticket_id"], command_id, command_type, 130
                )

        ticket = self._ticket("command-types")
        self.assertEqual(
            {member.value for member in CommandType},
            {
                "user_input",
                "human_approve",
                "human_edit_send",
                "human_ask",
                "human_reject",
            },
        )
        with self.assertRaises(ValueError):
            self.repo.begin_command(
                ticket["ticket_id"], "request:invalid-type", "human_action", 130
            )
        with sqlite3.connect(self.db_path) as connection, self.assertRaises(
            sqlite3.IntegrityError
        ):
            connection.execute(
                """
                INSERT INTO ticket_commands (
                    command_id, ticket_id, command_type, status,
                    lease_expires_at, lease_version, created_at, updated_at
                ) VALUES (?, ?, 'human_action', 'in_progress', ?, 1, ?, ?)
                """,
                (
                    "request:db-invalid-type",
                    ticket["ticket_id"],
                    (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        _, command_id, decision = self._started("event-boundary")
        for overrides in [
            {"node_name": ""},
            {"event_type": " "},
            {"summary": ""},
            {"metadata": []},
            {"from_status": "invalid"},
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.repo.append_event(
                    **self._event(
                        self.repo.get_ticket(
                            self._command_row(command_id)["ticket_id"]
                        ),
                        command_id,
                        decision.lease_version,
                        **overrides,
                    )
                )

    def test_database_checks_reject_invalid_ticket_values(self):
        ticket = self._ticket()
        invalid_statements = [
            ("UPDATE tickets SET status = 'invalid' WHERE ticket_id = ?",),
            ("UPDATE tickets SET priority = 'P9' WHERE ticket_id = ?",),
            ("UPDATE tickets SET risk_level = 'severe' WHERE ticket_id = ?",),
            ("UPDATE tickets SET review_decision = 'passed' WHERE ticket_id = ?",),
            ("UPDATE tickets SET revision_count = -1 WHERE ticket_id = ?",),
            ("UPDATE tickets SET response_version = -1 WHERE ticket_id = ?",),
            ("UPDATE tickets SET requires_human = 2 WHERE ticket_id = ?",),
            ("UPDATE tickets SET risk_flags_json = '{}' WHERE ticket_id = ?",),
        ]
        for (statement,) in invalid_statements:
            with self.subTest(statement=statement), sqlite3.connect(
                self.db_path
            ) as connection, self.assertRaises(sqlite3.IntegrityError):
                connection.execute(statement, (ticket["ticket_id"],))

    def test_existing_command_rejects_ticket_or_type_conflict(self):
        first = self._ticket("req-1")
        second = self._ticket("req-2")
        self.repo.begin_command(
            first["ticket_id"], "request:shared", "user_input", 130
        )
        conflicts = [
            (second["ticket_id"], "user_input", None),
            (first["ticket_id"], "human_approve", None),
            (first["ticket_id"], "user_input", "a" * 64),
        ]
        for ticket_id, command_type, payload_fingerprint in conflicts:
            with self.subTest(
                ticket_id=ticket_id, command_type=command_type
            ), self.assertRaises(IdempotencyConflictError):
                self.repo.begin_command(
                    ticket_id,
                    "request:shared",
                    command_type,
                    130,
                    payload_fingerprint=payload_fingerprint,
                )

    def test_expired_command_renews_once_and_fences_old_worker(self):
        ticket, command_id, started = self._started()
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE ticket_commands SET lease_expires_at = ? WHERE command_id = ?",
                (expired, command_id),
            )

        workers = 4
        barrier = threading.Barrier(workers)
        decisions = []
        errors = []
        result_lock = threading.Lock()

        def begin():
            try:
                barrier.wait()
                decision = self.repo.begin_command(
                    ticket["ticket_id"], command_id, "user_input", 130
                )
                with result_lock:
                    decisions.append(decision)
            except Exception as error:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(error)

        threads = [threading.Thread(target=begin) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(
            sum(d.disposition == CommandDisposition.RESUME for d in decisions), 1
        )
        self.assertEqual(
            sum(d.disposition == CommandDisposition.IN_PROGRESS for d in decisions),
            workers - 1,
        )
        self.assertEqual({d.lease_version for d in decisions}, {2})

        with self.assertRaises(ValueError):
            self.repo.append_event(
                **self._event(ticket, command_id, started.lease_version)
            )
        with self.assertRaises(ValueError):
            self.repo.fail_command(command_id, started.lease_version, "STALE_WORKER")

    def test_one_ticket_allows_only_one_distinct_in_progress_command(self):
        ticket = self._ticket("single-active")
        barrier = threading.Barrier(2)
        decisions = []
        errors = []
        lock = threading.Lock()

        def begin(command_id):
            try:
                barrier.wait()
                decision = self.repo.begin_command(
                    ticket["ticket_id"], command_id, "user_input", 130
                )
                with lock:
                    decisions.append((command_id, decision))
            except Exception as error:  # pragma: no cover - asserted below
                with lock:
                    errors.append(error)

        threads = [
            threading.Thread(target=begin, args=("request:parallel-a",)),
            threading.Thread(target=begin, args=("request:parallel-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0][1].disposition, CommandDisposition.START)
        self.assertEqual(len(errors), 1)
        self.assertIn("原 command_id", str(errors[0]))
        with sqlite3.connect(self.db_path) as connection:
            active = connection.execute(
                "SELECT COUNT(*) FROM ticket_commands WHERE ticket_id = ? AND status = 'in_progress'",
                (ticket["ticket_id"],),
            ).fetchone()[0]
        self.assertEqual(active, 1)

    def test_expired_active_command_must_resume_with_original_id(self):
        ticket, command_id, started = self._started("expired-original-only")
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE ticket_commands SET lease_expires_at = ? WHERE command_id = ?",
                (expired, command_id),
            )

        with self.assertRaisesRegex(ValueError, "原 command_id"):
            self.repo.begin_command(
                ticket["ticket_id"], "request:different-id", "user_input", 130
            )
        resumed = self.repo.begin_command(
            ticket["ticket_id"], command_id, "user_input", 130
        )
        self.assertEqual(resumed.disposition, CommandDisposition.RESUME)
        self.assertEqual(resumed.lease_version, started.lease_version + 1)

    def test_expired_current_lease_cannot_append_fail_or_commit(self):
        operations = ("append", "fail", "commit")
        for operation in operations:
            with self.subTest(operation=operation):
                ticket, command_id, started = self._started(f"expired-{operation}")
                expired = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                ).isoformat()
                with sqlite3.connect(self.db_path) as connection:
                    connection.execute(
                        "UPDATE ticket_commands SET lease_expires_at = ? WHERE command_id = ?",
                        (expired, command_id),
                    )

                with self.assertRaisesRegex(ValueError, "lease"):
                    if operation == "append":
                        self.repo.append_event(
                            **self._event(ticket, command_id, started.lease_version)
                        )
                    elif operation == "fail":
                        self.repo.fail_command(
                            command_id, started.lease_version, "MODEL_TIMEOUT"
                        )
                    else:
                        event = {
                            key: value
                            for key, value in self._event(
                                ticket, command_id, started.lease_version
                            ).items()
                            if key not in {
                                "ticket_id",
                                "command_id",
                                "lease_version",
                            }
                        }
                        self.repo.commit_command_result(
                            ticket["ticket_id"],
                            command_id,
                            started.lease_version,
                            {"status": "triaged"},
                            [event],
                        )
                self.assertEqual(self._command_row(command_id)["status"], "in_progress")
                self.assertEqual(self.repo.get_ticket(ticket["ticket_id"])["status"], "new")
                self.assertEqual(len(self.repo.list_events(ticket["ticket_id"])), 1)

    def test_command_and_accepted_event_are_created_in_one_transaction(self):
        with self.assertRaises(ValueError):
            self.repo.begin_command(
                "KG-DOES-NOT-EXIST", "request:missing", "user_input", 130
            )
        with sqlite3.connect(self.db_path) as connection:
            command_count = connection.execute(
                "SELECT COUNT(*) FROM ticket_commands WHERE command_id = ?",
                ("request:missing",),
            ).fetchone()[0]
            event_count = connection.execute(
                "SELECT COUNT(*) FROM ticket_events WHERE command_id = ?",
                ("request:missing",),
            ).fetchone()[0]
        self.assertEqual((command_count, event_count), (0, 0))

    def test_fail_enforces_version_and_strict_terminal_idempotency(self):
        _, failed_id, failed = self._started("failed")
        self.repo.fail_command(failed_id, failed.lease_version, "MODEL_TIMEOUT")
        self.repo.fail_command(failed_id, failed.lease_version, "MODEL_TIMEOUT")
        with self.assertRaises(ValueError):
            self.repo.fail_command(failed_id, 99, "MODEL_TIMEOUT")
        with self.assertRaises(ValueError):
            self.repo.fail_command(failed_id, failed.lease_version, "DIFFERENT_ERROR")

        with self.assertRaises(ValueError):
            self.repo.fail_command("missing", 1, "UNKNOWN")

    def test_events_and_result_event_must_belong_to_command_and_ticket(self):
        first, first_command, first_decision = self._started("first")
        second, second_command, second_decision = self._started("second")
        with self.assertRaises(IdempotencyConflictError):
            self.repo.append_event(
                **self._event(second, first_command, first_decision.lease_version)
            )
        with self.assertRaises(IdempotencyConflictError):
            self.repo.append_event(
                **self._event(
                    second,
                    first_command,
                    first_decision.lease_version,
                )
            )
        self.assertEqual(self._command_row(first_command)["status"], "in_progress")

    def test_commit_requires_a_matching_explicit_terminal_event(self):
        invalid_events = [
            {"step_index": 0, "event_type": "command.accepted"},
            {"step_index": 1, "event_type": "diagnosis.completed"},
            {"step_index": 1, "event_type": "ticket.triaged", "to_status": "resolved"},
        ]
        for index, overrides in enumerate(invalid_events):
            ticket, command_id, decision = self._started(f"terminal-{index}")
            event = {
                key: value
                for key, value in self._event(
                    ticket,
                    command_id,
                    decision.lease_version,
                    **overrides,
                ).items()
                if key not in {"ticket_id", "command_id", "lease_version"}
            }
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.repo.commit_command_result(
                    ticket["ticket_id"],
                    command_id,
                    decision.lease_version,
                    {"status": "triaged"},
                    [event],
                )
            self.assertEqual(self._command_row(command_id)["status"], "in_progress")
            self.assertEqual(len(self.repo.list_events(ticket["ticket_id"])), 1)

        ticket, command_id, decision = self._started("terminal-empty")
        with self.assertRaises(ValueError):
            self.repo.commit_command_result(
                ticket["ticket_id"],
                command_id,
                decision.lease_version,
                {"status": "triaged"},
                [],
            )

    def test_commit_rejects_stale_ticket_status_and_broken_event_chain(self):
        ticket, command_id, decision = self._started("stale-ticket-status")
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE tickets SET status = 'triaged' WHERE ticket_id = ?",
                (ticket["ticket_id"],),
            )
        stale_event = {
            key: value
            for key, value in self._event(
                ticket,
                command_id,
                decision.lease_version,
                event_type="ticket.escalated",
                from_status="new",
                to_status="escalated",
            ).items()
            if key not in {"ticket_id", "command_id", "lease_version"}
        }
        with self.assertRaisesRegex(ValueError, "当前状态"):
            self.repo.commit_command_result(
                ticket["ticket_id"],
                command_id,
                decision.lease_version,
                {"status": "escalated"},
                [stale_event],
            )
        self.assertEqual(self.repo.get_ticket(ticket["ticket_id"])["status"], "triaged")

        broken_ticket, broken_command, broken_decision = self._started("broken-chain")
        broken_events = []
        for step, from_status, to_status, event_type in (
            (1, "new", "triaged", "triage.completed"),
            (2, "diagnosing", "escalated", "ticket.escalated"),
        ):
            broken_events.append(
                {
                    key: value
                    for key, value in self._event(
                        broken_ticket,
                        broken_command,
                        broken_decision.lease_version,
                        step_index=step,
                        from_status=from_status,
                        to_status=to_status,
                        event_type=event_type,
                    ).items()
                    if key not in {"ticket_id", "command_id", "lease_version"}
                }
            )
        with self.assertRaisesRegex(ValueError, "事件链"):
            self.repo.commit_command_result(
                broken_ticket["ticket_id"],
                broken_command,
                broken_decision.lease_version,
                {"status": "escalated"},
                broken_events,
            )
        self.assertEqual(
            self.repo.get_ticket(broken_ticket["ticket_id"])["status"], "new"
        )

        illegal_ticket, illegal_command, illegal_decision = self._started(
            "illegal-transition"
        )
        illegal_event = {
            key: value
            for key, value in self._event(
                illegal_ticket,
                illegal_command,
                illegal_decision.lease_version,
                event_type="ticket.resolved",
                from_status="new",
                to_status="resolved",
            ).items()
            if key not in {"ticket_id", "command_id", "lease_version"}
        }
        with self.assertRaisesRegex(ValueError, "非法状态转移"):
            self.repo.commit_command_result(
                illegal_ticket["ticket_id"],
                illegal_command,
                illegal_decision.lease_version,
                {"status": "resolved"},
                [illegal_event],
            )

    def test_secret_like_values_are_rejected_before_reaching_database(self):
        secrets = [
            MNEMONIC,
            "A=" + "a" * 64,
            "WIF=" + "5" + "H" * 50,
            "PIN: 123456",
            "passphrase: correct horse battery staple",
        ]
        for index, secret in enumerate(secrets):
            with self.subTest(index=index), self.assertRaisesRegex(
                ValueError, "敏感信息"
            ) as caught:
                self.repo.create_ticket(f"secret-{index}", "1001", secret, [])
            self.assertNotIn(secret, str(caught.exception))

        ticket, command_id, decision = self._started("safe")
        with self.assertRaises(ValueError):
            self.repo.append_event(
                **self._event(ticket, command_id, decision.lease_version, summary=MNEMONIC)
            )
        with self.assertRaises(ValueError):
            self.repo.append_event(
                **self._event(
                    ticket,
                    command_id,
                    decision.lease_version,
                    metadata={"raw": MNEMONIC},
                )
            )
        database_bytes = self.db_path.read_bytes()
        for secret in secrets:
            self.assertNotIn(secret.encode(), database_bytes)

    def test_redacted_secret_marker_is_safe_to_persist(self):
        sanitized = SanitizedText("[REDACTED_SECRET]")
        ticket = self.repo.create_ticket(
            "redacted", "1001", sanitized.value, ["secret_exposure"]
        )
        self.assertEqual(ticket["sanitized_input"], "[REDACTED_SECRET]")

    def test_secret_detection_is_precise_for_bip39_and_normal_english(self):
        normal_messages = [
            "this support message has twelve ordinary words but is not a wallet recovery phrase today",
            "one two three four five six seven eight nine ten eleven twelve thirteen fourteen",
        ]
        for index, message in enumerate(normal_messages):
            with self.subTest(index=index):
                self.assertFalse(contains_unredacted_secret(message))
                self.repo.create_ticket(f"normal-{index}", "1001", message, [])

        bip39_with_common_separators = MNEMONIC.upper().replace(" ", ", ")
        self.assertTrue(contains_unredacted_secret(bip39_with_common_separators))
        with self.assertRaisesRegex(ValueError, "敏感信息"):
            self.repo.create_ticket(
                "bip39-separated", "1001", bip39_with_common_separators, []
            )

    def test_bip39_detection_supports_all_standard_word_counts(self):
        words = MNEMONIC.split()
        for word_count in (12, 15, 18, 21, 24):
            phrase = " ".join(words[index % len(words)] for index in range(word_count))
            with self.subTest(word_count=word_count):
                self.assertTrue(contains_unredacted_secret(phrase))
                with self.assertRaisesRegex(ValueError, "敏感信息"):
                    self.repo.create_ticket(
                        f"bip39-{word_count}", "1001", phrase, []
                    )

    def test_explicit_transaction_hash_field_can_be_persisted(self):
        ticket, command_id, decision = self._started("transaction-hash")
        event_id = self.repo.append_event(
            **self._event(
                ticket,
                command_id,
                decision.lease_version,
                metadata={"transaction_hash": TransactionHash(TRANSACTION_HASH)},
            )
        )
        event = next(
            item
            for item in self.repo.list_events(ticket["ticket_id"])
            if item["event_id"] == event_id
        )
        self.assertEqual(
            json.loads(event["metadata_json"])["transaction_hash"], TRANSACTION_HASH
        )

    def test_transaction_hash_is_rejected_outside_typed_allowed_field(self):
        ticket, command_id, decision = self._started("transaction-boundary")
        invalid_metadata = [
            {"transaction_hash": TRANSACTION_HASH},
            {"nested": {"transaction_hash": TransactionHash(TRANSACTION_HASH)}},
            {"nested": {"value": TRANSACTION_HASH}},
            {"transaction_hash": {"value": TransactionHash(TRANSACTION_HASH)}},
        ]
        for metadata in invalid_metadata:
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.repo.append_event(
                    **self._event(
                        ticket,
                        command_id,
                        decision.lease_version,
                        metadata=metadata,
                    )
                )

        with self.assertRaises(ValueError):
            self.repo.append_event(
                **self._event(
                    ticket,
                    command_id,
                    decision.lease_version,
                    summary=TRANSACTION_HASH,
                )
            )
        terminal_event = {
            key: value
            for key, value in self._event(
                ticket, command_id, decision.lease_version
            ).items()
            if key not in {"ticket_id", "command_id", "lease_version"}
        }
        with self.assertRaises(ValueError):
            self.repo.commit_command_result(
                ticket["ticket_id"],
                command_id,
                decision.lease_version,
                {"status": "triaged", "draft_answer": TRANSACTION_HASH},
                [terminal_event],
            )

    def test_append_event_is_idempotent_and_strictly_monotonic(self):
        ticket, command_id, decision = self._started()
        values = self._event(ticket, command_id, decision.lease_version)
        first = self.repo.append_event(**values)
        second = self.repo.append_event(**values)
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            self.repo.append_event(**{**values, "summary": "冲突的摘要"})
        with self.assertRaises(ValueError):
            self.repo.append_event(**self._event(ticket, command_id, decision.lease_version, 0))
        with self.assertRaises(ValueError):
            self.repo.append_event(**self._event(ticket, command_id, decision.lease_version, True))
        third = self.repo.append_event(
            **self._event(ticket, command_id, decision.lease_version, 3)
        )
        self.assertGreater(third, first)
        with self.assertRaises(ValueError):
            self.repo.append_event(
                **self._event(ticket, command_id, decision.lease_version, 2)
            )

    def test_concurrent_duplicate_append_writes_one_row(self):
        ticket, command_id, decision = self._started()
        values = self._event(ticket, command_id, decision.lease_version)
        workers = 6
        barrier = threading.Barrier(workers)
        event_ids = []
        errors = []
        lock = threading.Lock()

        def append():
            try:
                barrier.wait()
                event_id = self.repo.append_event(**values)
                with lock:
                    event_ids.append(event_id)
            except Exception as error:  # pragma: no cover - asserted below
                with lock:
                    errors.append(error)

        threads = [threading.Thread(target=append) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(set(event_ids)), 1)
        self.assertEqual(len(self.repo.list_events(ticket["ticket_id"])), 2)

    def test_admin_update_ticket_has_a_narrow_management_only_boundary(self):
        ticket = self._ticket()
        invalid_updates = [
            {},
            {"request_id": "replacement"},
            {"status": "unknown"},
            {"status": []},
            {"priority": "P9"},
            {"risk_level": "severe"},
            {"review_decision": "passed"},
            {"revision_count": -1},
            {"response_version": True},
            {"requires_human": 1},
            {"risk_flags_json": '["secret_exposure"]'},
            {"missing_fields_json": [1]},
            {"evidence_refs_json": [""]},
            {"summary": 123},
        ]
        for updates in invalid_updates:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.repo.admin_update_ticket(ticket["ticket_id"], updates)
        with self.assertRaises(ValueError):
            self.repo.admin_update_ticket(
                "KG-MISSING", {"manual_gate_reason": "等待人工复核"}
            )

        self.repo.admin_update_ticket(
            ticket["ticket_id"],
            {
                "requires_human": True,
                "manual_gate_reason": "等待人工复核",
            },
        )
        updated = self.repo.get_ticket(ticket["ticket_id"])
        self.assertEqual(updated["requires_human"], 1)
        self.assertEqual(updated["manual_gate_reason"], "等待人工复核")

    def test_legacy_unversioned_schema_fails_closed(self):
        legacy_path = Path(self.tmp.name) / "legacy.db"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                "CREATE TABLE tickets (ticket_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
            )
        with self.assertRaisesRegex(RuntimeError, "schema.*重建"):
            TicketRepository(str(legacy_path))

    def test_commit_command_result_atomically_updates_ticket_events_and_command(self):
        ticket, command_id, decision = self._started()
        events = [
            {
                key: value
                for key, value in self._event(
                    ticket,
                    command_id,
                    decision.lease_version,
                    step,
                    **(
                        {"from_status": "triaged", "to_status": "triaged"}
                        if step == 2
                        else {}
                    ),
                ).items()
                if key not in {"ticket_id", "command_id", "lease_version"}
            }
            for step in (1, 2)
        ]
        completed = self.repo.commit_command_result(
            ticket["ticket_id"],
            command_id,
            decision.lease_version,
            {
                "status": "triaged",
                "priority": "P2",
                "risk_flags_json": ["secret_exposure"],
            },
            events,
        )
        self.assertEqual(completed.disposition, CommandDisposition.COMPLETED)
        self.assertEqual(completed.result_status, "triaged")
        self.assertEqual(completed.lease_version, decision.lease_version)
        self.assertEqual(len(self.repo.list_events(ticket["ticket_id"])), 3)
        command = self._command_row(command_id)
        self.assertEqual(command["status"], "completed")
        self.assertEqual(command["result_event_id"], completed.result_event_id)

    def test_commit_command_result_rolls_back_all_writes_on_event_conflict(self):
        ticket, command_id, decision = self._started()
        existing_values = self._event(ticket, command_id, decision.lease_version)
        self.repo.append_event(**existing_values)
        events_before = self.repo.list_events(ticket["ticket_id"])
        command_before = self._command_row(command_id)

        conflicting_event = {
            key: value
            for key, value in {**existing_values, "summary": "冲突的摘要"}.items()
            if key not in {"ticket_id", "command_id", "lease_version"}
        }
        with self.assertRaises(ValueError):
            self.repo.commit_command_result(
                ticket["ticket_id"],
                command_id,
                decision.lease_version,
                {"status": "triaged", "priority": "P2"},
                [conflicting_event],
            )

        self.assertEqual(self.repo.get_ticket(ticket["ticket_id"])["status"], "new")
        self.assertEqual(self.repo.list_events(ticket["ticket_id"]), events_before)
        self.assertEqual(self._command_row(command_id), command_before)

    def test_workbench_projection_revalidates_and_limits_ticket_fields(self):
        ticket = self._ticket("workbench-projection")
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE tickets
                SET status = 'escalated', category = 'other', priority = 'P1',
                    risk_level = 'high', summary = ?, draft_answer = ?
                WHERE ticket_id = ?
                """,
                ("设备连接问题", "请使用官方应用重新配对。", ticket["ticket_id"]),
            )

        projected = self.repo.list_workbench_tickets()
        self.assertEqual(len(projected), 1)
        self.assertEqual(
            set(projected[0]),
            {
                "ticket_id",
                "user_id",
                "conversation_id",
                "parent_ticket_id",
                "status",
                "sanitized_input",
                "summary",
                "category",
                "priority",
                "risk_level",
                "draft_answer",
                "manual_gate_reason",
            },
        )
        self.assertEqual(projected[0]["draft_answer"], "请使用官方应用重新配对。")

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE tickets SET draft_answer = '' WHERE ticket_id = ?",
                (ticket["ticket_id"],),
            )
        self.assertIsNone(self.repo.list_workbench_tickets()[0]["draft_answer"])

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE tickets SET draft_answer = ? WHERE ticket_id = ?",
                (MNEMONIC, ticket["ticket_id"]),
            )
        with self.assertRaisesRegex(ValueError, "未脱敏"):
            self.repo.list_workbench_tickets()

    def test_follow_up_ticket_inherits_conversation_and_parent(self):
        first = self.repo.create_ticket(
            "conversation-root", "1001", "蓝牙连接失败", []
        )
        follow_up = self.repo.create_ticket(
            "conversation-follow-up",
            "1001",
            "换手机后仍然无法配对",
            [],
            parent_ticket_id=first["ticket_id"],
        )
        self.assertEqual(follow_up["parent_ticket_id"], first["ticket_id"])
        self.assertEqual(follow_up["conversation_id"], first["conversation_id"])
        projected = self.repo.list_workbench_tickets()
        self.assertEqual(
            {
                item["parent_ticket_id"]
                for item in projected
                if item["ticket_id"] == follow_up["ticket_id"]
            },
            {first["ticket_id"]},
        )


if __name__ == "__main__":
    unittest.main()
