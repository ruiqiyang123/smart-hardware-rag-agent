import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database.profile_db as profile_db_module
from database.profile_db import ProfileDatabase, UserProfile


MNEMONIC = (
    "abandon ability able about above absent absorb abstract absurd abuse access accident"
)
PRIVATE_KEY = "private key: " + "a" * 64


class ProfileDatabaseSecurityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "profiles.db"
        self.database = ProfileDatabase(str(self.db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def _row_count(self) -> int:
        with sqlite3.connect(self.db_path) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM user_profiles"
            ).fetchone()[0]

    def test_rejects_secrets_oversize_control_and_wrong_types_without_persistence(self):
        unsafe_values = (
            ("mnemonic", UserProfile("u-mnemonic", region=MNEMONIC), MNEMONIC),
            ("private-key", UserProfile("u-key", device_model=PRIVATE_KEY), PRIVATE_KEY),
            ("oversize", UserProfile("u-long", region="Z" * 101), "Z" * 101),
            (
                "control",
                UserProfile("u-control", preferred_chains="CONTROL_MARKER\x00BTC"),
                "CONTROL_MARKER",
            ),
            ("wrong-type", UserProfile("u-type", region=123), "123"),
            ("wrong-bool", UserProfile("u-bool", backup_verified=1), "u-bool"),
            ("secret-user-id", UserProfile("a" * 64), "a" * 64),
        )

        with patch.object(profile_db_module.logger, "error") as error_log:
            for name, profile, _marker in unsafe_values:
                with self.subTest(name=name):
                    self.assertFalse(self.database.save_profile(profile))

        self.assertEqual(self._row_count(), 0)
        stored = self.db_path.read_bytes()
        for _name, _profile, marker in unsafe_values:
            self.assertNotIn(marker.encode(), stored)
        rendered_logs = repr(error_log.call_args_list)
        for _name, _profile, marker in unsafe_values:
            self.assertNotIn(marker, rendered_logs)
        self.assertNotIn(MNEMONIC, rendered_logs)
        self.assertNotIn(PRIVATE_KEY, rendered_logs)

    def test_polluted_legacy_row_fails_closed_and_logs_only_error_type(self):
        polluted = "mnemonic: " + MNEMONIC
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO user_profiles (user_id, region)
                VALUES (?, ?)
                """,
                ("legacy-user", polluted),
            )

        with patch.object(profile_db_module.logger, "error") as error_log:
            self.assertIsNone(self.database.get_profile("legacy-user"))

        rendered_logs = repr(error_log.call_args_list)
        self.assertNotIn(polluted, rendered_logs)
        self.assertIn("error_type=%s", rendered_logs)
        self.assertIn("ValueError", rendered_logs)

    def test_valid_profile_is_trimmed_and_round_trips_as_typed_projection(self):
        profile = UserProfile(
            user_id=" 1001 ",
            experience_level=" 进阶 ",
            region=" 深圳 ",
            device_model=" KeyGuard Pro ",
            preferred_chains=" BTC, ETH ",
            connection_method=" USB-C ",
            passphrase_enabled=True,
            backup_verified=False,
        )

        self.assertTrue(self.database.save_profile(profile))
        stored = self.database.get_profile(" 1001 ")

        self.assertEqual(
            stored,
            UserProfile(
                user_id="1001",
                experience_level="进阶",
                region="深圳",
                device_model="KeyGuard Pro",
                preferred_chains="BTC, ETH",
                connection_method="USB-C",
                passphrase_enabled=True,
                backup_verified=False,
            ),
        )

    def test_profile_formatter_does_not_log_profile_content(self):
        source = (
            Path(__file__).resolve().parents[1] / "agent/tools/profile_tools.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("logger", source)
        self.assertNotIn("获取到档案", source)


if __name__ == "__main__":
    unittest.main()
