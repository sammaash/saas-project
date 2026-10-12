"""Supabase compatibility checks that run on plain PostgreSQL.

Supabase differs from a local cluster in three ways that matter here: the migration role is a
non-superuser (so it cannot freely own objects as other roles), new public tables are
granted to anon/authenticated/service_role by default privileges, and BYPASSRLS belongs to
the migration role. These tests pin the migrations and runner to that model.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from db import run_migrations as runner
from saas_test_support import SaasDatabaseTestCase

REPO = Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((REPO / "db" / "migrations").glob("*.sql"))

PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
PLATFORM_TABLES = (
    "tenants", "tenant_channels", "tenant_users", "tenant_settings", "plans", "subscriptions",
    "audit_log", "products", "product_variants", "platform_admins", "reserved_phone_numbers",
    "catalogue_import_batches", "quotations", "quotation_lines", "whatsapp_webhook_events",
    "whatsapp_conversations", "saas_schema_migrations",
)
AUTHENTICATED_EXPECTED = {
    "tenants": {"SELECT"},
    "tenant_channels": {"SELECT"},
    "tenant_users": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "tenant_settings": {"SELECT", "INSERT", "UPDATE"},
    "plans": {"SELECT"},
    "subscriptions": {"SELECT"},
    "audit_log": {"SELECT"},
    "products": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "product_variants": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "catalogue_import_batches": {"SELECT"},
    "quotations": {"SELECT"},
    "quotation_lines": {"SELECT"},
}
FULL_HARDENING = REPO / "db" / "migrations" / "0029_complete_privilege_hardening.sql"
HARDENING = REPO / "db" / "migrations" / "0030_catalogue_import_read_privilege.sql"


def _granted(cursor, role, table):
    held = set()
    for privilege in PRIVILEGES:
        cursor.execute("select has_table_privilege(%s, %s, %s)", (role, f"public.{table}", privilege))
        if cursor.fetchone()[0]:
            held.add(privilege)
    return held


class DefaultPrivilegeTests(SaasDatabaseTestCase):
    @staticmethod
    def _ensure_service_role(cursor):
        # A fresh PostgreSQL cluster (like CI's service container) has no
        # service_role: has_table_privilege() raises for a missing role, so the
        # privilege assertions below need the Supabase role to exist first.
        cursor.execute(
            "do $$ begin "
            "if not exists (select 1 from pg_roles where rolname = 'service_role') then "
            "create role service_role nologin; "
            "end if; "
            "end $$"
        )

    def _assert_intended_privileges(self, cursor):
        for table in PLATFORM_TABLES:
            with self.subTest(table=table, role="anon"):
                self.assertEqual(_granted(cursor, "anon", table), set())
            with self.subTest(table=table, role="authenticated"):
                self.assertEqual(_granted(cursor, "authenticated", table), AUTHENTICATED_EXPECTED.get(table, set()))
            with self.subTest(table=table, role="service_role"):
                self.assertEqual(_granted(cursor, "service_role", table), set())
        cursor.execute("select has_column_privilege('authenticated', 'public.tenants', 'name', 'UPDATE')")
        self.assertTrue(cursor.fetchone()[0])
        for column in ("status", "slug", "id"):
            cursor.execute("select has_column_privilege('authenticated', 'public.tenants', %s, 'UPDATE')", (column,))
            self.assertFalse(cursor.fetchone()[0], column)
        for role in ("anon", "authenticated", "service_role"):
            cursor.execute("select has_sequence_privilege(%s, 'public.audit_log_id_seq', 'USAGE,SELECT,UPDATE')", (role,))
            self.assertFalse(cursor.fetchone()[0], role)
        cursor.execute("select relrowsecurity from pg_class where oid = 'public.saas_schema_migrations'::regclass")
        self.assertTrue(cursor.fetchone()[0], "the migration ledger must have RLS enabled")

    def test_migrated_database_has_only_the_intended_dashboard_privileges(self):
        with self.superuser() as cursor:
            self._ensure_service_role(cursor)
            self._assert_intended_privileges(cursor)

    def _simulate_hosted_grants_then_harden(self, repeat):
        import psycopg  # noqa: PLC0415

        with self.superuser() as cursor:
            self._ensure_service_role(cursor)
            cursor.execute("grant all on all tables in schema public to anon, authenticated, service_role")
            cursor.execute("grant all on all sequences in schema public to anon, authenticated, service_role")
            cursor.execute("alter table public.saas_schema_migrations disable row level security")
            cursor.execute("grant all on public.saas_schema_migrations to anon, authenticated, service_role")
            self.assertGreater(len(_granted(cursor, "anon", "products")), 1, "simulation did not take effect")
            self.assertGreater(
                len(_granted(cursor, "service_role", "products")), 1, "simulation did not take effect"
            )
            sql = FULL_HARDENING.read_text(encoding="utf-8")
            import_read_grant = HARDENING.read_text(encoding="utf-8")
            for _ in range(repeat):
                psycopg.ClientCursor(cursor.connection).execute(sql)
                psycopg.ClientCursor(cursor.connection).execute(import_read_grant)
            self._assert_intended_privileges(cursor)

    def test_supabase_style_default_grants_are_removed(self):
        self._simulate_hosted_grants_then_harden(repeat=1)

    def test_hardening_is_idempotent(self):
        self._simulate_hosted_grants_then_harden(repeat=2)

    def test_hardening_only_touches_this_platforms_tables(self):
        text = HARDENING.read_text(encoding="utf-8")
        self.assertNotIn("all tables in schema", text.lower().replace("-- ", ""), "must not touch unrelated tables")
        self.assertNotIn("all sequences in schema", text.lower())


class FunctionSecurityTests(SaasDatabaseTestCase):
    def test_every_security_definer_function_pins_its_search_path(self):
        with self.superuser() as cursor:
            cursor.execute(
                """
                select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                 where n.nspname = 'app' and p.prosecdef
                   and not exists (select 1 from unnest(coalesce(p.proconfig, '{}')) c where c like 'search_path=%')
                """
            )
            self.assertEqual(cursor.fetchall(), [])

    def test_search_paths_put_pg_catalog_first(self):
        with self.superuser() as cursor:
            cursor.execute(
                """
                select p.proname, c from pg_proc p join pg_namespace n on n.oid = p.pronamespace,
                       unnest(coalesce(p.proconfig, '{}')) c
                 where n.nspname = 'app' and p.prosecdef and c like 'search_path=%'
                   and c not like 'search_path=pg_catalog,%'
                """
            )
            self.assertEqual(cursor.fetchall(), [])

    def test_no_security_definer_function_is_executable_by_public_or_anon(self):
        with self.superuser() as cursor:
            cursor.execute(
                """
                select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                 where n.nspname = 'app' and p.prosecdef
                   and (has_function_privilege('public', p.oid, 'EXECUTE')
                        or has_function_privilege('anon', p.oid, 'EXECUTE'))
                """
            )
            self.assertEqual(cursor.fetchall(), [])

    def test_platform_functions_are_owned_by_the_provisioner_not_the_migration_role(self):
        with self.superuser() as cursor:
            cursor.execute(
                "select p.proname, pg_get_userbyid(p.proowner) from pg_proc p "
                "where p.pronamespace = 'app'::regnamespace and p.proname like 'platform\\_%'"
            )
            owners = {owner for _name, owner in cursor.fetchall()}
        self.assertEqual(owners, {"saas_platform_provisioner"})


class MigrationPortabilityTests(unittest.TestCase):
    def test_phase4_hardening_does_not_reference_tables_created_later(self):
        phase4 = (
            REPO / "db" / "migrations" / "0027_supabase_phase4_privilege_hardening.sql"
        ).read_text(encoding="utf-8")
        self.assertNotIn("whatsapp_webhook_events", phase4)
        self.assertNotIn("whatsapp_conversations", phase4)

    """Static checks: nothing may depend on being a PostgreSQL superuser."""

    FORBIDDEN = re.compile(
        r"\balter\s+system\b|\bcreate\s+language\b|session_replication_role|set\s+session\s+authorization"
        r"|\bcopy\b[^;]*\bprogram\b|\bcreate\s+event\s+trigger\b|\bcreate\s+tablespace\b"
        r"|\bsuperuser\b(?!\s*\))|(?<!no)\bbypassrls\b|\breplication\b|create\s+extension\s+(?!if\s+not\s+exists\s+pgcrypto)",
        re.I,
    )

    @staticmethod
    def _code(path):
        """SQL without comments and string literals (so prose cannot trip the checks)."""
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"--[^\n]*", "", text)
        return re.sub(r"'(?:[^']|'')*'", "''", text)

    def test_no_migration_uses_superuser_only_features(self):
        for path in MIGRATIONS:
            with self.subTest(migration=path.name):
                hit = self.FORBIDDEN.search(self._code(path))
                self.assertIsNone(hit, hit.group(0) if hit else "")

    def test_owner_changes_are_bracketed_by_a_temporary_schema_create_grant(self):
        for path in MIGRATIONS:
            code = self._code(path).lower()
            changes = [m.start() for m in re.finditer(r"owner\s+to\s+saas_platform_provisioner", code)]
            if not changes or path.name.startswith("0014_"):  # 0014 re-asserts an existing owner
                continue
            with self.subTest(migration=path.name):
                grant = code.find("grant create on schema app to saas_platform_provisioner")
                revoke = code.rfind("revoke create on schema app from saas_platform_provisioner")
                self.assertTrue(0 <= grant < min(changes), "grant create must precede the first owner change")
                self.assertGreater(revoke, max(changes), "create must be revoked after the last owner change")

    def test_migration_files_never_create_the_roles_with_privileged_attributes(self):
        for path in MIGRATIONS:
            for match in re.finditer(r"create\s+role\s+(\w+)([^;]*);", self._code(path), re.I):
                with self.subTest(migration=path.name, role=match.group(1)):
                    attributes = match.group(2).lower()
                    self.assertNotRegex(attributes, r"superuser|createrole|createdb|(?<!no)bypassrls|replication")

    def test_hardening_migration_is_last_and_not_applied_with_other_grants_in_between(self):
        self.assertEqual(MIGRATIONS[-1].name, HARDENING.name)


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)
        if "rolsuper" in sql:
            self.row = (self.conn.superuser, self.conn.version)
        elif "where rolname = %s" in sql:
            self.row = (1,) if self.conn.role_exists else None
        elif "pg_has_role" in sql:
            self.row = (self.conn.has_membership,)

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, superuser=False, version=170011, role_exists=False, has_membership=False):
        self.superuser, self.version = superuser, version
        self.role_exists, self.has_membership = role_exists, has_membership
        self.executed, self.commits, self.rollbacks = [], 0, 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ran(self, fragment):
        return any(fragment in sql for sql in self.executed)


class ProvisionerAccessTests(unittest.TestCase):
    def test_superusers_need_no_membership(self):
        conn = _Conn(superuser=True)
        self.assertFalse(runner.ensure_provisioner_access(conn))
        self.assertFalse(conn.ran("grant"))
        self.assertFalse(conn.ran("create role"))

    def test_hosted_pg16_creates_the_role_and_self_grants_set_and_inherit(self):
        conn = _Conn()
        self.assertTrue(runner.ensure_provisioner_access(conn))
        self.assertTrue(conn.ran("create role saas_platform_provisioner nologin noinherit nobypassrls"))
        self.assertTrue(conn.ran("grant saas_platform_provisioner to current_user with inherit true, set true"))
        self.assertGreaterEqual(conn.commits, 1)

    def test_the_role_is_not_recreated_when_it_exists(self):
        conn = _Conn(role_exists=True)
        self.assertTrue(runner.ensure_provisioner_access(conn))
        self.assertFalse(conn.ran("create role"))

    def test_existing_usable_membership_is_left_alone_and_not_released(self):
        conn = _Conn(role_exists=True, has_membership=True)
        self.assertFalse(runner.ensure_provisioner_access(conn))
        self.assertFalse(conn.ran("grant saas_platform_provisioner"))

    def test_pg15_uses_plain_membership(self):
        conn = _Conn(version=150004, role_exists=True)
        self.assertTrue(runner.ensure_provisioner_access(conn))
        self.assertTrue(conn.ran("grant saas_platform_provisioner to current_user"))
        self.assertFalse(conn.ran("with inherit"))

    def test_membership_is_revoked_after_the_run(self):
        conn = _Conn()
        runner.release_provisioner_access(conn)
        self.assertTrue(conn.ran("revoke saas_platform_provisioner from current_user"))

    def _run_main(self, conn, apply_side_effect=None, extra=()):
        fake_psycopg = SimpleNamespace(connect=lambda url: conn)
        with mock.patch.object(runner, "_psycopg", return_value=fake_psycopg), \
                mock.patch.object(runner, "apply_migrations", side_effect=apply_side_effect):
            return runner.main(["--database-url", "postgresql://u@db.example.com/postgres", *extra])

    def test_membership_is_released_even_when_a_migration_fails(self):
        conn = _Conn()
        with self.assertRaises(RuntimeError):
            self._run_main(conn, apply_side_effect=RuntimeError("boom"))
        self.assertTrue(conn.ran("grant saas_platform_provisioner"))
        self.assertTrue(conn.ran("revoke saas_platform_provisioner from current_user"))
        self.assertGreaterEqual(conn.rollbacks, 1)

    def test_membership_is_released_after_a_successful_run(self):
        conn = _Conn()
        self.assertEqual(self._run_main(conn, apply_side_effect=lambda c: []), 0)
        self.assertTrue(conn.ran("revoke saas_platform_provisioner from current_user"))

    def test_a_superuser_run_never_grants_or_revokes(self):
        conn = _Conn(superuser=True)
        self.assertEqual(self._run_main(conn, apply_side_effect=lambda c: []), 0)
        self.assertFalse(conn.ran("grant saas_platform_provisioner"))
        self.assertFalse(conn.ran("revoke saas_platform_provisioner"))


if __name__ == "__main__":
    unittest.main()
