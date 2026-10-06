"""Database tests for platform administration (migrations 0019-0022).

Covers: platform-admin authorization, the platform provisioner, the draft tenant state,
activation readiness, the resolver status check, tenant isolation of the new surfaces and
the credentials_ref scope constraint. Each test rolls back, so seeded data is untouched.
"""

from __future__ import annotations

import unittest

from saas_test_support import (
    PLATFORM_ADMIN_USER_ID,
    PLAN_ID,
    TENANT_ONE_ID,
    TENANT_ONE_OWNER_USER_ID,
    TENANT_ONE_PHONE_NUMBER_ID,
    TENANT_TWO_ID,
    SaasDatabaseTestCase,
    as_platform,
)


def _errors():
    import psycopg.errors as errors  # noqa: PLC0415

    return errors


def _provision(cursor, name="Draft Co", slug="draft-co", **kwargs):
    cursor.execute(
        "select app.platform_provision_tenant(%s, %s, %s, %s, %s, %s)",
        (
            name,
            slug,
            kwargs.get("timezone", "Africa/Nairobi"),
            kwargs.get("currency", "KES"),
            kwargs.get("owner"),
            kwargs.get("plan"),
        ),
    )
    return str(cursor.fetchone()[0])


def _add_catalogue(cursor, tenant_id):
    """Insert one active product with one active variant as the table owner."""
    cursor.execute("reset role")
    cursor.execute(
        "insert into public.products (tenant_id, name) values (%s, 'Item') returning id",
        (tenant_id,),
    )
    product_id = cursor.fetchone()[0]
    cursor.execute(
        "insert into public.product_variants (tenant_id, product_id, code, name, price) "
        "values (%s, %s, 'std', 'Standard', 10)",
        (tenant_id, product_id),
    )
    cursor.execute("set local role saas_platform_admin")


PLATFORM_FUNCTIONS = (
    "app.platform_provision_tenant(text, text, text, text, uuid, uuid)",
    "app.platform_tenant_readiness(uuid)",
    "app.platform_set_tenant_status(uuid, text, boolean)",
    "app.platform_list_tenants(uuid, text)",
    "app.platform_list_plans()",
    "app.platform_overview()",
    "app.platform_recent_audit(uuid, integer)",
    "app.is_platform_admin()",
    "app.require_platform_admin()",
)


class PlatformAuthorizationTests(SaasDatabaseTestCase):
    def test_registered_administrator_is_recognised(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            cursor.execute("select app.is_platform_admin()")
            self.assertTrue(cursor.fetchone()[0])

    def test_unregistered_identity_is_not_an_administrator(self):
        with as_platform(TENANT_ONE_OWNER_USER_ID, register=False) as cursor:
            cursor.execute("select app.is_platform_admin()")
            self.assertFalse(cursor.fetchone()[0])

    def test_tenant_owner_is_not_a_platform_administrator(self):
        with as_platform(TENANT_ONE_OWNER_USER_ID, register=False) as cursor:
            with self.assertRaises(_errors().InsufficientPrivilege):
                _provision(cursor)

    def test_missing_identity_cannot_provision(self):
        with as_platform(None) as cursor:
            with self.assertRaises(_errors().InsufficientPrivilege):
                _provision(cursor)

    def test_disabled_administrator_is_denied(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            cursor.execute("reset role")
            cursor.execute(
                "update public.platform_admins set status = 'disabled' where user_id = %s",
                (PLATFORM_ADMIN_USER_ID,),
            )
            cursor.execute("set local role saas_platform_admin")
            cursor.execute("select app.is_platform_admin()")
            self.assertFalse(cursor.fetchone()[0])
            with self.assertRaises(_errors().InsufficientPrivilege):
                _provision(cursor)

    def test_every_platform_function_rejects_a_non_administrator(self):
        calls = {
            "provision": "select app.platform_provision_tenant('X Co', 'x-co')",
            "readiness": f"select app.platform_tenant_readiness('{TENANT_ONE_ID}')",
            "status": f"select app.platform_set_tenant_status('{TENANT_ONE_ID}', 'suspended')",
            "list": "select * from app.platform_list_tenants()",
            "plans": "select * from app.platform_list_plans()",
            "overview": "select app.platform_overview()",
            "audit": "select * from app.platform_recent_audit()",
        }
        for label, sql in calls.items():
            with self.subTest(function=label):
                with as_platform(TENANT_ONE_OWNER_USER_ID, register=False) as cursor:
                    with self.assertRaises(_errors().InsufficientPrivilege):
                        cursor.execute(sql)

    def test_tenant_roles_cannot_execute_platform_functions(self):
        for signature in PLATFORM_FUNCTIONS:
            for role in ("app_backend", "authenticated", "anon", "public"):
                with self.subTest(function=signature, role=role):
                    with self.superuser() as cursor:
                        cursor.execute(
                            "select has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
                            (role, signature),
                        )
                        self.assertFalse(cursor.fetchone()[0])

    def test_platform_admins_table_is_closed_to_application_roles(self):
        for role in ("app_backend", "authenticated", "anon", "saas_platform_admin"):
            with self.subTest(role=role):
                with self.superuser() as cursor:
                    cursor.execute(
                        "select has_table_privilege(%s, 'public.platform_admins', "
                        "'SELECT,INSERT,UPDATE,DELETE')",
                        (role,),
                    )
                    self.assertFalse(cursor.fetchone()[0])

    def test_platform_admins_has_rls_enabled(self):
        with self.superuser() as cursor:
            cursor.execute(
                "select relrowsecurity from pg_class where oid = 'public.platform_admins'::regclass"
            )
            self.assertTrue(cursor.fetchone()[0])


class ProvisioningTests(SaasDatabaseTestCase):
    def test_creates_a_draft_tenant_with_settings_and_an_audit_event(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = _provision(cursor, "Second Co", "second-co", currency="USD")
            cursor.execute("reset role")
            cursor.execute("select name, slug, status from public.tenants where id = %s", (tenant_id,))
            self.assertEqual(cursor.fetchone(), ("Second Co", "second-co", "draft"))
            cursor.execute(
                "select timezone, currency from public.tenant_settings where tenant_id = %s",
                (tenant_id,),
            )
            self.assertEqual(cursor.fetchone(), ("Africa/Nairobi", "USD"))
            cursor.execute(
                "select actor_type, actor_user_id::text, action from public.audit_log "
                "where tenant_id = %s",
                (tenant_id,),
            )
            self.assertEqual(
                cursor.fetchall(),
                [("platform", PLATFORM_ADMIN_USER_ID, "tenant.provisioned")],
            )

    def test_owner_and_subscription_are_optional(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = _provision(cursor)
            cursor.execute("reset role")
            cursor.execute("select count(*) from public.tenant_users where tenant_id = %s", (tenant_id,))
            self.assertEqual(cursor.fetchone()[0], 0)
            cursor.execute("select count(*) from public.subscriptions where tenant_id = %s", (tenant_id,))
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_owner_and_subscription_are_created_when_supplied(self):
        owner = "dddddddd-0000-4000-8000-000000000001"
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = _provision(cursor, owner=owner, plan=PLAN_ID)
            cursor.execute("reset role")
            cursor.execute(
                "select role, status from public.tenant_users where tenant_id = %s and user_id = %s",
                (tenant_id, owner),
            )
            self.assertEqual(cursor.fetchone(), ("owner", "active"))
            cursor.execute(
                "select status from public.subscriptions where tenant_id = %s and plan_id = %s",
                (tenant_id, PLAN_ID),
            )
            self.assertEqual(cursor.fetchone(), ("trial",))

    def test_unknown_plan_rolls_everything_back(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            cursor.execute("savepoint before_failure")
            with self.assertRaises(_errors().ForeignKeyViolation):
                _provision(cursor, "Rollback Co", "rollback-co",
                           plan="ffffffff-ffff-4fff-8fff-ffffffffffff")
            cursor.execute("rollback to savepoint before_failure")
            cursor.execute("reset role")
            cursor.execute("select count(*) from public.tenants where slug = 'rollback-co'")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_duplicate_slug_is_rejected(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            _provision(cursor, "One", "dup-slug")
            cursor.execute("savepoint s")
            with self.assertRaises(_errors().UniqueViolation):
                _provision(cursor, "Two", "dup-slug")

    def test_invalid_slug_is_rejected(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            with self.assertRaises(_errors().CheckViolation):
                _provision(cursor, "Bad", "Bad Slug!")

    def test_provisioning_writes_only_the_new_tenants_rows(self):
        tables = ("tenant_users", "tenant_settings", "tenant_channels", "products", "subscriptions")
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            cursor.execute("reset role")
            before = {}
            for table in tables:
                cursor.execute(f"select count(*) from public.{table} where tenant_id = any(%s::uuid[])",
                               ([TENANT_ONE_ID, TENANT_TWO_ID],))
                before[table] = cursor.fetchone()[0]
            cursor.execute("set local role saas_platform_admin")
            new_id = _provision(cursor, "Isolated Co", "isolated-co", owner=PLATFORM_ADMIN_USER_ID, plan=PLAN_ID)
            cursor.execute("reset role")
            for table in tables:
                cursor.execute(f"select count(*) from public.{table} where tenant_id = any(%s::uuid[])",
                               ([TENANT_ONE_ID, TENANT_TWO_ID],))
                self.assertEqual(cursor.fetchone()[0], before[table], table)
            cursor.execute("select count(*) from public.tenant_settings where tenant_id = %s", (new_id,))
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_the_unguarded_phase_1_provisioner_is_retired(self):
        with self.superuser() as cursor:
            cursor.execute(
                "select to_regprocedure('app.provision_tenant(text,text,uuid,text,text,uuid)')"
            )
            self.assertIsNone(cursor.fetchone()[0])

    def test_provisioning_functions_are_owned_by_the_nologin_provisioner(self):
        with self.superuser() as cursor:
            cursor.execute(
                "select pg_get_userbyid(proowner), prosecdef from pg_proc "
                "where oid = 'app.platform_provision_tenant(text,text,text,text,uuid,uuid)'::regprocedure"
            )
            self.assertEqual(cursor.fetchone(), ("saas_platform_provisioner", True))
            cursor.execute(
                "select rolcanlogin, rolbypassrls, rolsuper from pg_roles "
                "where rolname = 'saas_platform_provisioner'"
            )
            self.assertEqual(cursor.fetchone(), (False, False, False))


class DraftStateTests(SaasDatabaseTestCase):
    def _draft_with_channel(self, cursor, phone="draft-phone-001"):
        tenant_id = _provision(cursor, "Draft Co", "draft-co")
        cursor.execute("reset role")
        cursor.execute(
            "insert into public.tenant_channels (tenant_id, phone_number_id, status) "
            "values (%s, %s, 'pending')",
            (tenant_id, phone),
        )
        return tenant_id

    def test_new_tenants_default_to_draft(self):
        with self.superuser() as cursor:
            cursor.execute(
                "insert into public.tenants (name, slug) values ('Default Co', 'default-co') "
                "returning status"
            )
            self.assertEqual(cursor.fetchone()[0], "draft")

    def test_active_channel_is_refused_for_a_draft_tenant(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = self._draft_with_channel(cursor)
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute(
                    "update public.tenant_channels set status = 'active' where tenant_id = %s",
                    (tenant_id,),
                )

    def test_active_channel_cannot_be_inserted_for_a_draft_tenant(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = _provision(cursor)
            cursor.execute("reset role")
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute(
                    "insert into public.tenant_channels (tenant_id, phone_number_id, status) "
                    "values (%s, 'draft-phone-002', 'active')",
                    (tenant_id,),
                )

    def test_a_pending_channel_on_a_draft_tenant_does_not_route(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            self._draft_with_channel(cursor, "draft-phone-003")
            cursor.execute("set local role app_backend")
            cursor.execute("select app.resolve_tenant_by_phone_number_id('draft-phone-003')")
            self.assertIsNone(cursor.fetchone()[0])

    def test_active_tenants_still_route(self):
        with self.superuser() as cursor:
            cursor.execute("set local role app_backend")
            cursor.execute(
                "select app.resolve_tenant_by_phone_number_id(%s)", (TENANT_ONE_PHONE_NUMBER_ID,)
            )
            self.assertEqual(str(cursor.fetchone()[0]), TENANT_ONE_ID)

    def test_suspended_tenants_do_not_route(self):
        with self.superuser() as cursor:
            cursor.execute("update public.tenants set status = 'suspended' where id = %s", (TENANT_ONE_ID,))
            cursor.execute("set local role app_backend")
            cursor.execute(
                "select app.resolve_tenant_by_phone_number_id(%s)", (TENANT_ONE_PHONE_NUMBER_ID,)
            )
            self.assertIsNone(cursor.fetchone()[0])

    def test_a_tenant_cannot_return_to_draft(self):
        with self.superuser() as cursor:
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute("update public.tenants set status = 'draft' where id = %s", (TENANT_ONE_ID,))

    def test_resolver_is_owned_by_the_provisioner_and_not_open_to_dashboard_roles(self):
        with self.superuser() as cursor:
            cursor.execute(
                "select pg_get_userbyid(proowner) from pg_proc "
                "where oid = 'app.resolve_tenant_by_phone_number_id(text)'::regprocedure"
            )
            self.assertEqual(cursor.fetchone()[0], "saas_platform_provisioner")
            for role, expected in (("app_backend", True), ("authenticated", False), ("anon", False)):
                cursor.execute(
                    "select has_function_privilege(%s, "
                    "'app.resolve_tenant_by_phone_number_id(text)', 'EXECUTE')",
                    (role,),
                )
                self.assertEqual(cursor.fetchone()[0], expected, role)

    def test_backend_cannot_change_tenant_status(self):
        with self.backend(TENANT_ONE_ID) as cursor:
            with self.assertRaises(_errors().InsufficientPrivilege):
                cursor.execute("update public.tenants set status = 'suspended' where id = app.current_tenant_id()")

    def test_backend_cannot_change_tenant_slug(self):
        with self.backend(TENANT_ONE_ID) as cursor:
            with self.assertRaises(_errors().InsufficientPrivilege):
                cursor.execute("update public.tenants set slug = 'renamed-slug' where id = app.current_tenant_id()")

    def test_backend_can_still_rename_its_tenant(self):
        with self.backend(TENANT_ONE_ID) as cursor:
            cursor.execute("update public.tenants set name = 'Renamed' where id = app.current_tenant_id()")
            self.assertEqual(cursor.rowcount, 1)

    def test_a_tenant_owner_cannot_activate_their_own_draft_tenant(self):
        owner = "dddddddd-0000-4000-8000-000000000009"
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = _provision(cursor, owner=owner)
            cursor.execute("reset role")
            cursor.execute("set local role authenticated")
            cursor.execute(
                "select set_config('request.jwt.claims', %s, true)", ('{"sub": "%s"}' % owner,)
            )
            with self.assertRaises(_errors().InsufficientPrivilege):
                cursor.execute("update public.tenants set status = 'active' where id = %s", (tenant_id,))


class ActivationTests(SaasDatabaseTestCase):
    def test_readiness_reports_each_requirement(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = _provision(cursor)
            cursor.execute("select app.platform_tenant_readiness(%s)", (tenant_id,))
            checks = {c["key"]: c["ok"] for c in cursor.fetchone()[0]["checks"]}
        self.assertEqual(checks, {"settings": True, "catalogue": False, "owner": False, "channel": False})

    def test_activation_fails_until_the_catalogue_exists(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = _provision(cursor, owner=PLATFORM_ADMIN_USER_ID)
            with self.assertRaises(_errors().CheckViolation) as raised:
                cursor.execute("select app.platform_set_tenant_status(%s, 'active')", (tenant_id,))
            self.assertIn("catalogue", str(raised.exception))

    def test_activation_requires_an_owner_unless_waived(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = _provision(cursor)
            _add_catalogue(cursor, tenant_id)
            cursor.execute("savepoint s")
            with self.assertRaises(_errors().CheckViolation) as raised:
                cursor.execute("select app.platform_set_tenant_status(%s, 'active')", (tenant_id,))
            self.assertIn("owner", str(raised.exception))
            cursor.execute("rollback to savepoint s")
            cursor.execute("select app.platform_set_tenant_status(%s, 'active', true)", (tenant_id,))
            self.assertEqual(cursor.fetchone()[0], "active")

    def test_activation_succeeds_when_ready_and_is_audited(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = _provision(cursor, owner=PLATFORM_ADMIN_USER_ID)
            _add_catalogue(cursor, tenant_id)
            cursor.execute("select app.platform_set_tenant_status(%s, 'active')", (tenant_id,))
            self.assertEqual(cursor.fetchone()[0], "active")
            cursor.execute("reset role")
            cursor.execute("select status from public.tenants where id = %s", (tenant_id,))
            self.assertEqual(cursor.fetchone()[0], "active")
            cursor.execute(
                "select metadata ->> 'from', metadata ->> 'to', actor_user_id::text "
                "from public.audit_log where tenant_id = %s and action = 'tenant.status_changed'",
                (tenant_id,),
            )
            self.assertEqual(cursor.fetchone(), ("draft", "active", PLATFORM_ADMIN_USER_ID))

    def test_an_inactive_variant_does_not_make_a_tenant_ready(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = _provision(cursor, owner=PLATFORM_ADMIN_USER_ID)
            _add_catalogue(cursor, tenant_id)
            cursor.execute("reset role")
            cursor.execute("update public.product_variants set is_active = false where tenant_id = %s", (tenant_id,))
            cursor.execute("set local role saas_platform_admin")
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute("select app.platform_set_tenant_status(%s, 'active')", (tenant_id,))

    def test_an_unsupported_status_is_refused(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = _provision(cursor)
            with self.assertRaises(_errors().InvalidParameterValue):
                cursor.execute("select app.platform_set_tenant_status(%s, 'draft')", (tenant_id,))

    def test_unknown_tenant_is_not_found(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            with self.assertRaises(_errors().NoDataFound):
                cursor.execute(
                    "select app.platform_set_tenant_status('ffffffff-ffff-4fff-8fff-ffffffffffff', 'suspended')"
                )

    def test_channel_can_become_active_once_the_tenant_is_active(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            tenant_id = _provision(cursor, owner=PLATFORM_ADMIN_USER_ID)
            _add_catalogue(cursor, tenant_id)
            cursor.execute("select app.platform_set_tenant_status(%s, 'active')", (tenant_id,))
            cursor.execute("reset role")
            cursor.execute(
                "insert into public.tenant_channels (tenant_id, phone_number_id, status) "
                "values (%s, 'active-phone-001', 'active')",
                (tenant_id,),
            )
            cursor.execute("set local role app_backend")
            cursor.execute("select app.resolve_tenant_by_phone_number_id('active-phone-001')")
            self.assertEqual(str(cursor.fetchone()[0]), tenant_id)


class PlatformViewTests(SaasDatabaseTestCase):
    def test_list_tenants_shows_every_tenant_with_counts(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            cursor.execute("select id::text, product_count from app.platform_list_tenants()")
            rows = dict(cursor.fetchall())
        self.assertIn(TENANT_ONE_ID, rows)
        self.assertIn(TENANT_TWO_ID, rows)

    def test_list_tenants_filters_by_status_and_id(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            draft_id = _provision(cursor)
            cursor.execute("select id::text from app.platform_list_tenants(null, 'draft')")
            self.assertIn(draft_id, [r[0] for r in cursor.fetchall()])
            cursor.execute("select id::text from app.platform_list_tenants(%s, null)", (TENANT_ONE_ID,))
            self.assertEqual(cursor.fetchall(), [(TENANT_ONE_ID,)])

    def test_overview_summarises_the_platform(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            cursor.execute("select app.platform_overview()")
            data = cursor.fetchone()[0]
        self.assertGreaterEqual(data["tenants_total"], 2)
        self.assertIn("active", data["tenants_by_status"])

    def test_audit_view_is_capped_and_filterable(self):
        with as_platform(PLATFORM_ADMIN_USER_ID) as cursor:
            cursor.execute("select count(*) from app.platform_recent_audit(null, 100000)")
            self.assertLessEqual(cursor.fetchone()[0], 200)
            cursor.execute("select tenant_id::text from app.platform_recent_audit(%s, 50)", (TENANT_ONE_ID,))
            self.assertTrue(all(row[0] == TENANT_ONE_ID for row in cursor.fetchall()))

    def test_platform_role_still_has_no_table_privileges(self):
        with self.superuser() as cursor:
            cursor.execute(
                "select count(*) from information_schema.role_table_grants "
                "where table_schema = 'public' and grantee = 'saas_platform_admin'"
            )
            self.assertEqual(cursor.fetchone()[0], 0)


class TenantScopeTests(SaasDatabaseTestCase):
    def test_variant_cannot_reference_another_tenants_product(self):
        with self.superuser() as cursor:
            cursor.execute(
                "insert into public.products (tenant_id, name) values (%s, 'Other') returning id",
                (TENANT_TWO_ID,),
            )
            foreign_product = cursor.fetchone()[0]
            cursor.execute("set local role app_backend")
            cursor.execute("select set_config('app.tenant_id', %s, true)", (TENANT_ONE_ID,))
            with self.assertRaises((_errors().ForeignKeyViolation, _errors().InsufficientPrivilege)):
                cursor.execute(
                    "insert into public.product_variants (tenant_id, product_id, code, name, price) "
                    "values (app.current_tenant_id(), %s, 'x', 'X', 1)",
                    (foreign_product,),
                )

    def test_credentials_ref_must_belong_to_the_channels_tenant(self):
        with self.superuser() as cursor:
            cursor.execute("savepoint s")
            with self.assertRaises(_errors().CheckViolation):
                cursor.execute(
                    "insert into public.tenant_channels (tenant_id, phone_number_id, credentials_ref) "
                    "values (%s, 'cross-tenant-001', %s)",
                    (TENANT_ONE_ID, f"tenants/{TENANT_TWO_ID}/whatsapp"),
                )
            cursor.execute("rollback to savepoint s")
            cursor.execute(
                "insert into public.tenant_channels (tenant_id, phone_number_id, credentials_ref, is_default) "
                "values (%s, 'own-ref-001', %s, false)",
                (TENANT_ONE_ID, f"tenants/{TENANT_ONE_ID}/whatsapp"),
            )

    def test_credentials_ref_is_optional(self):
        with self.superuser() as cursor:
            cursor.execute(
                "insert into public.tenant_channels (tenant_id, phone_number_id, is_default) "
                "values (%s, 'no-ref-001', false)",
                (TENANT_ONE_ID,),
            )

    def test_phone_number_cannot_be_assigned_to_two_tenants(self):
        with self.superuser() as cursor:
            with self.assertRaises(_errors().UniqueViolation):
                cursor.execute(
                    "insert into public.tenant_channels (tenant_id, phone_number_id, is_default) "
                    "values (%s, %s, false)",
                    (TENANT_TWO_ID, TENANT_ONE_PHONE_NUMBER_ID),
                )

    def test_backend_in_one_tenant_cannot_see_the_other_tenants_channels(self):
        with self.backend(TENANT_ONE_ID) as cursor:
            cursor.execute("select distinct tenant_id::text from public.tenant_channels")
            self.assertEqual(cursor.fetchall(), [(TENANT_ONE_ID,)])


if __name__ == "__main__":
    unittest.main()
