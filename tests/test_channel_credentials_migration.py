"""Static contract for migration 0040 (channel_credentials).

These assertions read the migration text. They are not a substitute for applying it
against a database, but they pin the security model that must not drift: who owns the
table, who may read it, which role owns the functions, and the R27 grant that the
accessor needs in order to run as its owner.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "db" / "migrations"
MIGRATION = MIGRATIONS / "0040_channel_credentials.sql"


class MigrationFileTests(unittest.TestCase):
    def test_it_exists_and_follows_the_naming_convention(self):
        self.assertTrue(MIGRATION.is_file(), "0040_channel_credentials.sql must exist")
        names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        self.assertIn(MIGRATION.name, names)
        self.assertRegex(MIGRATION.name, r"^\d{4}_[a-z0-9_]+\.sql$")

    def test_it_does_not_alter_tenant_channels(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()
        self.assertNotIn("alter table public.tenant_channels", sql)
        self.assertNotIn("drop table", sql)
        self.assertNotIn("delete from", sql)


class TableTests(unittest.TestCase):
    def setUp(self):
        self.sql = MIGRATION.read_text(encoding="utf-8")
        self.lowered = self.sql.lower()

    def test_every_approved_column_exists(self):
        for column in (
            "id",
            "tenant_id",
            "channel_id",
            "kind",
            "ciphertext",
            "nonce",
            "algorithm",
            "key_ref",
            "token_fingerprint",
            "issued_at",
            "expires_at",
            "revoked_at",
            "rotated_from",
            "created_by",
            "created_at",
        ):
            with self.subTest(column=column):
                self.assertRegex(self.lowered, rf"\n\s+{column}\s+")

    def test_one_active_credential_per_channel_and_kind(self):
        self.assertIn("create unique index", self.lowered)
        self.assertRegex(
            self.lowered,
            r"channel_credentials_one_active_per_channel_kind_idx\s*\n\s*on public\.channel_credentials \(channel_id, kind\)\s*\n\s*where revoked_at is null",
        )

    def test_there_is_an_expiry_lookup_index(self):
        self.assertRegex(
            self.lowered,
            r"channel_credentials_expiry_idx\s*\n\s*on public\.channel_credentials \(expires_at\)",
        )

    def test_the_kind_and_algorithm_are_constrained(self):
        self.assertIn("channel_credentials_kind_check", self.lowered)
        self.assertIn("business_token", self.lowered)
        self.assertIn("registration_pin", self.lowered)
        self.assertIn("channel_credentials_algorithm_check", self.lowered)

    def test_rls_is_enabled_and_deliberately_not_forced(self):
        self.assertIn(
            "alter table public.channel_credentials enable row level security;", self.lowered
        )
        self.assertNotIn(
            "alter table public.channel_credentials force row level security", self.lowered
        )
        # The reason must be recorded, or a future reader will "fix" it.
        self.assertIn("force row level security", self.lowered)

    def test_exactly_one_policy_and_only_for_the_function_owner(self):
        policies = re.findall(r"create policy (\w+) on public\.channel_credentials", self.lowered)
        self.assertEqual(policies, ["channel_credentials_vault_owner"])
        self.assertRegex(
            self.lowered,
            r"create policy channel_credentials_vault_owner on public\.channel_credentials\s*\n\s*for all to saas_platform_provisioner",
        )

    def test_no_application_role_holds_a_privilege(self):
        self.assertRegex(
            self.lowered,
            r"revoke all on public\.channel_credentials\s*\n\s*from public, anon, authenticated, app_backend",
        )
        self.assertIn("revoke all on public.channel_credentials from service_role", self.lowered)
        granted = re.findall(
            r"grant (?:select, insert, update|[a-z, ]+) on public\.channel_credentials\s*\n?\s*to (\w+)",
            self.lowered,
        )
        self.assertEqual(granted, ["saas_platform_provisioner"])
        self.assertNotIn("delete on public.channel_credentials", self.lowered)

class FunctionTests(unittest.TestCase):
    def setUp(self):
        self.sql = MIGRATION.read_text(encoding="utf-8")
        self.lowered = self.sql.lower()

    def test_the_three_functions_exist_and_are_definer_with_a_pinned_path(self):
        names = (
            "app.channel_credential(",
            "app.platform_store_credential(",
            "app.platform_revoke_credential(",
        )
        for name in names:
            with self.subTest(function=name):
                start = self.lowered.index(f"create or replace function {name}")
                # Up to the next definition (or the ownership block at the end).
                following = [
                    self.lowered.index(f"create or replace function {other}", start + 1)
                    for other in names
                    if self.lowered.find(f"create or replace function {other}", start + 1) != -1
                ]
                end = min(following) if following else self.lowered.index("grant create on schema")
                block = self.lowered[start:end]
                self.assertIn("security definer", block, name)
                self.assertIn(
                    "set search_path = pg_catalog, public, app, pg_temp", block, name
                )

    def test_each_function_is_owned_by_the_provisioner(self):
        owners = re.findall(
            r"alter function (app\.\w+)\([^)]*\)\s*\n?\s*owner to (\w+)", self.lowered
        )
        self.assertEqual(len(owners), 3)
        for function, owner in owners:
            self.assertEqual(owner, "saas_platform_provisioner", function)

    def test_create_on_schema_is_granted_only_for_the_ownership_change(self):
        grant_at = self.lowered.index("grant create on schema app to saas_platform_provisioner")
        revoke_at = self.lowered.index("revoke create on schema app from saas_platform_provisioner")
        self.assertLess(grant_at, revoke_at)

    def test_public_execute_is_stripped_from_every_new_function(self):
        revoked = re.findall(r"revoke all on function (app\.\w+)\(", self.lowered)
        self.assertEqual(
            sorted(set(revoked)),
            [
                "app.channel_credential",
                "app.platform_revoke_credential",
                "app.platform_store_credential",
            ],
        )

    def test_the_accessor_is_granted_to_the_backend_only(self):
        self.assertRegex(
            self.lowered,
            r"grant execute on function app\.channel_credential\(uuid\) to app_backend;",
        )
        self.assertNotRegex(
            self.lowered,
            r"grant execute on function app\.channel_credential\(uuid\) to [^;]*authenticated",
        )
        for name in ("app.platform_store_credential", "app.platform_revoke_credential"):
            with self.subTest(function=name):
                self.assertRegex(
                    self.lowered,
                    rf"grant execute on function {name}\([^)]*\)\s*\n?\s*to saas_platform_admin;",
                )

class AccessorContractTests(unittest.TestCase):
    """The read path must be one row, tenant-gated, and loud without context."""

    def setUp(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        start = sql.index(
            "create or replace function app.channel_credential(p_channel_id uuid)"
        )
        end = sql.index("create or replace function app.platform_store_credential(")
        self.lowered = sql[start:end].lower()

    def test_it_takes_only_a_channel_id(self):
        self.assertIn(
            "create or replace function app.channel_credential(p_channel_id uuid)", self.lowered
        )
        self.assertNotIn("p_tenant_id", self.lowered)

    def test_it_fails_closed_without_tenant_context(self):
        self.assertIn("context_tenant uuid := app.current_tenant_id()", self.lowered)
        self.assertRegex(
            self.lowered, r"if context_tenant is null then\s*\n\s*raise exception"
        )
        self.assertIn("errcode = '42501'", self.lowered)

    def test_it_matches_the_channel_tenant_and_returns_one_row(self):
        self.assertIn("and c.tenant_id = context_tenant", self.lowered)
        self.assertIn("limit 1", self.lowered)

    def test_it_returns_inactive_rows_so_the_caller_can_refuse_them(self):
        self.assertIn("c.revoked_at", self.lowered)
        self.assertIn("c.expires_at", self.lowered)


class WriteFunctionContractTests(unittest.TestCase):
    def setUp(self):
        self.lowered = MIGRATION.read_text(encoding="utf-8").lower()

    def test_the_write_functions_require_a_platform_administrator(self):
        self.assertEqual(self.lowered.count("perform app.require_platform_admin();"), 2)

    def test_the_tenant_is_derived_from_the_channel_never_supplied(self):
        self.assertIn("select c.tenant_id into target_tenant", self.lowered)
        self.assertIn("from public.tenant_channels c", self.lowered)
        for function in ("platform_store_credential", "platform_revoke_credential"):
            with self.subTest(function=function):
                start = self.lowered.index(f"function app.{function}(")
                body = self.lowered[start : start + 1200]
                self.assertNotIn("p_tenant_id", body)

    def test_storing_revokes_the_previous_live_credential_first(self):
        store = self.lowered[self.lowered.index("function app.platform_store_credential(") :]
        revoke_at = store.index("set revoked_at = now()")
        insert_at = store.index("insert into public.channel_credentials")
        self.assertLess(revoke_at, insert_at)

    def test_both_write_functions_audit(self):
        self.assertIn("'credential.stored'", self.lowered)
        self.assertIn("'credential.revoked'", self.lowered)
        self.assertIn("insert into public.audit_log", self.lowered)


class R27Tests(unittest.TestCase):
    """The grant without which every send fails at runtime."""

    def setUp(self):
        self.lowered = MIGRATION.read_text(encoding="utf-8").lower()

    def test_the_provisioner_may_execute_the_tenant_context_helper(self):
        self.assertIn(
            "grant execute on function app.current_tenant_id() to saas_platform_provisioner;",
            self.lowered,
        )

    def test_the_reason_is_documented_next_to_the_grant(self):
        index = self.lowered.index(
            "grant execute on function app.current_tenant_id() to saas_platform_provisioner;"
        )
        context = self.lowered[max(0, index - 800) : index]
        self.assertIn("r27", context)
        self.assertIn("0012", context)


class AuditFingerprintTests(unittest.TestCase):
    """The credential fingerprint must never be written to audit_log.

    audit_log rows are readable by every active tenant member (0011/0012), and an unsalted
    SHA-256 of a low-entropy secret (a six-digit registration PIN) is plaintext-equivalent.
    0040 had never been applied to production and staging is recreated from scratch, so the
    fix is made in 0040 itself instead of a replacement migration.
    """

    def setUp(self):
        self.lowered = MIGRATION.read_text(encoding="utf-8").lower()

    def test_the_audit_metadata_contains_no_fingerprint_under_any_key(self):
        audit_start = self.lowered.index("insert into public.audit_log", self.lowered.index(
            "create or replace function app.platform_store_credential"))
        audit_end = self.lowered.index("return new_id", audit_start)
        block = "\n".join(
            line for line in self.lowered[audit_start:audit_end].splitlines()
            if not line.strip().startswith("--")
        )
        self.assertNotIn("fingerprint", block)
        self.assertIn("jsonb_build_object", block)

    def test_the_fingerprint_is_still_validated_and_stored_in_the_vault_only(self):
        self.assertRegex(self.lowered, r"p_token_fingerprint !~ '\^\[0-9a-f\]\{64\}\$'")
        self.assertIn("token_fingerprint, expires_at, rotated_from, created_by", self.lowered)


class R31DataMigrationTests(unittest.TestCase):
    """A migration that changes data must state — and can assert — how much it changes.

    FORCE-RLS tables silently update zero rows when the migration runs as their
    non-superuser owner, so an unasserted data change can "succeed" while doing
    nothing. Function bodies are excluded: they run at request time, not at migration
    time, and cannot be checked this way.
    """

    STATEMENT_DML = re.compile(r"^\s*(insert\s+into|update|delete\s+from)\s", re.M)

    def _statement_level_sql(self, sql: str) -> str:
        without_bodies = re.sub(r"\$\$.*?\$\$", "", sql, flags=re.S)
        return re.sub(r"--[^\n]*", "", without_bodies)

    def test_0040_changes_no_data(self):
        sql = self._statement_level_sql(MIGRATION.read_text(encoding="utf-8"))
        self.assertIsNone(self.STATEMENT_DML.search(sql), "0040 must be schema-only")

    def test_every_new_data_migration_states_its_expected_row_count(self):
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < "0040":
                continue
            sql = self._statement_level_sql(path.read_text(encoding="utf-8"))
            if not self.STATEMENT_DML.search(sql):
                continue
            with self.subTest(migration=path.name):
                self.assertRegex(sql, r"(?i)expected rows:\s*\d+")


class SchemaInventoryTests(unittest.TestCase):
    """B-A: the vault table must be declared everywhere this repo enumerates tables."""

    @staticmethod
    def _support():
        try:
            from . import saas_test_support as support
        except ImportError:  # discovery inside the tests directory
            import saas_test_support as support  # type: ignore[no-redef]

        return support

    def test_it_is_declared_as_tenant_scoped(self):
        support = self._support()
        self.assertIn(
            ("channel_credentials", "tenant_id"), support.TENANT_SCOPED_TABLES
        )

    def test_no_application_role_may_read_it(self):
        support = self._support()
        self.assertIn("channel_credentials", support.AUTHENTICATED_INACCESSIBLE_TABLES)
        self.assertIn("channel_credentials", support.APP_BACKEND_INACCESSIBLE_TABLES)


if __name__ == "__main__":
    unittest.main()
