"""Regression tests for the security-hardening pass (migrations 0023-0024, db/local guards).

Channels are written only by platform functions; platform-attributed audit events can only
come from platform functions; a NULL owner waiver is never a bypass; development-only SQL
refuses to run unless the migration runner explicitly allows it.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from saas_test_support import (
    PLATFORM_ADMIN_USER_ID,
    TENANT_ONE_ID,
    TENANT_ONE_OWNER_USER_ID,
    TENANT_ONE_PHONE_NUMBER_ID,
    TENANT_TWO_ID,
    TENANT_TWO_OWNER_USER_ID,
    SaasDatabaseTestCase,
    as_platform,
)
from test_saas_platform import _add_catalogue, _errors, _provision

REPO = Path(__file__).resolve().parent.parent
CHANNEL_FUNCTIONS = (
    ("app.platform_create_channel(uuid, text, text, text)", "select app.platform_create_channel('{t}', 'new-phone-1')"),
    ("app.platform_update_channel(uuid, text, text, text)", "select app.platform_update_channel('{c}', 'new-phone-2')"),
    ("app.platform_set_channel_status(uuid, text)", "select app.platform_set_channel_status('{c}', 'disabled')"),
    ("app.platform_record_audit(uuid, text, text, text, jsonb)", "select app.platform_record_audit('{t}', 'x.y')"),
)


def _channel_id(cursor, tenant_id):
    cursor.execute("select id::text from public.tenant_channels where tenant_id = %s", (tenant_id,))
    return cursor.fetchone()[0]


class ChannelWriteBoundaryTests(SaasDatabaseTestCase):
    def test_tenant_facing_roles_have_no_write_privileges_on_channels(self):
        with self.superuser() as cursor:
            for role in ("app_backend", "authenticated", "anon", "saas_platform_admin"):
                for privilege in ("INSERT", "UPDATE", "DELETE"):
                    with self.subTest(role=role, privilege=privilege):
                        cursor.execute(
                            "select has_table_privilege(%s, 'public.tenant_channels', %s)", (role, privilege)
                        )
                        self.assertFalse(cursor.fetchone()[0])

    def test_tenant_roles_can_still_read_their_own_channels(self):
        with self.backend(TENANT_ONE_ID) as cursor:
            cursor.execute("select phone_number_id from public.tenant_channels")
            self.assertEqual(cursor.fetchall(), [(TENANT_ONE_PHONE_NUMBER_ID,)])
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            cursor.execute("select count(*) from public.tenant_channels")
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_app_backend_cannot_create_activate_or_rebind_a_channel(self):
        statements = (
            f"insert into public.tenant_channels (tenant_id, phone_number_id, status, is_default) "
            f"values ('{TENANT_ONE_ID}', 'backend-made-1', 'active', false)",
            f"insert into public.tenant_channels (tenant_id, phone_number_id, is_default) "
            f"values ('{TENANT_ONE_ID}', 'backend-made-2', false)",
            "update public.tenant_channels set status = 'active'",
            "update public.tenant_channels set phone_number_id = 'rebound-by-backend'",
            "delete from public.tenant_channels",
        )
        for sql in statements:
            with self.subTest(sql=sql), self.backend(TENANT_ONE_ID) as cursor:
                with self.assertRaises(_errors().InsufficientPrivilege):
                    cursor.execute(sql)

    def test_tenant_owner_cannot_create_activate_or_rebind_a_channel(self):
        statements = (
            f"insert into public.tenant_channels (tenant_id, phone_number_id, status, is_default) "
            f"values ('{TENANT_ONE_ID}', 'owner-squat-1', 'active', false)",
            "update public.tenant_channels set status = 'active'",
            "update public.tenant_channels set phone_number_id = 'owner-rebound'",
            "update public.tenant_channels set tenant_id = '%s'" % TENANT_TWO_ID,
            "delete from public.tenant_channels",
        )
        for sql in statements:
            with self.subTest(sql=sql), self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
                with self.assertRaises(_errors().InsufficientPrivilege):
                    cursor.execute(sql)

    def test_channel_cannot_be_moved_to_another_tenant_by_anyone(self):
        with self.superuser() as cursor:
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute(
                    "update public.tenant_channels set tenant_id = %s where tenant_id = %s",
                    (TENANT_TWO_ID, TENANT_ONE_ID),
                )

    def test_channel_functions_reject_non_administrators(self):
        with self.superuser() as cursor:
            channel = _channel_id(cursor, TENANT_ONE_ID)
        for signature, template in CHANNEL_FUNCTIONS:
            with self.subTest(function=signature):
                with as_platform(TENANT_ONE_OWNER_USER_ID, register=False) as cursor:
                    with self.assertRaises(_errors().InsufficientPrivilege):
                        cursor.execute(template.format(t=TENANT_ONE_ID, c=channel))

    def test_channel_functions_are_closed_to_tenant_roles(self):
        with self.superuser() as cursor:
            for signature, _template in CHANNEL_FUNCTIONS:
                for role in ("app_backend", "authenticated", "anon", "public"):
                    with self.subTest(function=signature, role=role):
                        cursor.execute(
                            "select has_function_privilege(%s, %s::regprocedure, 'EXECUTE')", (role, signature)
                        )
                        self.assertFalse(cursor.fetchone()[0])

    def test_platform_creates_a_pending_default_channel_with_no_credentials(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant = _provision(cursor)
            cursor.execute(
                "select app.platform_create_channel(%s, 'plat-phone-1', 'plat-waba-1', '+254700000321')", (tenant,)
            )
            first = cursor.fetchone()[0]
            cursor.execute("select app.platform_create_channel(%s, 'plat-phone-2')", (tenant,))
            second = cursor.fetchone()[0]
            cursor.execute("reset role")
            cursor.execute(
                "select phone_number_id, waba_id, display_phone_number, status, is_default, credentials_ref "
                "from public.tenant_channels where id = %s", (first,))
            self.assertEqual(cursor.fetchone(), ("plat-phone-1", "plat-waba-1", "+254700000321", "pending", True, None))
            cursor.execute("select is_default, status from public.tenant_channels where id = %s", (second,))
            self.assertEqual(cursor.fetchone(), (False, "pending"))
            cursor.execute(
                "select actor_type, actor_user_id::text from public.audit_log "
                "where tenant_id = %s and action = 'channel.created'", (tenant,))
            self.assertEqual(cursor.fetchall(), [("platform", PLATFORM_ADMIN_USER_ID)] * 2)

    def test_invalid_identifiers_are_rejected_by_the_database(self):
        bad = (("has space", None, None), ("ab", None, None), ("good-phone", "bad waba!", None),
               ("good-phone", None, "0700000000"), (None, None, None))
        for phone, waba, display in bad:
            with self.subTest(phone=phone, waba=waba, display=display):
                with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
                    tenant = _provision(cursor)
                    with self.assertRaises(_errors().InvalidParameterValue):
                        cursor.execute("select app.platform_create_channel(%s, %s, %s, %s)",
                                       (tenant, phone, waba, display))

    def test_a_number_cannot_be_bound_to_two_tenants(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant = _provision(cursor)
            with self.assertRaises(_errors().UniqueViolation):
                cursor.execute("select app.platform_create_channel(%s, %s)", (tenant, TENANT_ONE_PHONE_NUMBER_ID))

    def test_unknown_tenant_or_channel_is_not_found(self):
        missing = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        for sql in (f"select app.platform_create_channel('{missing}', 'ghost-phone')",
                    f"select app.platform_update_channel('{missing}', 'ghost-phone')",
                    f"select app.platform_set_channel_status('{missing}', 'disabled')"):
            with self.subTest(sql=sql), as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
                with self.assertRaises(_errors().NoDataFound):
                    cursor.execute(sql)

    def test_reserved_numbers_can_never_be_bound(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant = _provision(cursor)
            cursor.execute("reset role")
            cursor.execute("insert into public.reserved_phone_numbers (phone_number_id, reason) values ('live-reserved-1', 'live number')")
            cursor.execute("savepoint s")
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute("insert into public.tenant_channels (tenant_id, phone_number_id) values (%s, 'live-reserved-1')", (tenant,))
            cursor.execute("rollback to savepoint s")
            cursor.execute("set local role saas_platform_admin")
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute("select app.platform_create_channel(%s, 'live-reserved-1')", (tenant,))

    def test_renaming_a_channel_to_a_reserved_number_is_refused(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant = _provision(cursor)
            cursor.execute("select app.platform_create_channel(%s, 'plain-phone-1')", (tenant,))
            channel = cursor.fetchone()[0]
            cursor.execute("reset role")
            cursor.execute("insert into public.reserved_phone_numbers (phone_number_id) values ('live-reserved-2')")
            cursor.execute("set local role saas_platform_admin")
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute("select app.platform_update_channel(%s, 'live-reserved-2')", (channel,))

    def test_reserved_numbers_table_is_closed_to_every_application_role(self):
        with self.superuser() as cursor:
            for role in ("app_backend", "authenticated", "anon", "saas_platform_admin"):
                with self.subTest(role=role):
                    cursor.execute(
                        "select has_table_privilege(%s, 'public.reserved_phone_numbers', 'SELECT,INSERT,UPDATE,DELETE')",
                        (role,))
                    self.assertFalse(cursor.fetchone()[0])

    def test_active_channel_needs_an_active_tenant_and_identifiers_are_frozen_while_active(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant = _provision(cursor, owner=PLATFORM_ADMIN_USER_ID)
            cursor.execute("select app.platform_create_channel(%s, 'frozen-phone-1')", (tenant,))
            channel = cursor.fetchone()[0]
            cursor.execute("savepoint s")
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute("select app.platform_set_channel_status(%s, 'active')", (channel,))
            cursor.execute("rollback to savepoint s")
            _add_catalogue(cursor, tenant)
            cursor.execute("select app.platform_set_tenant_status(%s, 'active')", (tenant,))
            cursor.execute("select app.platform_update_channel(%s, 'frozen-phone-2', 'w-1')", (channel,))
            cursor.execute("select app.platform_set_channel_status(%s, 'active')", (channel,))
            cursor.execute("savepoint s2")
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute("select app.platform_update_channel(%s, 'frozen-phone-3')", (channel,))
            cursor.execute("rollback to savepoint s2")
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute("reset role")
                cursor.execute("update public.tenant_channels set phone_number_id = 'sneaky' where id = %s", (channel,))
            cursor.execute("rollback")

    def test_invalid_channel_status_is_refused_and_null_is_not_a_bypass(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant = _provision(cursor)
            cursor.execute("select app.platform_create_channel(%s, 'status-phone-1')", (tenant,))
            channel = cursor.fetchone()[0]
            for status in ("bogus", None):
                cursor.execute("savepoint s")
                with self.assertRaises(_errors().InvalidParameterValue):
                    cursor.execute("select app.platform_set_channel_status(%s, %s)", (channel, status))
                cursor.execute("rollback to savepoint s")

    def test_credentials_ref_scope_still_applies_to_every_writer(self):
        with self.superuser() as cursor:
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute(
                    "insert into public.tenant_channels (tenant_id, phone_number_id, credentials_ref, is_default) "
                    "values (%s, 'cross-ref-1', %s, false)", (TENANT_ONE_ID, f"tenants/{TENANT_TWO_ID}/whatsapp"))


class AuditIntegrityTests(SaasDatabaseTestCase):
    def test_app_backend_cannot_forge_platform_or_user_audit_events(self):
        for actor_type in ("platform", "user"):
            with self.subTest(actor_type=actor_type), self.backend(TENANT_ONE_ID) as cursor:
                with self.assertRaises(_errors().InsufficientPrivilege):
                    cursor.execute(
                        "insert into public.audit_log (tenant_id, actor_user_id, actor_type, action) "
                        "values (app.current_tenant_id(), %s, %s, 'tenant.status_changed')",
                        (PLATFORM_ADMIN_USER_ID, actor_type))

    def test_app_backend_can_still_append_system_events(self):
        with self.backend(TENANT_ONE_ID) as cursor:
            cursor.execute(
                "insert into public.audit_log (tenant_id, actor_type, action) "
                "values (app.current_tenant_id(), 'system', 'worker.ran')")
            self.assertEqual(cursor.rowcount, 1)

    def test_platform_audit_function_records_the_verified_actor_only(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            cursor.execute(
                "select app.platform_record_audit(%s, 'catalogue.test', 'product', 'p1', %s::jsonb)",
                (TENANT_ONE_ID, json.dumps({"k": "v"})))
            cursor.execute("reset role")
            cursor.execute(
                "select actor_type, actor_user_id::text, metadata ->> 'k' from public.audit_log "
                "where tenant_id = %s and action = 'catalogue.test'", (TENANT_ONE_ID,))
            self.assertEqual(cursor.fetchall(), [("platform", PLATFORM_ADMIN_USER_ID, "v")])

    def test_platform_audit_function_rejects_non_administrators(self):
        for user_id in (TENANT_ONE_OWNER_USER_ID, None):
            with self.subTest(user=user_id), as_platform(user_id, register=False) as cursor:
                with self.assertRaises(_errors().InsufficientPrivilege):
                    cursor.execute("select app.platform_record_audit(%s, 'forged.event')", (TENANT_ONE_ID,))

    def test_platform_audit_function_rejects_unknown_tenants(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            with self.assertRaises(_errors().NoDataFound):
                cursor.execute("select app.platform_record_audit('ffffffff-ffff-4fff-8fff-ffffffffffff', 'x.y')")

    def test_dashboard_users_cannot_write_audit_at_all(self):
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            with self.assertRaises(_errors().InsufficientPrivilege):
                cursor.execute(
                    "insert into public.audit_log (tenant_id, actor_type, action) values (%s, 'platform', 'x.y')",
                    (TENANT_ONE_ID,))

    def test_audit_remains_isolated_between_tenants(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            cursor.execute("select app.platform_record_audit(%s, 'tenant.two_only')", (TENANT_TWO_ID,))
            cursor.execute("reset role")
            cursor.execute("set local role app_backend")
            cursor.execute("select set_config('app.tenant_id', %s, true)", (TENANT_ONE_ID,))
            cursor.execute("select count(*) from public.audit_log where action = 'tenant.two_only'")
            self.assertEqual(cursor.fetchone()[0], 0)


class NullWaiverTests(SaasDatabaseTestCase):
    def _ready_tenant_without_owner(self, cursor):
        tenant = _provision(cursor)
        _add_catalogue(cursor, tenant)
        return tenant

    def test_null_waiver_does_not_bypass_the_owner_requirement(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant = self._ready_tenant_without_owner(cursor)
            cursor.execute("savepoint before_activation")
            with self.assertRaises(_errors().CheckViolation) as raised:
                cursor.execute("select app.platform_set_tenant_status(%s, 'active', null)", (tenant,))
            self.assertIn("owner", str(raised.exception))
            cursor.execute("rollback to savepoint before_activation")
            cursor.execute("reset role")
            cursor.execute("select status from public.tenants where id = %s", (tenant,))
            self.assertEqual(cursor.fetchone()[0], "draft")

    def test_explicit_true_waiver_still_works_and_is_audited(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant = self._ready_tenant_without_owner(cursor)
            cursor.execute("select app.platform_set_tenant_status(%s, 'active', true)", (tenant,))
            cursor.execute("reset role")
            cursor.execute(
                "select (metadata ->> 'owner_waived')::boolean from public.audit_log "
                "where tenant_id = %s and action = 'tenant.status_changed'", (tenant,))
            self.assertTrue(cursor.fetchone()[0])

    def test_null_status_is_refused(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant = _provision(cursor)
            with self.assertRaises(_errors().InvalidParameterValue):
                cursor.execute("select app.platform_set_tenant_status(%s, null)", (tenant,))


class DevelopmentSqlGuardTests(SaasDatabaseTestCase):
    """db/local and db/seeds refuse to run unless the runner enabled development data."""

    GUARDED = sorted((REPO / "db" / "local").glob("*.sql")) + sorted((REPO / "db" / "seeds").glob("*.sql"))

    def _run(self, path, allow):
        import psycopg  # noqa: PLC0415
        from saas_test_support import _connect  # noqa: PLC0415

        conn = _connect()
        try:
            if allow:
                conn.execute("select set_config('saas.allow_dev_data', 'on', false)")
            cursor = psycopg.ClientCursor(conn)
            cursor.execute(path.read_text(encoding="utf-8"))
        finally:
            conn.rollback()
            conn.close()

    def test_there_are_scripts_to_guard(self):
        self.assertTrue(any("local" in p.parts for p in self.GUARDED))
        self.assertTrue(any("seeds" in p.parts for p in self.GUARDED))

    def test_every_dev_script_refuses_without_the_flag(self):
        for path in self.GUARDED:
            with self.subTest(script=path.name):
                with self.assertRaises(_errors().RaiseException) as raised:
                    self._run(path, allow=False)
                self.assertIn("saas.allow_dev_data", str(raised.exception))

    def test_a_non_on_value_does_not_enable_dev_scripts(self):
        import psycopg  # noqa: PLC0415
        from saas_test_support import _connect  # noqa: PLC0415

        conn = _connect()
        try:
            conn.execute("select set_config('saas.allow_dev_data', 'true', false)")
            with self.assertRaises(_errors().RaiseException):
                psycopg.ClientCursor(conn).execute(self.GUARDED[0].read_text(encoding="utf-8"))
        finally:
            conn.rollback()
            conn.close()

    def test_local_login_scripts_run_when_the_runner_enables_them(self):
        for path in (p for p in self.GUARDED if "local" in p.parts):
            with self.subTest(script=path.name):
                self._run(path, allow=True)

    def test_every_dev_script_contains_the_guard(self):
        for path in self.GUARDED:
            with self.subTest(script=path.name):
                self.assertIn("saas.allow_dev_data", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
