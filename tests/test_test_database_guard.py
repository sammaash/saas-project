"""Database-free proofs for the test-database fail-closed guard.

Nothing here opens a connection: the point is that missing or unsafe configuration is
rejected *before* ``psycopg.connect`` or ``apply_migrations`` can run.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests import saas_test_support as support

SAFE_LOCAL = "postgresql://postgres:pw@127.0.0.1:5432/saas_phase1_test"
SUPABASE = (
    "postgresql://postgres.sftjojcsojjxaabhcroe:pw"
    "@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
)


def _env(**values):
    return patch.dict(os.environ, values, clear=True)


class TestDatabaseGuardTests(unittest.TestCase):
    def test_missing_configuration_is_none_and_require_fails(self):
        with _env():
            self.assertIsNone(support.test_database_url())
            self.assertFalse(support.database_available())
            with self.assertRaises(support.TestDatabaseConfigurationError):
                support.require_test_database_url()

    def test_ambient_application_and_operator_dsns_are_never_used(self):
        with _env(
            SAAS_DATABASE_URL=SAFE_LOCAL,
            SAAS_OPERATOR_DATABASE_URL=SAFE_LOCAL,
            SAAS_REQUIRE_TEST_DATABASE="1",
        ):
            self.assertIsNone(support.test_database_url())
            with self.assertRaises(support.TestDatabaseConfigurationError):
                support.require_test_database_url()

    def test_supabase_hosts_are_refused(self):
        with _env(SAAS_TEST_DATABASE_URL=SUPABASE):
            with self.assertRaisesRegex(
                support.TestDatabaseConfigurationError, "Supabase"
            ):
                support.test_database_url()
            with self.assertRaises(support.TestDatabaseConfigurationError):
                support.database_available()

    def test_production_project_reference_is_refused(self):
        url = "postgresql://postgres:pw@127.0.0.1:5432/saas_test_sftjojcsojjxaabhcroe"
        with _env(
            SAAS_TEST_DATABASE_URL=url,
            SAAS_PRODUCTION_PROJECT_REF="sftjojcsojjxaabhcroe",
        ):
            with self.assertRaisesRegex(
                support.TestDatabaseConfigurationError, "production project"
            ):
                support.test_database_url()

    def test_non_local_database_needs_an_explicit_remote_opt_in(self):
        remote = "postgresql://postgres:pw@10.0.0.5:5432/saas_test"
        with _env(SAAS_TEST_DATABASE_URL=remote):
            with self.assertRaisesRegex(
                support.TestDatabaseConfigurationError, "non-local"
            ):
                support.test_database_url()
        with _env(
            SAAS_TEST_DATABASE_URL=remote, SAAS_TEST_DATABASE_ALLOW_REMOTE="1"
        ):
            self.assertEqual(support.test_database_url(), remote)

    def test_shared_database_names_are_refused_unless_confirmed(self):
        with _env(SAAS_TEST_DATABASE_URL="postgresql://postgres:pw@localhost:5432/postgres"):
            with self.assertRaisesRegex(
                support.TestDatabaseConfigurationError, "shared"
            ):
                support.test_database_url()
        with _env(
            SAAS_TEST_DATABASE_URL="postgresql://postgres:pw@localhost:5432/saas_ci",
            SAAS_TEST_DATABASE_CONFIRM="saas_ci",
        ):
            self.assertEqual(
                support.test_database_url(),
                "postgresql://postgres:pw@localhost:5432/saas_ci",
            )

    def test_query_parameters_cannot_redirect_the_target(self):
        for query in ("host=prod.example.com", "dbname=prod", "hostaddr=10.0.0.5"):
            with self.subTest(query=query), _env(
                SAAS_TEST_DATABASE_URL=(
                    f"postgresql://postgres:pw@127.0.0.1:5432/saas_test?{query}"
                )
            ):
                with self.assertRaisesRegex(
                    support.TestDatabaseConfigurationError, "redirect"
                ):
                    support.test_database_url()

    def test_app_backend_url_is_derived_only_for_loopback(self):
        with _env(SAAS_TEST_DATABASE_URL=SAFE_LOCAL):
            url = support.app_backend_test_url()
        self.assertIn("app_backend:", url)
        self.assertIn("@127.0.0.1:5432/saas_phase1_test", url)

        remote = "postgresql://postgres:pw@10.0.0.5:5432/saas_test"
        with _env(
            SAAS_TEST_DATABASE_URL=remote, SAAS_TEST_DATABASE_ALLOW_REMOTE="1"
        ):
            with self.assertRaisesRegex(
                support.TestDatabaseConfigurationError, "SAAS_TEST_APP_DATABASE_URL"
            ):
                support.app_backend_test_url()

    def test_app_backend_url_must_target_the_same_database(self):
        with _env(
            SAAS_TEST_DATABASE_URL=SAFE_LOCAL,
            SAAS_TEST_APP_DATABASE_URL=(
                "postgresql://app_backend:pw@127.0.0.1:5432/another_test"
            ),
        ):
            with self.assertRaisesRegex(
                support.TestDatabaseConfigurationError, "same test database"
            ):
                support.app_backend_test_url()

    def test_connect_is_never_reached_without_an_explicit_test_url(self):
        with _env(), patch("psycopg.connect") as connect:
            with self.assertRaises(support.TestDatabaseConfigurationError):
                support._connect()
            connect.assert_not_called()

    def test_database_test_class_skips_or_fails_without_a_test_url(self):
        with _env():
            with self.assertRaises(unittest.SkipTest):
                support.SaasDatabaseTestCase.setUpClass()
        with _env(SAAS_REQUIRE_TEST_DATABASE="1"):
            with self.assertRaises(AssertionError):
                support.SaasDatabaseTestCase.setUpClass()

    def test_database_test_class_errors_on_an_unsafe_url(self):
        with _env(SAAS_TEST_DATABASE_URL=SUPABASE):
            with self.assertRaises(support.TestDatabaseConfigurationError):
                support.SaasDatabaseTestCase.setUpClass()

    def test_migration_guard_precedes_the_connection(self):
        from pathlib import Path

        source = Path(__file__).with_name("test_saas_migration_runner.py").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            source.index("require_test_database_url()"),
            source.index("psycopg.connect(url)"),
        )


if __name__ == "__main__":
    unittest.main()
