"""Safety and immutability tests for the Phase 1 migration runner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from db.run_migrations import apply_migrations, main

try:
    from .saas_test_support import SaasDatabaseTestCase, test_database_url
except ImportError:
    from saas_test_support import SaasDatabaseTestCase, test_database_url  # type: ignore[no-redef]


class ResetSafetyTests(unittest.TestCase):
    def test_reset_requires_explicit_confirmation(self):
        with self.assertRaises(SystemExit) as caught:
            main(["--database-url", "postgresql://localhost/saas_platform", "--reset"])
        self.assertEqual(caught.exception.code, 2)

    def test_reset_refuses_supabase_even_with_confirmation(self):
        with self.assertRaises(SystemExit) as caught:
            main(
                [
                    "--database-url",
                    "postgresql://postgres:secret@db.example.supabase.co/platform",
                    "--reset",
                    "--i-am-sure",
                ]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_reset_refuses_remote_database_even_with_confirmation(self):
        with self.assertRaises(SystemExit) as caught:
            main(
                [
                    "--database-url",
                    "postgresql://postgres:secret@production.example.com/platform",
                    "--reset",
                    "--i-am-sure",
                ]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_reset_refuses_host_override_in_loopback_url(self):
        with self.assertRaises(SystemExit) as caught:
            main(
                [
                    "--database-url",
                    "postgresql://postgres@localhost/platform?host=production.example.com",
                    "--reset",
                    "--i-am-sure",
                ]
            )
        self.assertEqual(caught.exception.code, 2)


class MigrationChecksumTests(SaasDatabaseTestCase):
    def test_modified_applied_migration_is_rejected(self):
        import psycopg

        with tempfile.TemporaryDirectory() as directory:
            migration = Path(directory) / "0001_extensions_and_helpers.sql"
            migration.write_text("select 1; -- changed", encoding="utf-8")
            conn = psycopg.connect(test_database_url())
            try:
                with self.assertRaisesRegex(SystemExit, "modified after it was applied"):
                    apply_migrations(conn, Path(directory))
            finally:
                conn.rollback()
                conn.close()


if __name__ == "__main__":
    unittest.main()
