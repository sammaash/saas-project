"""Safety and immutability tests for the Phase 1 migration runner."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
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


@contextlib.contextmanager
def _fake_psycopg():
    """Provide a fake lazy-imported driver so tests can prove whether connect is reached."""
    driver = mock.MagicMock()
    with mock.patch.dict(sys.modules, {"psycopg": driver}):
        yield driver


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

    def _refused_before_connect(self, url, *flags):
        with _fake_psycopg() as driver:
            self._refused(url, *flags)
        driver.connect.assert_not_called()

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

    def test_pghostaddr_environment_cannot_redirect_a_loopback_url(self):
        with mock.patch.dict(os.environ, {"PGHOSTADDR": "10.1.2.3"}):
            self._refused_before_connect(
                "postgresql://postgres@localhost/saas_platform"
            )

    def test_override_does_not_let_an_unapproved_remote_through_the_guard(self):
        """--allow-nonlocal-dev should NOT bypass the staging-target guard.
        """
        for flags in (
            (),
            ("--allow-nonlocal-dev",),
            ("--include-local", "--allow-nonlocal-dev"),
            ("--seed", "--allow-nonlocal-dev"),
        ):
            with self.subTest(flags=flags):
                self._refused_before_connect(self.REMOTE, *flags)

    def test_arbitrary_loopback_database_names_are_rejected_before_connect(self):
        for host in ("localhost", "127.0.0.1", "[::1]"):
            for database in ("platform", "postgres"):
                with self.subTest(host=host, database=database):
                    self._refused_before_connect(
                        f"postgresql://postgres@{host}/{database}"
                    )

    def test_approved_loopback_database_reaches_connect_without_override(self):
        class Reached(Exception):
            pass

        urls = (
            "postgresql://postgres@localhost/saas_platform",
            "postgresql://postgres@127.0.0.1:5433/saas_platform",
            "postgresql://postgres@[::1]/saas_platform",
        )
        for url in urls:
            with self.subTest(url=url), _fake_psycopg() as driver:
                driver.connect.side_effect = Reached
                with self.assertRaises(Reached):
                    main(["--database-url", url, "--status"])
                driver.connect.assert_called_once_with(url)

    def test_plain_migrations_are_blocked_on_unapproved_remote_hosts(self):
        for flags in ((), ("--include-local",), ("--seed",), ("--status",)):
            with self.subTest(flags=flags):
                self._refused_before_connect(self.REMOTE, *flags)

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


class FreshDatabaseStatusTests(unittest.TestCase):
    """--status is the first command on a new database, before any ledger exists."""

    @staticmethod
    def _connection_without_ledger():
        cursor = mock.MagicMock()
        cursor.fetchone.return_value = (None,)
        connection = mock.MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        return connection

    def test_status_reports_every_migration_as_pending(self):
        connection = self._connection_without_ledger()
        output = io.StringIO()
        with mock.patch.object(
            migration_runner,
            "_applied",
            side_effect=AssertionError("read the ledger before checking it exists"),
        ), contextlib.redirect_stdout(output):
            migration_runner.print_status(connection)
        printed = output.getvalue()
        self.assertIn("no migrations recorded yet", printed)
        self.assertIn("not applied yet", printed)
        self.assertIn("0036_whatsapp_quotations.sql", printed)
        self.assertIn("0037_quotation_briefs_and_deliveries.sql", printed)
        self.assertNotIn("CHECKSUM MISMATCHES", printed)


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


class StagingTargetValidationTests(unittest.TestCase):
    """Tests for the fail-closed staging target validation guard."""

    def test_valid_staging_target_accepts(self):
        """A valid Railway staging target should pass validation."""
        staging_url = "postgresql://app_backend:password@postgres.railway.internal:5432/railway"
        parsed = migration_runner.parse_database_url(staging_url)
        self.assertIsNotNone(parsed)
        self.assertTrue(migration_runner.validate_staging_target(parsed))

    def test_production_hostname_rejected(self):
        """Production hostnames should be rejected."""
        prod_url = "postgresql://app_backend:password@prod-db.example.com:5432/production"
        parsed = migration_runner.parse_database_url(prod_url)
        self.assertIsNone(parsed)

    def test_supabase_target_rejected(self):
        """Supabase URLs should be rejected by parse_database_url."""
        supabase_url = "postgresql://postgres:secret@db.example.supabase.co/postgres"
        parsed = migration_runner.parse_database_url(supabase_url)
        self.assertIsNone(parsed)

    def test_wrong_database_name_rejected(self):
        """Non-Railway database names should be rejected."""
        wrong_url = "postgresql://app_backend:password@postgres.railway.internal:5432/wrongdb"
        parsed = migration_runner.parse_database_url(wrong_url)
        self.assertIsNone(parsed)

    def test_localhost_target_rejected(self):
        """Localhost targets should be rejected for staging migrations."""
        local_url = "postgresql://app_backend:password@localhost:5432/railway"
        parsed = migration_runner.parse_database_url(local_url)
        self.assertIsNone(parsed)

    def test_invalid_scheme_rejected(self):
        """Invalid schemes should be rejected."""
        invalid_url = "mysql://app_backend:password@postgres.railway.internal:5432/railway"
        parsed = migration_runner.parse_database_url(invalid_url)
        self.assertIsNone(parsed)

    def test_missing_port_rejected(self):
        """Missing port should be rejected."""
        no_port_url = "postgresql://app_backend:password@postgres.railway.internal/railway"
        parsed = migration_runner.parse_database_url(no_port_url)
        self.assertIsNone(parsed)

    def test_wrong_port_rejected(self):
        """Wrong port should be rejected."""
        wrong_port_url = "postgresql://app_backend:password@postgres.railway.internal:5433/railway"
        parsed = migration_runner.parse_database_url(wrong_port_url)
        self.assertIsNone(parsed)


class AlternativeGuardDesignTests(unittest.TestCase):
    """Additional tests exploring guard design edge cases."""

    def test_hostname_vs_suffix_checking(self):
        """Ensure guard does NOT rely on hostname suffix alone."""
        url = "postgresql://user:pass@random-service.railway.internal:5432/railway"
        parsed = migration_runner.parse_database_url(url)
        self.assertIsNone(parsed)

    def test_database_name_independence_from_postgres(self):
        """Ensure 'postgres' database name is not accepted as default."""
        postgres_db_url = "postgresql://user:pass@postgres.railway.internal:5432/postgres"
        parsed = migration_runner.parse_database_url(postgres_db_url)
        self.assertIsNone(parsed)

    def test_production_indicators_in_path(self):
        """Production indicators in the path should not be accepted."""
        prod_path_url = "postgresql://user:pass@postgres.railway.internal:5432/production"
        parsed = migration_runner.parse_database_url(prod_path_url)
        self.assertIsNone(parsed)


class StagingTargetGuardTests(unittest.TestCase):
    """Tests for the staging target guard that applies to ALL database operations."""

    REMOTE = "postgresql://postgres:secret@production.example.com/platform"
    SUPABASE = "postgresql://postgres:secret@db.abcdef.supabase.co/postgres"
    VALID_STAGING = "postgresql://app_backend:password@postgres.railway.internal:5432/railway"
    LOCALHOST = "postgresql://postgres@localhost/platform"
    LOOPBACK_127 = "postgresql://postgres@127.0.0.1/platform"
    LOOPBACK_IPV6 = "postgresql://postgres@[::1]/platform"

    def _refused(self, url, *flags):
        with self.assertRaises(SystemExit) as caught:
            main(["--database-url", url, *flags])
        self.assertEqual(caught.exception.code, 2)

    def _accepted(self, url, *flags):
        try:
            main(["--database-url", url, *flags])
            return True
        except SystemExit:
            return False

    def test_status_with_unapproved_remote_is_blocked(self):
        """--status should block unapproved remote URLs (not just ordinary migrations)."""
        # Without dev flags, unapproved remote should be blocked
        self._refused(self.REMOTE, "--status")

        # With --include-local, unapproved remote should still be blocked
        self._refused(self.REMOTE, "--include-local", "--status")

        # With --seed, unapproved remote should still be blocked
        self._refused(self.REMOTE, "--seed", "--status")

    def test_allow_nonlocal_dev_does_not_bypass_staging_target_guard(self):
        """--allow-nonlocal-dev should NOT bypass the staging-target guard."""
        # --allow-nonlocal-dev alone should not let unapproved remote through
        self._refused(self.REMOTE, "--allow-nonlocal-dev")

        # --allow-nonlocal-dev with --include-local should not let unapproved remote through
        self._refused(self.REMOTE, "--include-local", "--allow-nonlocal-dev")

        # --allow-nonlocal-dev with --seed should not let unapproved remote through
        self._refused(self.REMOTE, "--seed", "--allow-nonlocal-dev")

    def test_loopback_hosts_accepted_with_status(self):
        """Loopback hosts with approved disposable database names are accepted with --status.

        Uses mocked connection - no PostgreSQL server or DNS resolution required.
        Loopback hosts are authorized against the explicit disposable-database allowlist
        before the mocked connection is reached.
        """
        from db.run_migrations import LOOPBACK_HOSTS

        # Test that loopback hostnames are accepted (preflight validation passes)
        # without requiring a real database connection
        loopback_urls = [
            "postgresql://postgres@localhost:5432/saas_platform",
            "postgresql://postgres@127.0.0.1:5432/saas_platform",
            "postgresql://postgres@[::1]:5432/saas_platform",
        ]

        class Reached(Exception):
            pass

        for db_url in loopback_urls:
            with self.subTest(url=db_url):
                parts = urlsplit(db_url)
                hostname = parts.hostname
                self.assertIn(hostname, LOOPBACK_HOSTS,
                              f"Hostname {hostname} should be a recognized loopback address")
                with _fake_psycopg() as driver:
                    driver.connect.side_effect = Reached
                    with self.assertRaises(Reached):
                        main(["--database-url", db_url, "--status"])
                driver.connect.assert_called_once_with(db_url)

    def test_valid_staging_target_accepted_with_status(self):
        """Valid staging target should be accepted with --status (mocked connection).

        Uses mocked connection - no DNS resolution or real server required.
        Verifies the staging target URL components (postgres.railway.internal / railway)
        are validated before psycopg.connect() is called.
        """
        from db.run_migrations import EXPECTED_STAGING_HOSTNAME, EXPECTED_STAGING_DATABASE

        # Parse the valid staging URL and verify components
        parsed = migration_runner.parse_database_url(self.VALID_STAGING)
        self.assertIsNotNone(parsed, "Valid staging URL should parse successfully")

        # Hostname should match the expected staging hostname
        self.assertEqual(parsed["hostname"], EXPECTED_STAGING_HOSTNAME,
                         f"Hostname should be {EXPECTED_STAGING_HOSTNAME}")

        # Database name should be the expected staging database
        self.assertEqual(parsed["database"], EXPECTED_STAGING_DATABASE,
                         f"Database name should be {EXPECTED_STAGING_DATABASE}")

    def test_valid_staging_target_reaches_connect_with_fake_driver(self):
        class Reached(Exception):
            pass

        with _fake_psycopg() as driver:
            driver.connect.side_effect = Reached
            with self.assertRaises(Reached):
                main(["--database-url", self.VALID_STAGING, "--status"])
        driver.connect.assert_called_once_with(self.VALID_STAGING)

    def test_pghostaddr_cannot_redirect_the_staging_target(self):
        with mock.patch.dict(os.environ, {"PGHOSTADDR": "10.1.2.3"}):
            with _fake_psycopg() as driver:
                with self.assertRaises(SystemExit) as caught:
                    main(["--database-url", self.VALID_STAGING, "--status"])
            self.assertEqual(caught.exception.code, 2)
        driver.connect.assert_not_called()

    def test_override_allows_dev_data_on_the_valid_staging_target(self):
        class Reached(Exception):
            pass

        with _fake_psycopg() as driver:
            driver.connect.side_effect = Reached
            with self.assertRaises(Reached):
                main(
                    [
                        "--database-url",
                        self.VALID_STAGING,
                        "--include-local",
                        "--allow-nonlocal-dev",
                    ]
                )
        driver.connect.assert_called_once_with(self.VALID_STAGING)

    def test_runtime_identity_accepts_only_approved_database_names(self):
        for current_database, expected_database, expected in (
            ("railway", "railway", True),
            ("saas_platform", "saas_platform", True),
            ("railway", "saas_platform", False),
            ("saas_platform", "railway", False),
            ("other_database", "railway", False),
        ):
            with self.subTest(
                current_database=current_database, expected_database=expected_database
            ):
                connection = mock.MagicMock()
                cursor = connection.cursor.return_value.__enter__.return_value
                cursor.fetchone.return_value = (current_database,)
                self.assertEqual(
                    migration_runner.check_runtime_database_identity(
                        connection, expected_database
                    ),
                    expected,
                )

    def test_supabase_blocked_with_status_and_override(self):
        """Supabase should be blocked with --status and --allow-nonlocal-dev."""
        self._refused(self.SUPABASE, "--status", "--allow-nonlocal-dev")


if __name__ == "__main__":
    unittest.main()
