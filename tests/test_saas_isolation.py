"""Phase 1 tenant isolation tests.

Covers the eleven required assertions for the two seeded tenants:

    Tenant 001  Cindy Bakes
    Tenant 002  Test Bakery

Run:

    python -m unittest discover -s tests -p "test_*.py" -v
    python -m unittest tests.test_saas_isolation -v

The suite skips automatically when no PostgreSQL test database is configured, so the
existing unittest discovery run stays green on machines without a database.
"""

from __future__ import annotations

import json
import unittest
import urllib.parse

try:  # `tests` is a package when run as `python -m unittest tests.test_...`
    from .saas_test_support import (
        OUTSIDER_USER_ID,
        INVITED_USER_ID,
        PLAN_ID,
        SECONDARY_OWNER_USER_ID,
        TENANT_ONE_CHANNEL_ID,
        TENANT_ONE_ID,
        TENANT_ONE_NAME,
        TENANT_ONE_OWNER_USER_ID,
        TENANT_ONE_PHONE_NUMBER_ID,
        TENANT_ONE_STAFF_USER_ID,
        TENANT_SCOPED_TABLES,
        TENANT_TWO_ID,
        TENANT_TWO_NAME,
        TENANT_TWO_OWNER_USER_ID,
        TENANT_TWO_PHONE_NUMBER_ID,
        SaasDatabaseTestCase,
        test_database_url,
    )
except ImportError:  # ...and a plain directory under `discover -s tests`
    from saas_test_support import (  # type: ignore[no-redef]
        OUTSIDER_USER_ID,
        INVITED_USER_ID,
        PLAN_ID,
        SECONDARY_OWNER_USER_ID,
        TENANT_ONE_CHANNEL_ID,
        TENANT_ONE_ID,
        TENANT_ONE_NAME,
        TENANT_ONE_OWNER_USER_ID,
        TENANT_ONE_PHONE_NUMBER_ID,
        TENANT_ONE_STAFF_USER_ID,
        TENANT_SCOPED_TABLES,
        TENANT_TWO_ID,
        TENANT_TWO_NAME,
        TENANT_TWO_OWNER_USER_ID,
        TENANT_TWO_PHONE_NUMBER_ID,
        SaasDatabaseTestCase,
        test_database_url,
    )

try:  # optional dependency: the whole suite is skipped without it
    import psycopg
except ModuleNotFoundError:  # pragma: no cover
    psycopg = None


def _denied_error() -> type[BaseException]:
    """The error a denied statement raises.

    SQLSTATE 42501 (insufficient_privilege) covers both a missing GRANT and an RLS policy
    violation, which is why the security assertions below check for it specifically
    instead of for "any exception".
    """
    if psycopg is not None:
        return psycopg.errors.InsufficientPrivilege
    return Exception


def _app_backend_url() -> str | None:
    """Test URL rewritten to authenticate as the real application role.

    Exercises the production access path end to end: connect as app_backend (a non-owner
    role that is subject to RLS) and scope the transaction with set_config. Requires
    db/local/0001_local_app_backend_login.sql, which is development/CI only.
    """
    base = test_database_url()
    if not base:
        return None

    parts = urllib.parse.urlsplit(base)
    if not parts.hostname:
        return None

    netloc = f"app_backend:dev_app_backend_password@{parts.hostname}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"

    return urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


class TenantIsolationTests(SaasDatabaseTestCase):
    """The required isolation assertions (requirements 1-8)."""

    # -- 1 & 2: each tenant reads its own records ------------------------------
    def test_1_tenant_001_can_read_its_own_records(self):
        expected_own_rows = {
            "tenants": 1,
            "tenant_channels": 1,
            "tenant_users": 2,  # owner + staff
            "tenant_settings": 1,
            "subscriptions": 1,
            "audit_log": 1,  # tenant row only; the platform row must not appear
        }
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            cursor.execute("select name, slug from public.tenants")
            self.assertEqual(cursor.fetchall(), [(TENANT_ONE_NAME, "cindy-bakes")])

            for table, tenant_column in TENANT_SCOPED_TABLES:
                with self.subTest(table=table):
                    self.assertEqual(
                        self.count_rows(cursor, table, tenant_column, TENANT_ONE_ID),
                        expected_own_rows[table],
                    )

    def test_2_tenant_002_can_read_its_own_records(self):
        expected_own_rows = {
            "tenants": 1,
            "tenant_channels": 1,
            "tenant_users": 1,  # owner only
            "tenant_settings": 1,
            "subscriptions": 1,
            "audit_log": 0,  # none seeded for tenant 002; tenant 001's row stays invisible
        }
        with self.user(TENANT_TWO_OWNER_USER_ID) as cursor:
            cursor.execute("select name from public.tenants")
            self.assertEqual(cursor.fetchall(), [(TENANT_TWO_NAME,)])

            for table, tenant_column in TENANT_SCOPED_TABLES:
                with self.subTest(table=table):
                    self.assertEqual(
                        self.count_rows(cursor, table, tenant_column, TENANT_TWO_ID),
                        expected_own_rows[table],
                    )

    # -- 3 & 4: neither tenant reads the other ---------------------------------
    def test_3_tenant_001_cannot_read_tenant_002_records(self):
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            for table, tenant_column in TENANT_SCOPED_TABLES:
                with self.subTest(table=table):
                    self.assertEqual(
                        self.count_rows(cursor, table, tenant_column, TENANT_TWO_ID), 0
                    )

    def test_4_tenant_002_cannot_read_tenant_001_records(self):
        with self.user(TENANT_TWO_OWNER_USER_ID) as cursor:
            for table, tenant_column in TENANT_SCOPED_TABLES:
                with self.subTest(table=table):
                    self.assertEqual(
                        self.count_rows(cursor, table, tenant_column, TENANT_ONE_ID), 0
                    )

    # -- 5: cross-tenant UPDATE affects nothing --------------------------------
    def test_5_tenant_001_cannot_update_tenant_002_records(self):
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            cursor.execute(
                "update public.tenant_channels set display_phone_number = %s "
                "where tenant_id = %s",
                ("+254799999999", TENANT_TWO_ID),
            )
            self.assertEqual(cursor.rowcount, 0)

            cursor.execute(
                "update public.tenants set name = 'Hijacked' where id = %s", (TENANT_TWO_ID,)
            )
            self.assertEqual(cursor.rowcount, 0)

        # Read back over an RLS-bypassing connection: the data is genuinely untouched.
        with self.superuser() as cursor:
            cursor.execute("select name from public.tenants where id = %s", (TENANT_TWO_ID,))
            self.assertEqual(cursor.fetchone()[0], TENANT_TWO_NAME)

    # -- 6: cross-tenant DELETE affects nothing --------------------------------
    def test_6_tenant_001_cannot_delete_tenant_002_records(self):
        # Granted but policy-filtered: the statement succeeds and deletes zero rows.
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            cursor.execute(
                "delete from public.tenant_channels where tenant_id = %s", (TENANT_TWO_ID,)
            )
            self.assertEqual(cursor.rowcount, 0)

        # `tenants` has neither a DELETE grant nor a DELETE policy, so this raises.
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            with self.assertRaises(_denied_error()):
                cursor.execute("delete from public.tenants where id = %s", (TENANT_TWO_ID,))

        with self.superuser() as cursor:
            cursor.execute("select name from public.tenants where id = %s", (TENANT_TWO_ID,))
            self.assertEqual(cursor.fetchone()[0], TENANT_TWO_NAME)
            cursor.execute(
                "select count(*) from public.tenant_channels where tenant_id = %s",
                (TENANT_TWO_ID,),
            )
            self.assertEqual(cursor.fetchone()[0], 1)

    # -- 7: a tenant cannot insert a row claiming another tenant ---------------
    def test_7_tenant_001_cannot_insert_a_row_claiming_tenant_002(self):
        """WITH CHECK is what stops this: a read-only policy would not."""
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            with self.assertRaises(_denied_error()):
                cursor.execute(
                    "insert into public.tenant_channels "
                    "(tenant_id, channel_type, phone_number_id) "
                    "values (%s, 'whatsapp', %s)",
                    (TENANT_TWO_ID, "dev-phone-id-hijack-attempt"),
                )

        # The same must hold for the backend when its context is a different tenant.
        with self.backend(TENANT_ONE_ID) as cursor:
            with self.assertRaises(_denied_error()):
                cursor.execute(
                    "insert into public.tenant_settings (tenant_id) values (%s)",
                    (TENANT_TWO_ID,),
                )

        # Nothing was created for tenant 002.
        with self.superuser() as cursor:
            cursor.execute(
                "select count(*) from public.tenant_channels where phone_number_id = %s",
                ("dev-phone-id-hijack-attempt",),
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    # -- 8: no tenant context exposes nothing ----------------------------------
    def test_8_query_without_tenant_context_exposes_no_tenant_rows(self):
        with self.backend(None) as cursor:
            # Sanity check that the context really is absent...
            cursor.execute("select app.current_tenant_id()")
            self.assertIsNone(cursor.fetchone()[0])

            # ...and that every tenant table therefore returns zero rows.
            for table, _tenant_column in TENANT_SCOPED_TABLES:
                with self.subTest(table=table, role="app_backend"):
                    cursor.execute(f"select count(*) from public.{table}")
                    self.assertEqual(cursor.fetchone()[0], 0)

        # An unauthenticated dashboard session (no JWT claims) is equally blind.
        with self.user(None) as cursor:
            for table, _tenant_column in TENANT_SCOPED_TABLES:
                with self.subTest(table=table, role="authenticated"):
                    cursor.execute(f"select count(*) from public.{table}")
                    self.assertEqual(cursor.fetchone()[0], 0)

    def test_8b_backend_cannot_write_without_tenant_context(self):
        with self.backend(None) as cursor:
            with self.assertRaises(_denied_error()):
                cursor.execute(
                    "insert into public.tenant_settings (tenant_id) values (%s)",
                    (TENANT_ONE_ID,),
                )

    # -- role behaviour (owner vs staff) and outsiders -------------------------
    def test_staff_member_can_read_but_not_write(self):
        """MVP roles are owner and staff: staff work the business, owners change it."""
        with self.user(TENANT_ONE_STAFF_USER_ID) as cursor:
            self.assertEqual(
                self.count_rows(cursor, "tenant_channels", "tenant_id", TENANT_ONE_ID), 1
            )

            # A member, but not an owner, so the UPDATE policy does not match.
            cursor.execute(
                "update public.tenant_channels set display_phone_number = %s "
                "where tenant_id = %s",
                ("+254700000009", TENANT_ONE_ID),
            )
            self.assertEqual(cursor.rowcount, 0)

    def test_invited_member_cannot_read_tenant_data(self):
        with self.superuser() as cursor:
            cursor.execute(
                "insert into public.tenant_users (tenant_id, user_id, role, status) "
                "values (%s, %s, 'staff', 'invited')",
                (TENANT_ONE_ID, INVITED_USER_ID),
            )
            cursor.execute("set local role authenticated")
            cursor.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": INVITED_USER_ID}),),
            )
            cursor.execute("select app.is_tenant_member(%s)", (TENANT_ONE_ID,))
            self.assertFalse(cursor.fetchone()[0])
            for table in ("tenants", "tenant_channels", "tenant_settings", "subscriptions"):
                with self.subTest(table=table):
                    cursor.execute(f"select count(*) from public.{table}")
                    self.assertEqual(cursor.fetchone()[0], 0)

            cursor.execute("select status from public.tenant_users")
            self.assertEqual(cursor.fetchall(), [("invited",)])

    def test_owner_cannot_move_a_row_to_another_tenant(self):
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            with self.assertRaises(_denied_error()):
                cursor.execute(
                    "update public.tenant_channels set tenant_id = %s where id = %s",
                    (TENANT_TWO_ID, TENANT_ONE_CHANNEL_ID),
                )

    def test_outsider_sees_nothing_at_all(self):
        with self.user(OUTSIDER_USER_ID) as cursor:
            for table, _tenant_column in TENANT_SCOPED_TABLES:
                with self.subTest(table=table):
                    cursor.execute(f"select count(*) from public.{table}")
                    self.assertEqual(cursor.fetchone()[0], 0)

    def test_backend_context_confines_the_backend_to_one_tenant(self):
        with self.backend(TENANT_TWO_ID) as cursor:
            cursor.execute("select name from public.tenants")
            self.assertEqual(cursor.fetchall(), [(TENANT_TWO_NAME,)])
            self.assertEqual(
                self.count_rows(cursor, "tenants", "id", TENANT_ONE_ID), 0
            )

class RoutingTests(SaasDatabaseTestCase):
    """The WhatsApp phone_number_id -> tenant_id routing abstraction."""

    def test_routing_resolves_each_tenants_own_number(self):
        from saas import routing

        self.assertEqual(
            routing.resolve_tenant_id_by_phone_number_id(
                TENANT_ONE_PHONE_NUMBER_ID, _app_backend_url()
            ),
            TENANT_ONE_ID,
        )
        self.assertEqual(
            routing.resolve_tenant_id_by_phone_number_id(
                TENANT_TWO_PHONE_NUMBER_ID, _app_backend_url()
            ),
            TENANT_TWO_ID,
        )

    def test_routing_returns_none_for_unknown_numbers(self):
        from saas import routing

        self.assertIsNone(routing.resolve_tenant_id_by_phone_number_id(None))
        self.assertIsNone(routing.resolve_tenant_id_by_phone_number_id(""))
        self.assertIsNone(
            routing.resolve_tenant_id_by_phone_number_id(
                "dev-phone-id-not-onboarded-999", _app_backend_url()
            )
        )

    def test_routing_returns_none_for_a_disabled_channel(self):
        from saas import routing

        with self.superuser() as cursor:
            cursor.execute(
                "update public.tenant_channels set status = 'disabled' where id = %s",
                (TENANT_ONE_CHANNEL_ID,),
            )
            cursor.execute(
                "select app.resolve_tenant_by_phone_number_id(%s)", (TENANT_ONE_PHONE_NUMBER_ID,)
            )
            self.assertIsNone(cursor.fetchone()[0])

    def test_routing_is_identity_not_authorisation(self):
        """Routing answers "whose number is this?", not "may we serve them?".

        A suspended tenant's active channel still resolves, because public.tenants is
        FORCE-RLS'd and the resolver deliberately cannot read it. Tenant status is checked
        afterwards, inside tenant context, where the tenant row is legitimately readable.
        """
        from saas import routing

        with self.superuser() as cursor:
            cursor.execute(
                "update public.tenants set status = 'suspended' where id = %s", (TENANT_TWO_ID,)
            )
            self.assertEqual(
                routing.resolve_tenant_id_by_phone_number_id(
                    TENANT_TWO_PHONE_NUMBER_ID, _app_backend_url()
                ),
                TENANT_TWO_ID,
            )

        # ...and in context the backend can read the status and decide what to do.
        with self.backend(TENANT_TWO_ID) as cursor:
            cursor.execute("select status from public.tenants")
            self.assertEqual(cursor.fetchone()[0], "active")

    def test_routing_function_is_not_available_to_dashboard_sessions(self):
        """EXECUTE is granted to app_backend only: a browser never resolves a tenant."""
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            with self.assertRaises(_denied_error()):
                cursor.execute(
                    "select app.resolve_tenant_by_phone_number_id(%s)",
                    (TENANT_TWO_PHONE_NUMBER_ID,),
                )


class ConstraintTests(SaasDatabaseTestCase):
    """Database-enforced guarantees the platform relies on."""

    def test_phone_number_id_is_globally_unique(self):
        with self.superuser() as cursor:
            with self.assertRaises(Exception):
                cursor.execute(
                    "insert into public.tenant_channels (tenant_id, phone_number_id) "
                    "values (%s, %s)",
                    (TENANT_TWO_ID, TENANT_ONE_PHONE_NUMBER_ID),
                )

    def test_one_live_subscription_per_tenant(self):
        with self.superuser() as cursor:
            with self.assertRaises(Exception):
                cursor.execute(
                    "insert into public.subscriptions "
                    "(tenant_id, plan_id, status, billing_method) "
                    "values (%s, %s, 'active', 'manual')",
                    (TENANT_ONE_ID, PLAN_ID),
                )

    def test_cancelled_subscription_does_not_block_a_new_one(self):
        """Terminal states are excluded from the partial unique index, so history is kept."""
        with self.superuser() as cursor:
            cursor.execute(
                "update public.subscriptions set status = 'cancelled' where tenant_id = %s",
                (TENANT_ONE_ID,),
            )
            cursor.execute(
                "insert into public.subscriptions "
                "(tenant_id, plan_id, status, billing_method) "
                "values (%s, %s, 'trial', 'manual')",
                (TENANT_ONE_ID, PLAN_ID),
            )
            self.assertEqual(cursor.rowcount, 1)

    def test_multiple_active_owners_can_be_reduced_to_one(self):
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            cursor.execute(
                "insert into public.tenant_users (tenant_id, user_id, role, status) "
                "values (%s, %s, 'owner', 'active')",
                (TENANT_ONE_ID, SECONDARY_OWNER_USER_ID),
            )
            cursor.execute(
                "delete from public.tenant_users where tenant_id = %s and user_id = %s",
                (TENANT_ONE_ID, SECONDARY_OWNER_USER_ID),
            )
            self.assertEqual(cursor.rowcount, 1)
            cursor.execute(
                "select count(*) from public.tenant_users "
                "where tenant_id = %s and role = 'owner' and status = 'active'",
                (TENANT_ONE_ID,),
            )
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_final_active_owner_cannot_be_removed_or_demoted(self):
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            with self.assertRaises(Exception) as caught:
                cursor.execute(
                    "delete from public.tenant_users where tenant_id = %s and user_id = %s",
                    (TENANT_ONE_ID, TENANT_ONE_OWNER_USER_ID),
                )
            self.assertEqual(getattr(caught.exception, "sqlstate", None), "23514")

        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            with self.assertRaises(Exception) as caught:
                cursor.execute(
                    "update public.tenant_users set status = 'disabled' "
                    "where tenant_id = %s and user_id = %s",
                    (TENANT_ONE_ID, TENANT_ONE_OWNER_USER_ID),
                )
            self.assertEqual(getattr(caught.exception, "sqlstate", None), "23514")

    def test_inactive_plan_is_hidden_from_users_but_resolvable_by_backend(self):
        with self.superuser() as cursor:
            cursor.execute("update public.plans set is_active = false where id = %s", (PLAN_ID,))
            cursor.execute("set local role authenticated")
            cursor.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": TENANT_ONE_OWNER_USER_ID}),),
            )
            cursor.execute("select id from public.plans where id = %s", (PLAN_ID,))
            self.assertEqual(cursor.fetchall(), [])

            cursor.execute("reset role")
            cursor.execute("set local role app_backend")
            cursor.execute("select set_config('app.tenant_id', %s, true)", (TENANT_ONE_ID,))
            cursor.execute(
                "select p.code from public.subscriptions s "
                "join public.plans p on p.id = s.plan_id where s.tenant_id = %s",
                (TENANT_ONE_ID,),
            )
            self.assertEqual(cursor.fetchall(), [("starter",)])

    def test_platform_provisioning_creates_tenant_owner_settings_and_audit_atomically(self):
        owner_id = "dddddddd-0000-4000-8000-000000000001"
        with self.superuser() as cursor:
            cursor.execute("set local role saas_platform_admin")
            cursor.execute(
                "select app.provision_tenant(%s, %s, %s, %s, %s, %s)",
                ("Provisioned Test", "provisioned-test", owner_id, "Africa/Nairobi", "KES", PLAN_ID),
            )
            tenant_id = cursor.fetchone()[0]

            cursor.execute("reset role")
            cursor.execute("select name from public.tenants where id = %s", (tenant_id,))
            self.assertEqual(cursor.fetchone(), ("Provisioned Test",))
            cursor.execute(
                "select role, status from public.tenant_users "
                "where tenant_id = %s and user_id = %s",
                (tenant_id, owner_id),
            )
            self.assertEqual(cursor.fetchone(), ("owner", "active"))
            cursor.execute("select currency from public.tenant_settings where tenant_id = %s", (tenant_id,))
            self.assertEqual(cursor.fetchone(), ("KES",))
            cursor.execute(
                "select action from public.audit_log "
                "where tenant_id = %s and action = 'tenant.provisioned'",
                (tenant_id,),
            )
            self.assertEqual(cursor.fetchone(), ("tenant.provisioned",))
            cursor.execute(
                "select count(*) from public.subscriptions "
                "where tenant_id = %s and plan_id = %s",
                (tenant_id, PLAN_ID),
            )
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_platform_provisioning_rolls_back_everything_on_failure(self):
        owner_id = "dddddddd-0000-4000-8000-000000000002"
        with self.superuser() as cursor:
            cursor.execute("savepoint before_provisioning_failure")
            cursor.execute("set local role saas_platform_admin")
            with self.assertRaises(Exception):
                cursor.execute(
                    "select app.provision_tenant(%s, %s, %s, %s, %s, %s)",
                    (
                        "Atomic Failure",
                        "atomic-failure",
                        owner_id,
                        "Africa/Nairobi",
                        "KES",
                        "ffffffff-ffff-4fff-8fff-ffffffffffff",
                    ),
                )
            cursor.execute("rollback to savepoint before_provisioning_failure")
            cursor.execute("select count(*) from public.tenants where slug = 'atomic-failure'")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_tenant_role_cannot_call_platform_provisioning(self):
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            with self.assertRaises(_denied_error()):
                cursor.execute(
                    "select app.provision_tenant(%s, %s, %s)",
                    ("Client Tenant", "client-tenant", TENANT_ONE_OWNER_USER_ID),
                )

class AuditLogTests(SaasDatabaseTestCase):
    """Append-only audit trail, including platform-level (tenant-less) events."""

    def test_backend_can_append_within_its_tenant_context(self):
        with self.backend(TENANT_ONE_ID) as cursor:
            cursor.execute(
                "insert into public.audit_log (tenant_id, actor_type, action) "
                "values (%s, 'system', 'test.append')",
                (TENANT_ONE_ID,),
            )
            self.assertEqual(cursor.rowcount, 1)

    def test_backend_cannot_append_a_platform_level_row(self):
        """Platform events (tenant_id NULL) are platform-owned; a tenant context cannot create one."""
        with self.backend(TENANT_ONE_ID) as cursor:
            with self.assertRaises(_denied_error()):
                cursor.execute(
                    "insert into public.audit_log (tenant_id, actor_type, action) "
                    "values (null, 'platform', 'test.platform')"
                )

    def test_audit_log_cannot_be_updated_or_deleted_by_application_roles(self):
        with self.backend(TENANT_ONE_ID) as cursor:
            with self.assertRaises(_denied_error()):
                cursor.execute(
                    "update public.audit_log set action = 'tampered' where tenant_id = %s",
                    (TENANT_ONE_ID,),
                )

        with self.backend(TENANT_ONE_ID) as cursor:
            with self.assertRaises(_denied_error()):
                cursor.execute(
                    "delete from public.audit_log where tenant_id = %s", (TENANT_ONE_ID,)
                )

        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            with self.assertRaises(_denied_error()):
                cursor.execute(
                    "update public.audit_log set action = 'tampered' where tenant_id = %s",
                    (TENANT_ONE_ID,),
                )

    def test_platform_level_audit_rows_are_invisible_to_tenants(self):
        with self.user(TENANT_ONE_OWNER_USER_ID) as cursor:
            # The owner sees exactly one audit row: its own tenant's.
            cursor.execute("select count(*) from public.audit_log")
            self.assertEqual(cursor.fetchone()[0], 1)

            cursor.execute("select count(*) from public.audit_log where tenant_id is null")
            self.assertEqual(cursor.fetchone()[0], 0)

        # The platform row does exist (proved over an RLS-bypassing connection).
        with self.superuser() as cursor:
            cursor.execute("select count(*) from public.audit_log where tenant_id is null")
            self.assertGreaterEqual(cursor.fetchone()[0], 1)


class TenantContextHelperTests(SaasDatabaseTestCase):
    """The tenant-context implementation in saas/tenant_context.py."""

    def test_tenant_context_is_transaction_local(self):
        """set_config(..., true) must not outlive its transaction.

        This is the property that stops a pooled connection handing one tenant's context
        to the next request: a session-level setting would survive, this must not.
        """
        with self.superuser() as cursor:
            cursor.execute("select set_config('app.tenant_id', %s, true)", (TENANT_ONE_ID,))
            cursor.execute("select app.current_tenant_id()")
            self.assertEqual(str(cursor.fetchone()[0]), TENANT_ONE_ID)

        # The helper rolled that transaction back, so a fresh connection has no context.
        with self.superuser() as cursor:
            cursor.execute("select app.current_tenant_id()")
            self.assertIsNone(cursor.fetchone()[0])

    def test_tenant_context_helper_refuses_an_empty_tenant_id(self):
        from saas import tenant_context

        with self.assertRaises(ValueError):
            with tenant_context.tenant_transaction("", None):
                pass  # pragma: no cover - the context manager raises on entry

    def test_tenant_context_helper_end_to_end_as_the_application_role(self):
        """Connect as app_backend and query: the real production access path."""
        from saas import tenant_context

        url = _app_backend_url()
        if not url:
            self.skipTest("could not derive an app_backend URL from the test URL")

        try:
            with tenant_context.tenant_transaction(TENANT_ONE_ID, url) as cursor:
                self.assertEqual(tenant_context.current_tenant_id(cursor), TENANT_ONE_ID)
                cursor.execute("select name from public.tenants")
                self.assertEqual(cursor.fetchall(), [(TENANT_ONE_NAME,)])
        except Exception as error:  # pragma: no cover - environment dependent
            if "password" in str(error).lower() or "authentication" in str(error).lower():
                self.skipTest(
                    "app_backend cannot log in; apply "
                    "db/local/0001_local_app_backend_login.sql first"
                )
            raise

    def test_database_connection_rejects_superuser_and_accepts_app_backend(self):
        from saas import db

        with self.assertRaisesRegex(RuntimeError, "expected 'app_backend'"):
            db.connect(test_database_url())

        url = _app_backend_url()
        self.assertIsNotNone(url)
        conn = db.connect(url)
        try:
            with conn.cursor() as cursor:
                cursor.execute("select current_user")
                self.assertEqual(cursor.fetchone()[0], "app_backend")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()




