"""Safety and immutability tests for the Phase 1 migration runner."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit, urlunsplit

from db import run_migrations as migration_runner
from db.run_migrations import MIGRATIONS_DIR, apply_migrations, is_local_target, main

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


# ---------------------------------------------------------------------------
# Option A: the immutable committed 0036 (variant A) plus the forward 0037
# ---------------------------------------------------------------------------
VARIANT_A_0036 = "9dd2c780deaecfad"
VARIANT_B_0036 = "aa89f2d6be3a64a6"
MIGRATION_0036 = "0036_whatsapp_quotations.sql"
MIGRATION_0037 = "0037_quotation_briefs_and_deliveries.sql"
SENTINEL_TABLE = "public.pending_sentinel_should_not_run"


@contextlib.contextmanager
def _disposable_database(suffix: str):
    """Create and drop a database on the same server as the test database.

    The checksum guard can only be exercised against a database whose ledger the test
    controls, and the ledger of the shared test database must stay untouched. The name
    always contains 'test', and the server is the one the already-validated
    SAAS_TEST_DATABASE_URL points at.
    """
    import psycopg

    parts = urlsplit(require_test_database_url())
    name = f"saas_migration_runner_{suffix}_test"
    maintenance = urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))
    target = urlunsplit((parts.scheme, parts.netloc, f"/{name}", "", ""))

    def administer(statement: str) -> None:
        with psycopg.connect(maintenance, autocommit=True) as conn:
            conn.execute(statement)

    administer(f'drop database if exists "{name}" with (force)')
    administer(f'create database "{name}"')
    try:
        yield target
    finally:
        administer(f'drop database if exists "{name}" with (force)')


def _repository_migrations_with_sentinel(directory: Path) -> Path:
    """Copy the repository migrations and add a file that must never be applied."""
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        shutil.copy(path, directory / path.name)
    (directory / "0099_sentinel_should_not_run.sql").write_text(
        f"create table {SENTINEL_TABLE} (id integer);\n", encoding="utf-8"
    )
    return directory


class VariantAForwardMigrationTests(SaasDatabaseTestCase):
    """A database that recorded variant A must then apply 0037 cleanly."""

    def test_a_variant_a_database_applies_0037_and_records_its_checksum(self):
        import psycopg

        expected_0037 = migration_runner._checksum(
            (MIGRATIONS_DIR / MIGRATION_0037).read_text(encoding="utf-8")
        )
        with _disposable_database("variant_a") as url, psycopg.connect(url) as conn:
            ran = apply_migrations(conn, MIGRATIONS_DIR)
            recorded = migration_runner._applied(conn)

        self.assertIn(MIGRATION_0037, ran)
        self.assertEqual(recorded[MIGRATION_0036], VARIANT_A_0036)
        self.assertEqual(recorded[MIGRATION_0037], expected_0037)
        self.assertEqual(len(recorded), len(list(MIGRATIONS_DIR.glob("*.sql"))))

    def test_the_variant_a_plus_0037_database_reports_no_mismatch(self):
        import psycopg

        with _disposable_database("variant_a_status") as url, psycopg.connect(url) as conn:
            apply_migrations(conn, MIGRATIONS_DIR)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                migration_runner.print_status(conn)

        printed = output.getvalue()
        self.assertIn(MIGRATION_0037, printed)
        self.assertNotIn("not applied yet", printed)
        self.assertNotIn("CHECKSUM MISMATCHES", printed)


class VariantBLedgerRejectionTests(SaasDatabaseTestCase):
    """A database that recorded the other 0036 must be refused, never repaired here."""

    def test_variant_b_ledger_is_rejected_before_any_pending_migration_runs(self):
        import psycopg

        with _disposable_database("variant_b") as url, tempfile.TemporaryDirectory() as tmp:
            with psycopg.connect(url) as conn:
                apply_migrations(conn, MIGRATIONS_DIR)
                with conn.cursor() as cursor:
                    cursor.execute(
                        "delete from public.saas_schema_migrations where version = %s",
                        (MIGRATION_0037,),
                    )
                    cursor.execute(
                        "update public.saas_schema_migrations set checksum = %s "
                        "where version = %s",
                        (VARIANT_B_0036, MIGRATION_0036),
                    )
                conn.commit()

            directory = _repository_migrations_with_sentinel(Path(tmp))
            with psycopg.connect(url) as conn:
                with self.assertRaisesRegex(SystemExit, "modified after it was applied"):
                    apply_migrations(conn, directory)

            with psycopg.connect(url) as conn, conn.cursor() as cursor:
                cursor.execute(
                    "select checksum from public.saas_schema_migrations where version = %s",
                    (MIGRATION_0036,),
                )
                self.assertEqual(
                    cursor.fetchone()[0],
                    VARIANT_B_0036,
                    "the refused run must not rewrite the recorded checksum",
                )
                cursor.execute(
                    "select count(*) from public.saas_schema_migrations where version = %s",
                    (MIGRATION_0037,),
                )
                self.assertEqual(cursor.fetchone()[0], 0, "a pending migration ran")
                cursor.execute("select to_regclass(%s)", (SENTINEL_TABLE,))
                self.assertIsNone(cursor.fetchone()[0], "a pending migration ran")


class ZeroThirtySevenGuardTests(SaasDatabaseTestCase):
    """0037 must not silently accept a pre-existing table."""

    def test_0037_refuses_a_preexisting_quotation_table(self):
        import psycopg

        with _disposable_database("guard") as url, tempfile.TemporaryDirectory() as tmp:
            with psycopg.connect(url) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("create table public.quotation_briefs (id uuid)")
                conn.commit()

                shutil.copy(MIGRATIONS_DIR / MIGRATION_0037, Path(tmp) / MIGRATION_0037)
                with self.assertRaises(Exception) as raised:
                    apply_migrations(conn, Path(tmp))
                self.assertIn("migration 0037 refuses to run", str(raised.exception))
                conn.rollback()

            with psycopg.connect(url) as conn, conn.cursor() as cursor:
                cursor.execute(
                    "select count(*) from public.saas_schema_migrations where version = %s",
                    (MIGRATION_0037,),
                )
                self.assertEqual(cursor.fetchone()[0], 0, "a refused migration was recorded")


if __name__ == "__main__":
    unittest.main()
