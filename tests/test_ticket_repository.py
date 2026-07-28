import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database.ticket_db import CommandDisposition, TicketRepository


class TicketRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TicketRepository(str(Path(self.tmp.name) / "tickets.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def _ticket(self, request_id="req-1"):
        return self.repo.create_ticket(
            request_id, "1001", "蓝牙连接失败", ["secret_exposure"]
        )

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
            ), self.assertRaises(ValueError):
                self.repo.create_ticket(
                    "req-1", user_id, sanitized_input, risk_flags
                )

    def test_command_completed_returns_saved_result(self):
        ticket = self._ticket("req-2")
        started = self.repo.begin_command(
            ticket["ticket_id"], "request:req-2", "user_input", lease_seconds=130
        )
        self.assertEqual(started.disposition, CommandDisposition.START)
        self.repo.complete_command(
            "request:req-2", result_status="triaged", result_event_id=None
        )
        duplicate = self.repo.begin_command(
            ticket["ticket_id"], "request:req-2", "user_input", lease_seconds=130
        )
        self.assertEqual(duplicate.disposition, CommandDisposition.COMPLETED)
        self.assertEqual(duplicate.result_status, "triaged")

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

    def test_existing_command_rejects_ticket_or_type_conflict(self):
        first = self._ticket("req-1")
        second = self._ticket("req-2")
        self.repo.begin_command(
            first["ticket_id"], "request:shared", "user_input", 130
        )
        conflicts = [
            (second["ticket_id"], "user_input"),
            (first["ticket_id"], "human_action"),
        ]
        for ticket_id, command_type in conflicts:
            with self.subTest(
                ticket_id=ticket_id, command_type=command_type
            ), self.assertRaises(ValueError):
                self.repo.begin_command(
                    ticket_id, "request:shared", command_type, 130
                )

    def test_expired_command_is_atomically_renewed_once(self):
        ticket = self._ticket()
        command_id = "request:req-1"
        self.repo.begin_command(ticket["ticket_id"], command_id, "user_input", 130)
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with sqlite3.connect(self.repo.db_path) as connection:
            connection.execute(
                "UPDATE ticket_commands SET lease_expires_at = ? WHERE command_id = ?",
                (expired, command_id),
            )

        workers = 4
        barrier = threading.Barrier(workers)
        dispositions = []
        errors = []
        result_lock = threading.Lock()

        def begin():
            try:
                barrier.wait()
                decision = self.repo.begin_command(
                    ticket["ticket_id"], command_id, "user_input", 130
                )
                with result_lock:
                    dispositions.append(decision.disposition)
            except Exception as error:  # pragma: no cover - failure is asserted below
                with result_lock:
                    errors.append(error)

        threads = [threading.Thread(target=begin) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(dispositions.count(CommandDisposition.RESUME), 1)
        self.assertEqual(dispositions.count(CommandDisposition.IN_PROGRESS), workers - 1)

    def test_command_and_accepted_event_are_created_in_one_transaction(self):
        with self.assertRaises(ValueError):
            self.repo.begin_command(
                "KG-DOES-NOT-EXIST", "request:missing", "user_input", 130
            )
        with sqlite3.connect(self.repo.db_path) as connection:
            command_count = connection.execute(
                "SELECT COUNT(*) FROM ticket_commands WHERE command_id = ?",
                ("request:missing",),
            ).fetchone()[0]
            event_count = connection.execute(
                "SELECT COUNT(*) FROM ticket_events WHERE command_id = ?",
                ("request:missing",),
            ).fetchone()[0]
        self.assertEqual((command_count, event_count), (0, 0))

    def test_complete_and_fail_reject_unknown_or_illegal_terminal_overwrite(self):
        ticket = self._ticket()
        with self.assertRaises(ValueError):
            self.repo.complete_command("missing", "triaged", None)
        with self.assertRaises(ValueError):
            self.repo.fail_command("missing", "UNKNOWN")

        completed_id = "request:completed"
        self.repo.begin_command(ticket["ticket_id"], completed_id, "user_input", 130)
        self.repo.complete_command(completed_id, "triaged", 7)
        self.repo.complete_command(completed_id, "triaged", 7)
        with self.assertRaises(ValueError):
            self.repo.complete_command(completed_id, "resolved", 8)
        with self.assertRaises(ValueError):
            self.repo.fail_command(completed_id, "LATE_FAILURE")

        failed_id = "request:failed"
        self.repo.begin_command(ticket["ticket_id"], failed_id, "user_input", 130)
        self.repo.fail_command(failed_id, "MODEL_TIMEOUT")
        self.repo.fail_command(failed_id, "MODEL_TIMEOUT")
        with self.assertRaises(ValueError):
            self.repo.fail_command(failed_id, "DIFFERENT_ERROR")
        with self.assertRaises(ValueError):
            self.repo.complete_command(failed_id, "triaged", None)

    def test_event_payload_does_not_contain_raw_secret(self):
        ticket = self.repo.create_ticket(
            "req-3", "1001", "[REDACTED_SECRET]", ["secret_exposure"]
        )
        self.repo.append_event(
            ticket_id=ticket["ticket_id"],
            command_id="request:req-3",
            step_index=1,
            node_name="ingress",
            event_type="ingress_guard.redacted",
            from_status="new",
            to_status="new",
            summary="检测并脱敏钱包秘密",
            metadata={"flags": ["secret_exposure"]},
        )
        serialized = str(self.repo.list_events(ticket["ticket_id"]))
        self.assertNotIn("abandon ability", serialized)

    def test_append_event_is_idempotent_but_rejects_conflicting_reuse(self):
        ticket = self._ticket()
        values = {
            "ticket_id": ticket["ticket_id"],
            "command_id": "request:req-1",
            "step_index": 1,
            "node_name": "triage",
            "event_type": "ticket.triaged",
            "from_status": "new",
            "to_status": "triaged",
            "summary": "完成分类",
            "metadata": {"priority": "P2"},
        }
        first = self.repo.append_event(**values)
        second = self.repo.append_event(**values)
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            self.repo.append_event(**{**values, "summary": "冲突的摘要"})

    def test_update_ticket_uses_allowlist_and_rejects_empty_updates(self):
        ticket = self._ticket()
        for updates in ({}, {"request_id": "replacement"}, {"unknown": "value"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.repo.update_ticket(ticket["ticket_id"], updates)

        self.repo.update_ticket(
            ticket["ticket_id"],
            {
                "status": "triaged",
                "risk_flags_json": json.dumps(["secret_exposure"]),
            },
        )
        updated = self.repo.get_ticket(ticket["ticket_id"])
        self.assertEqual(updated["status"], "triaged")


if __name__ == "__main__":
    unittest.main()
