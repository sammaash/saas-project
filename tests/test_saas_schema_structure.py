"""Structural checks on the Phase 1 schema (requirements 9, 10 and 11).

These tests inspect the PostgreSQL catalog rather than rows, so they keep working as the
schema grows: a new tenant-scoped table that forgets `tenant_id`, RLS, or its policies
fails here instead of quietly escaping the isolation suite.

Run with the rest of the suite:

    python -m unittest discover -s tests -p "test_*.py" -v
"""

from __future__ import annotations

import unittest

try:
    from .saas_test_support import (
        NOT_FORCED_BY_DESIGN,
        PLATFORM_OWNED_TABLES,
        TENANT_ONE_ID,
        TENANT_ONE_OWNER_USER_ID,
        TENANT_SCOPED_TABLES,
        TENANT_TWO_ID,
        TABLES_WITH_NULLABLE_TENANT_ID,
        SaasDatabaseTestCase,
    )
except ImportError:  # discovery inside the tests directory
    from saas_test_support import (  # type: ignore[no-redef]
        NOT_FORCED_BY_DESIGN,
        PLATFORM_OWNED_TABLES,
        TENANT_ONE_ID,
        TENANT_ONE_OWNER_USER_ID,
        TENANT_SCOPED_TABLES,
        TENANT_TWO_ID,
        TABLES_WITH_NULLABLE_TENANT_ID,
        SaasDatabaseTestCase,
    )

# Every table created in Phase 1.
PHASE_1_TABLES = (
    "tenants",
    "tenant_channels",
    "tenant_users",
    "tenant_settings",
    "plans",
    "subscriptions",
    "audit_log",
)

# Tables that carry a tenant identity in a column named tenant_id. `tenants` is excluded
# because its primary key (id) IS the tenant identity -- documented in 0003.
TABLES_WITH_TENANT_ID_COLUMN = tuple(
    table for table, column in TENANT_SCOPED_TABLES if column == "tenant_id"
)

# Requirement 11 applies to these: tenant_id must exist AND be NOT NULL. audit_log is
# excluded because its tenant_id is nullable by design (platform-level events) -- a
# dedicated test below asserts that exception explicitly.
TABLES_REQUIRING_NOT_NULL_TENANT_ID = tuple(
    table for table in TABLES_WITH_TENANT_ID_COLUMN
    if table not in TABLES_WITH_NULLABLE_TENANT_ID
)

# Audit history must be immutable through the application path.
APPEND_ONLY_TABLES = ("audit_log",)

APPLICATION_ROLES = ("app_backend", "authenticated")


class RlsConfigurationTests(SaasDatabaseTestCase):
    """Requirement 9: RLS is enabled on every Phase 1 table."""

    def test_rls_is_enabled_on_every_phase_1_table(self):
        with self.superuser() as cursor:
            cursor.execute(
                """
                select c.relname, c.relrowsecurity
                  from pg_class c
                  join pg_namespace n on n.oid = c.relnamespace
                 where n.nspname = 'public'
                   and c.relkind = 'r'
                   and c.relname = any(%s)
                """,
                (list(PHASE_1_TABLES),),
            )
            rows = dict(cursor.fetchall())

        missing = [table for table in PHASE_1_TABLES if table not in rows]
        self.assertEqual(missing, [], f"Phase 1 tables missing from the database: {missing}")

        not_enabled = [table for table, enabled in rows.items() if not enabled]
        self.assertEqual(not_enabled, [], f"RLS is not enabled on: {not_enabled}")


class RlsForceTests(SaasDatabaseTestCase):
    """Requirement 10: RLS is forced where required (with documented exceptions)."""

    def test_rls_is_forced_where_required(self):
        with self.superuser() as cursor:
            cursor.execute(
                """
                select c.relname, c.relforcerowsecurity
                  from pg_class c
                  join pg_namespace n on n.oid = c.relnamespace
                 where n.nspname = 'public'
                   and c.relkind = 'r'
                   and c.relname = any(%s)
                """,
                (list(PHASE_1_TABLES),),
            )
            forced = {table for table, is_forced in cursor.fetchall() if is_forced}

        expected_forced = set(PHASE_1_TABLES) - set(NOT_FORCED_BY_DESIGN)
        self.assertEqual(
            forced,
            expected_forced,
            "FORCE ROW LEVEL SECURITY does not match the documented set. tenant_channels "
            "and tenant_users are intentionally not forced because the SECURITY DEFINER "
            "helpers read them; everything else must be forced.",
        )

    def test_not_forced_tables_are_still_filtered_for_application_roles(self):
        """The FORCE exception must not weaken application isolation.

        FORCE only affects the table OWNER, and the application never connects as the
        owner -- so these two tables must still hide other tenants' rows from
        app_backend and authenticated.
        """
        for table in sorted(NOT_FORCED_BY_DESIGN):
            with self.subTest(table=table, role="app_backend"):
                with self.backend(TENANT_ONE_ID) as cursor:
                    self.assertEqual(
                        self.count_rows(cursor, table, "tenant_id", TENANT_TWO_ID), 0
                    )

            with self.subTest(table=table, role="authenticated"):
                with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
                    self.assertEqual(
                        self.count_rows(cursor, table, "tenant_id", TENANT_TWO_ID), 0
                    )

class TenantIdColumnTests(SaasDatabaseTestCase):
    """Requirement 11: tenant_id exists and is NOT NULL on every tenant-owned table."""

    def test_tenant_id_exists_and_is_not_null_on_every_tenant_owned_table(self):
        with self.superuser() as cursor:
            cursor.execute(
                """
                select table_name, column_name, is_nullable
                  from information_schema.columns
                 where table_schema = 'public'
                   and table_name = any(%s)
                   and column_name = 'tenant_id'
                """,
                (list(TABLES_REQUIRING_NOT_NULL_TENANT_ID),),
            )
            columns = {
                table: is_nullable for table, _column, is_nullable in cursor.fetchall()
            }

        for table in TABLES_REQUIRING_NOT_NULL_TENANT_ID:
            with self.subTest(table=table):
                self.assertIn(table, columns, f"{table} has no tenant_id column")
                self.assertEqual(
                    columns[table], "NO", f"{table}.tenant_id must be NOT NULL"
                )

    def test_platform_owned_tables_have_no_tenant_id(self):
        """plans belongs to the platform; a tenant_id here would be a design error."""
        with self.superuser() as cursor:
            cursor.execute(
                """
                select table_name
                  from information_schema.columns
                 where table_schema = 'public'
                   and column_name = 'tenant_id'
                   and table_name = any(%s)
                """,
                (list(PLATFORM_OWNED_TABLES),),
            )
            self.assertEqual(cursor.fetchall(), [])

    def test_only_the_documented_exception_allows_a_null_tenant_id(self):
        """audit_log is the ONLY table whose tenant_id may be NULL, because a
        platform-level audit event has no tenant. Any other nullable tenant_id is a bug."""
        with self.superuser() as cursor:
            cursor.execute(
                """
                select table_name
                  from information_schema.columns
                 where table_schema = 'public'
                   and table_name = any(%s)
                   and column_name = 'tenant_id'
                   and is_nullable = 'YES'
                """,
                (list(TABLES_WITH_TENANT_ID_COLUMN),),
            )
            nullable = sorted(table for (table,) in cursor.fetchall())

        self.assertEqual(
            nullable,
            sorted(TABLES_WITH_NULLABLE_TENANT_ID),
            "tenant_id may only be nullable on the documented exceptions",
        )


class PolicyCoverageTests(SaasDatabaseTestCase):
    """Every tenant-owned table must be governed by policies, not by convention."""

    def test_every_tenant_owned_table_has_policies(self):
        with self.superuser() as cursor:
            cursor.execute(
                """
                select tablename, cmd
                  from pg_policies
                 where schemaname = 'public'
                   and tablename = any(%s)
                """,
                (list(TABLES_WITH_TENANT_ID_COLUMN) + ["tenants"],),
            )
            policies: dict[str, set[str]] = {}
            for table, command in cursor.fetchall():
                policies.setdefault(table, set()).add(command)

        for table in TABLES_WITH_TENANT_ID_COLUMN + ("tenants",):
            with self.subTest(table=table):
                commands = policies.get(table, set())
                self.assertIn("SELECT", commands, f"{table} has no SELECT policy")
                self.assertTrue(
                    commands - {"SELECT"},
                    f"{table} has a SELECT policy but no write policy at all",
                )

    def test_audit_log_has_no_update_or_delete_policy(self):
        with self.superuser() as cursor:
            cursor.execute(
                """
                select cmd from pg_policies
                 where schemaname = 'public' and tablename = 'audit_log'
                """
            )
            commands = {command for (command,) in cursor.fetchall()}

        self.assertNotIn("UPDATE", commands, "audit_log must not be updatable")
        self.assertNotIn("DELETE", commands, "audit_log must not be deletable")

class GrantAndRoleTests(SaasDatabaseTestCase):
    """Least privilege: the application role is ordinary, and anon gets nothing."""

    def test_app_backend_cannot_bypass_rls(self):
        with self.superuser() as cursor:
            cursor.execute(
                """
                select rolname, rolsuper, rolbypassrls
                  from pg_roles
                 where rolname = any(%s)
                """,
                (list(APPLICATION_ROLES),),
            )
            roles = {
                name: (is_super, bypasses) for name, is_super, bypasses in cursor.fetchall()
            }

        self.assertIn("app_backend", roles, "app_backend role is missing")
        is_super, bypasses = roles["app_backend"]
        self.assertFalse(is_super, "app_backend must not be a superuser")
        self.assertFalse(bypasses, "app_backend must not have BYPASSRLS")

    def test_append_only_tables_have_no_update_or_delete_grants(self):
        with self.superuser() as cursor:
            cursor.execute(
                """
                select grantee, privilege_type
                  from information_schema.role_table_grants
                 where table_schema = 'public'
                   and table_name = any(%s)
                """,
                (list(APPEND_ONLY_TABLES),),
            )
            grants = {(grantee, privilege) for grantee, privilege in cursor.fetchall()}

        for grantee in APPLICATION_ROLES:
            for privilege in ("UPDATE", "DELETE"):
                with self.subTest(grantee=grantee, privilege=privilege):
                    self.assertNotIn(
                        (grantee, privilege),
                        grants,
                        f"{grantee} must not hold {privilege} on an append-only table",
                    )

    def test_anon_has_no_privileges_on_phase_1_tables(self):
        with self.superuser() as cursor:
            cursor.execute(
                """
                select table_name, privilege_type
                  from information_schema.role_table_grants
                 where table_schema = 'public'
                   and grantee = 'anon'
                   and table_name = any(%s)
                """,
                (list(PHASE_1_TABLES),),
            )
            self.assertEqual(
                cursor.fetchall(), [], "anon must have no access to platform tables"
            )

    def test_tenants_table_is_not_writable_by_dashboard_sessions(self):
        """Provisioning and offboarding are platform actions, not tenant actions."""
        with self.superuser() as cursor:
            cursor.execute(
                """
                select privilege_type
                  from information_schema.role_table_grants
                 where table_schema = 'public'
                   and table_name = 'tenants'
                   and grantee = 'authenticated'
                """
            )
            privileges = {privilege for (privilege,) in cursor.fetchall()}

        self.assertIn("SELECT", privileges)
        self.assertNotIn("INSERT", privileges, "tenants must not be insertable by tenants")
        self.assertNotIn("DELETE", privileges, "tenants must not be deletable by tenants")


if __name__ == "__main__":
    unittest.main()
