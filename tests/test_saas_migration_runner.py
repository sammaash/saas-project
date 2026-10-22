"""Safety and immutability tests for the Phase 1 migration runner."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from db.run_migrations import apply_migrations, is_local_target, main

try:
    from .saas_test_support import (
        SaasDatabaseTestCase,
        require_test_database_url,
        test_database_url,
    )
except ImportError:
    from saas_test_support import (  # type: ignore[no-redef]
        SaasDatabaseTestCase,
        require_test_database_url,
        test_database_url,
    )


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


class DevelopmentDataGuardTests(unittest.TestCase):
    """--include-local and --seed must never reach a non-local database by accident."""

    REMOTE = "postgresql://postgres:secret@production.example.com/platform"
    SUPABASE = "postgresql://postgres:secret@db.abcdef.supabase.co/postgres"

    def _refused(self, url, *flags):
        with self.assertRaises(SystemExit) as caught:
            main(["--database-url", url, *flags])
        self.assertEqual(caught.exception.code, 2)

    def test_local_and_seed_are_refused_for_remote_hosts(self):
        for flag in ("--include-local", "--seed"):
            with self.subTest(flag=flag):
                self._refused(self.REMOTE, flag)

    def test_supabase_is_refused_even_with_the_override(self):
        for flag in ("--include-local", "--seed"):
            with self.subTest(flag=flag):
                self._refused(self.SUPABASE, flag, "--allow-nonlocal-dev")

    def test_host_override_inside_a_loopback_url_is_refused(self):
        self._refused("postgresql://postgres@localhost/platform?host=production.example.com", "--include-local")
        self._refused("postgresql://postgres@localhost/platform?hostaddr=10.1.2.3", "--seed")

    def test_pghost_environment_cannot_redirect_a_hostless_url(self):
        with mock.patch.dict(os.environ, {"PGHOST": "production.example.com"}):
            self._refused("postgresql:///platform", "--include-local")

    def test_override_lets_a_non_local_host_through_the_guard(self):
        class Reached(Exception):
            pass

        with mock.patch("db.run_migrations._psycopg", side_effect=Reached):
            with self.assertRaises(Reached):
                main(["--database-url", self.REMOTE, "--include-local", "--allow-nonlocal-dev"])

    def test_loopback_hosts_pass_the_guard_without_an_override(self):
        class Reached(Exception):
            pass

        for url in ("postgresql://postgres@localhost/platform", "postgresql://postgres@127.0.0.1:5433/platform",
                    "postgresql://postgres@[::1]/platform"):
            with self.subTest(url=url), mock.patch("db.run_migrations._psycopg", side_effect=Reached):
                with self.assertRaises(Reached):
                    main(["--database-url", url, "--include-local", "--seed"])

    def test_plain_migrations_are_not_blocked_on_remote_hosts(self):
        class Reached(Exception):
            pass

        with mock.patch("db.run_migrations._psycopg", side_effect=Reached):
            with self.assertRaises(Reached):
                main(["--database-url", self.REMOTE])

    def test_is_local_target_classification(self):
        local = ("postgresql://u@localhost/db", "postgresql://u@127.0.0.1/db", "postgresql://u@[::1]/db",
                 "postgresql:///db", "postgresql:///db?host=/var/run/postgresql")
        remote = ("postgresql://u@prod.example.com/db", "postgresql://u@localhost/db?host=prod.example.com",
                  "postgresql://u@localhost,prod.example.com/db", "host=prod.example.com dbname=x",
                  "postgresql://u@10.0.0.5/db")
        for url in local:
            with self.subTest(url=url), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PGHOST", None)
                os.environ.pop("PGHOSTADDR", None)
                self.assertTrue(is_local_target(url))
        for url in remote:
            with self.subTest(url=url):
                self.assertFalse(is_local_target(url))


class MigrationChecksumTests(SaasDatabaseTestCase):
    def test_modified_applied_migration_is_rejected(self):
        import psycopg

        with tempfile.TemporaryDirectory() as directory:
            migration = Path(directory) / "0001_extensions_and_helpers.sql"
            migration.write_text("select 1; -- changed", encoding="utf-8")
            # Fail closed immediately before the connection: no ambient DSN can be used.
            url = require_test_database_url()
            conn = psycopg.connect(url)
            try:
                with self.assertRaisesRegex(SystemExit, "modified after it was applied"):
                    apply_migrations(conn, Path(directory))
            finally:
                conn.rollback()
                conn.close()


if __name__ == "__main__":
    unittest.main()
