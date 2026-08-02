import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.orchestration.session_lifecycle import SessionExpiryService
from database.ticket_db import TicketRepository


class SessionExpiryServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "tickets.db"
        self.repo = TicketRepository(str(self.db_path))
        self.service = SessionExpiryService(self.repo)
        self.ticket = self.repo.create_ticket(
            "session-expiry", "1001", "蓝牙连接失败", []
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE tickets SET status = 'pending_user' WHERE ticket_id = ?",
                (self.ticket["ticket_id"],),
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_arm_sets_exact_thirty_minute_deadline(self):
        now = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)
        deadline = self.service.arm(self.ticket["ticket_id"], now=now)
        self.assertEqual(deadline, (now + timedelta(minutes=30)).isoformat())
        self.assertEqual(
            self.repo.get_ticket(self.ticket["ticket_id"])["idle_expires_at"],
            deadline,
        )

    def test_due_ticket_becomes_closed_not_resolved(self):
        armed_at = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)
        self.service.arm(self.ticket["ticket_id"], now=armed_at)
        closed = self.service.close_expired(
            now=armed_at + timedelta(minutes=30, seconds=1)
        )
        self.assertEqual(closed, [self.ticket["ticket_id"]])
        ticket = self.repo.get_ticket(self.ticket["ticket_id"])
        self.assertEqual(ticket["status"], "closed")
        self.assertEqual(ticket["close_reason"], "inactivity")
        self.assertIsNotNone(ticket["closed_at"])
        self.assertIsNone(ticket["idle_expires_at"])
        self.assertNotEqual(ticket["status"], "resolved")
        self.assertEqual(
            self.repo.list_events(self.ticket["ticket_id"])[-1]["event_type"],
            "ticket.closed",
        )

    def test_ticket_is_not_closed_before_deadline(self):
        armed_at = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)
        self.service.arm(self.ticket["ticket_id"], now=armed_at)
        self.assertEqual(
            self.service.close_expired(now=armed_at + timedelta(minutes=29)), []
        )
        self.assertEqual(
            self.repo.get_ticket(self.ticket["ticket_id"])["status"],
            "pending_user",
        )

    def test_user_activity_cancels_expiry(self):
        armed_at = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)
        self.service.arm(self.ticket["ticket_id"], now=armed_at)
        self.assertTrue(self.service.clear(self.ticket["ticket_id"]))
        self.assertEqual(
            self.service.close_expired(now=armed_at + timedelta(hours=1)), []
        )


if __name__ == "__main__":
    unittest.main()
