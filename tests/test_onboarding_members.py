"""Tenant onboarding and member management, end to end against a disposable PostgreSQL.

The dashboard runs through Flask's test client as the real ``saas_platform_admin`` and
``app_backend`` logins. Supabase Auth is replaced by an in-memory fake, so no real account is
created and no invitation is ever sent. Every test removes what it created; the seeded
Cindy Bakes tenant (TENANT_ONE) is used as-is and its original rows are never modified.
"""

from __future__ import annotations

import unittest
import uuid

from platform_admin.app import create_app
from platform_admin.auth import AuthError, Authenticator, Identity
from platform_admin.auth_admin import AuthAdmin, AuthAdminError, AuthUser, normalize_email
from platform_admin.config import Settings
from saas_test_support import (
    PLATFORM_ADMIN_USER_ID,
    TENANT_ONE_ID,
    TENANT_ONE_OWNER_USER_ID,
    TENANT_ONE_SLUG,
    TENANT_ONE_STAFF_USER_ID,
    SaasDatabaseTestCase,
    _connect,
    app_backend_test_url,
    platform_admin_test_url,
)

SLUG_PREFIX = "onb-test-"
DOMAIN = "@onb-test.example"
PUBLIC = "https://admin.example.test"
CALLBACK = PUBLIC + "/t/auth/callback"
PASSWORD = "correct horse battery"


class FakeAuthAdmin(AuthAdmin):
    """In-memory Supabase Auth. Records every invitation instead of sending it."""

    def __init__(self):
        self.users: dict[str, AuthUser] = {}
        self.invites: list[tuple[str, str | None, str]] = []
        self.resets: list[tuple[str, str]] = []
        self.calls = 0
        self.fail_invite: AuthAdminError | None = None

    def add_existing(self, email: str, full_name: str | None = None) -> AuthUser:
        user = AuthUser(str(uuid.uuid4()), normalize_email(email), True, full_name=full_name)
        self.users[user.email] = user
        return user

    def invite(self, email, full_name, redirect_to):
        self.calls += 1
        if self.fail_invite is not None:
            raise self.fail_invite
        email = normalize_email(email)
        if email in self.users:
            if self.users[email].confirmed:
                raise AuthAdminError("An account with this email already exists.", "email_exists")
            self.invites.append((email, full_name, redirect_to))  # re-invite
            return self.users[email]
        user = AuthUser(str(uuid.uuid4()), email, False, full_name=full_name)
        self.users[email] = user
        self.invites.append((email, full_name, redirect_to))
        return user

    def get_user(self, user_id):
        self.calls += 1
        return next((u for u in self.users.values() if u.id == user_id), None)

    def find_user_by_email(self, email):
        self.calls += 1
        return self.users.get(normalize_email(email))

    def set_password(self, user_id, password):  # pragma: no cover - portal tests cover it
        raise AssertionError("not used")

    def send_password_reset(self, email, redirect_to):
        self.resets.append((email, redirect_to))


class FakeAuthenticator(Authenticator):
    def __init__(self):
        self.users: dict[str, tuple[str, str]] = {}

    def login(self, email, password):
        entry = self.users.get(email)
        if not entry or entry[0] != password:
            raise AuthError("Invalid email or password.")
        return Identity(entry[1], email)


def _settings(**overrides) -> Settings:
    values = dict(
        secret_key="k" * 40,
        cookie_secure=False,
        app_database_url=app_backend_test_url(),
        platform_database_url=platform_admin_test_url(),
        supabase_url="https://abcdefghijklmnopqrst.supabase.co",
        supabase_anon_key="anon-test-key",
        supabase_service_role_key="sb_secret_test_only",
        public_url=PUBLIC,
    )
    values.update(overrides)
    return Settings(**values)


def _rows(sql: str, params=()) -> list[tuple]:
    conn = _connect()
    with conn, conn.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall() if cursor.description else []
    conn.close()
    return rows


class OnboardingTestCase(SaasDatabaseTestCase):
    def setUp(self):
        self._cleanup()
        conn = _connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "insert into public.platform_admins (user_id) values (%s) on conflict do nothing",
                (PLATFORM_ADMIN_USER_ID,),
            )
            cursor.execute("select coalesce(max(id), 0) from public.audit_log")
            self.audit_baseline = cursor.fetchone()[0]
        conn.close()
        self.addCleanup(self._cleanup_new_audit_rows)
        self.cindy_before = self._cindy_snapshot()
        self.auth_admin = FakeAuthAdmin()
        self.authenticator = FakeAuthenticator()
        self.authenticator.users["admin@example.com"] = (PASSWORD, PLATFORM_ADMIN_USER_ID)
        self.app = create_app(_settings(), authenticator=self.authenticator, auth_admin=self.auth_admin)
        self.client = self.app.test_client()
        self.addCleanup(self._cleanup)
        self.addCleanup(self._assert_cindy_original_rows_untouched)
        self.login()

    # -- fixtures ----------------------------------------------------------------
    def _cleanup(self):
        conn = _connect()
        with conn, conn.cursor() as cursor:
            cursor.execute("select id from public.tenants where slug like %s", (SLUG_PREFIX + "%",))
            ids = [row[0] for row in cursor.fetchall()]
            if ids:
                cursor.execute("delete from public.audit_log where tenant_id = any(%s)", (ids,))
                cursor.execute("delete from public.tenants where id = any(%s)", (ids,))
            cursor.execute(
                "select user_id from public.platform_user_directory where email like %s", ("%" + DOMAIN,)
            )
            users = [row[0] for row in cursor.fetchall()]
            if users:
                # Memberships these tests added to the seeded Cindy tenant, and their audit rows.
                cursor.execute(
                    "delete from public.audit_log where tenant_id = %s and metadata->>'user_id' = any(%s)",
                    (TENANT_ONE_ID, [str(u) for u in users]),
                )
                cursor.execute(
                    "delete from public.tenant_users where tenant_id = %s and user_id = any(%s)",
                    (TENANT_ONE_ID, users),
                )
                cursor.execute("delete from public.platform_user_directory where user_id = any(%s)", (users,))
            cursor.execute(
                "delete from public.platform_admins where user_id = %s", (PLATFORM_ADMIN_USER_ID,)
            )
            # Reset-throttle state (0043) is test-only data in the disposable database.
            cursor.execute("delete from public.password_reset_requests")
        conn.close()

    def _cleanup_new_audit_rows(self):
        """Audit rows these tests wrote on Cindy Bakes or at platform level (no tenant)."""
        conn = _connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "delete from public.audit_log where id > %s and (tenant_id is null or tenant_id = %s)",
                (self.audit_baseline, TENANT_ONE_ID),
            )
        conn.close()

    @staticmethod
    def _cindy_snapshot():
        return (
            _rows("select id, name, slug, status from public.tenants where id = %s", (TENANT_ONE_ID,)),
            _rows("select id, user_id, role, status from public.tenant_users "
                  "where tenant_id = %s and user_id = any(%s) order by id",
                  (TENANT_ONE_ID, [TENANT_ONE_OWNER_USER_ID, TENANT_ONE_STAFF_USER_ID])),
        )

    def _assert_cindy_original_rows_untouched(self):
        self.assertEqual(self._cindy_snapshot(), self.cindy_before)

    # -- helpers -----------------------------------------------------------------
    def token(self, client=None):
        with (client or self.client).session_transaction() as session:
            return session["csrf_token"]

    def login(self, client=None, email="admin@example.com"):
        client = client or self.client
        client.get("/login")
        return client.post("/login", data={"csrf_token": self.token(client), "email": email,
                                           "password": PASSWORD})

    def post(self, path, data=None, client=None):
        client = client or self.client
        return client.post(path, data={**(data or {}), "csrf_token": self.token(client)})

    def follow(self, response, client=None) -> str:
        return (client or self.client).get(response.headers["Location"]).get_data(as_text=True)

    def onboard(self, slug_suffix="alpha", mode="invite", email=None, **extra):
        data = {
            "name": f"Onboarding {slug_suffix.title()}",
            "slug": SLUG_PREFIX + slug_suffix,
            "contact_email": f"hello-{slug_suffix}{DOMAIN}",
            "contact_phone": "+254 700 000 000",
            "owner_full_name": f"Owner {slug_suffix.title()}",
            "owner_email": email or f"owner-{slug_suffix}{DOMAIN}",
            "owner_mode": mode,
        }
        data.update(extra)
        return self.post("/tenants/onboard", data)

    def tenant_id(self, slug_suffix):
        rows = _rows("select id from public.tenants where slug = %s", (SLUG_PREFIX + slug_suffix,))
        return str(rows[0][0]) if rows else None

    def members(self, tenant_id):
        return _rows("select id, user_id::text, role, status from public.tenant_users "
                     "where tenant_id = %s order by created_at, id", (tenant_id,))

    def audit_actions(self, tenant_id):
        return [r[0] for r in _rows("select action from public.audit_log where tenant_id = %s", (tenant_id,))]


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------
class OnboardingTests(OnboardingTestCase):
    def test_invite_creates_tenant_owner_contact_directory_and_audit_in_one_step(self):
        response = self.onboard("alpha")
        self.assertEqual(response.status_code, 302)
        page = self.follow(response)
        self.assertIn("Invitation sent to owner-alpha" + DOMAIN, page)

        tenant_id = self.tenant_id("alpha")
        self.assertIsNotNone(tenant_id)
        self.assertEqual(_rows("select status from public.tenants where id = %s", (tenant_id,)), [("draft",)])
        invited = self.auth_admin.users["owner-alpha" + DOMAIN]
        self.assertEqual(self.auth_admin.invites, [("owner-alpha" + DOMAIN, "Owner Alpha", CALLBACK)])
        # Real Auth UUID, owner, active.
        self.assertEqual([m[1:] for m in self.members(tenant_id)], [(invited.id, "owner", "active")])
        contact = _rows("select configuration->'contact' from public.tenant_settings where tenant_id = %s",
                        (tenant_id,))[0][0]
        self.assertEqual(contact, {"email": "hello-alpha" + DOMAIN, "phone": "+254 700 000 000"})
        directory = _rows("select email, full_name, invited_at is not null from public.platform_user_directory "
                          "where user_id = %s", (invited.id,))
        self.assertEqual(directory, [("owner-alpha" + DOMAIN, "Owner Alpha", True)])
        actions = self.audit_actions(tenant_id)
        for action in ("tenant.provisioned", "tenant.contact_updated", "tenant.onboarded"):
            self.assertIn(action, actions)

    def test_link_existing_account_sends_no_invitation(self):
        existing = self.auth_admin.add_existing("owner-beta" + DOMAIN, "Beta Person")
        response = self.onboard("beta", mode="link")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.auth_admin.invites, [])
        self.assertEqual([m[1:] for m in self.members(self.tenant_id("beta"))],
                         [(existing.id, "owner", "active")])

    def test_link_with_a_mismatched_user_id_is_refused(self):
        self.auth_admin.add_existing("owner-gamma" + DOMAIN)
        other = self.auth_admin.add_existing("someone-else" + DOMAIN)
        response = self.onboard("gamma", mode="link", owner_user_id=other.id)
        self.assertEqual(response.status_code, 409)
        self.assertIn("different email address", response.get_data(as_text=True))
        self.assertIsNone(self.tenant_id("gamma"))

    def test_link_without_an_account_is_refused(self):
        response = self.onboard("delta", mode="link")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Choose &#34;Invite&#34;", response.get_data(as_text=True))
        self.assertIsNone(self.tenant_id("delta"))

    def test_duplicate_email_is_never_invited_twice(self):
        self.auth_admin.add_existing("owner-eps" + DOMAIN)
        response = self.onboard("eps", mode="invite")
        self.assertEqual(response.status_code, 409)
        self.assertIn("already exists", response.get_data(as_text=True))
        self.assertEqual(self.auth_admin.invites, [])
        self.assertIsNone(self.tenant_id("eps"))

    def test_duplicate_tenant_is_refused_before_any_supabase_call(self):
        response = self.post("/tenants/onboard", {
            "name": "Cindy Bakes again", "slug": TENANT_ONE_SLUG,
            "owner_full_name": "Someone", "owner_email": "dup-owner" + DOMAIN, "owner_mode": "invite",
        })
        self.assertEqual(response.status_code, 409)
        self.assertIn("already exists", response.get_data(as_text=True))
        self.assertEqual(self.auth_admin.calls, 0, "no account may be created for a duplicate tenant")
        self.assertEqual(_rows("select count(*) from public.tenants where slug = %s", (TENANT_ONE_SLUG,)), [(1,)])

    def test_failed_invitation_creates_nothing(self):
        self.auth_admin.fail_invite = AuthAdminError("Supabase is rate-limiting emails.", "rate_limited")
        response = self.onboard("zeta")
        self.assertEqual(response.status_code, 429)
        self.assertIsNone(self.tenant_id("zeta"))

    def test_partial_failure_keeps_the_account_and_a_retry_by_link_converges(self):
        missing_plan = str(uuid.uuid4())
        response = self.onboard("eta", plan_id=missing_plan)
        self.assertGreaterEqual(response.status_code, 400)
        body = response.get_data(as_text=True)
        self.assertIn("nothing was deleted", body)
        self.assertIn("Link an existing account", body)
        self.assertIsNone(self.tenant_id("eta"), "the failed provisioning must roll back entirely")
        account = self.auth_admin.users["owner-eta" + DOMAIN]
        # M4: the invited account stays discoverable even though provisioning rolled back.
        self.assertEqual(_rows("select user_id::text from public.platform_user_directory where email = %s",
                               ("owner-eta" + DOMAIN,)), [(account.id,)])
        orphans = self.client.get("/members", query_string={"view": "unattached"}).get_data(as_text=True)
        self.assertIn("owner-eta" + DOMAIN, orphans)

        retry = self.onboard("eta", mode="link")
        self.assertEqual(retry.status_code, 302)
        self.assertEqual(len(self.auth_admin.invites), 1, "the retry must not invite again")
        self.assertEqual([m[1:] for m in self.members(self.tenant_id("eta"))],
                         [(account.id, "owner", "active")])
        orphans = self.client.get("/members", query_string={"view": "unattached"}).get_data(as_text=True)
        self.assertNotIn("<br>owner-eta" + DOMAIN + "<br>", orphans)  # no table row (the flash may name it)
        self.assertIn("Every known account has a membership.", orphans)

    def test_invalid_input_is_rejected_before_any_supabase_call(self):
        for field, value in (("owner_email", "not-an-email"), ("slug", "Bad Slug"),
                             ("contact_phone", "call me"), ("owner_mode", "create")):
            response = self.onboard("theta", **{field: value})
            self.assertEqual(response.status_code, 400, field)
        self.assertEqual(self.auth_admin.calls, 0)
        self.assertIsNone(self.tenant_id("theta"))

    def test_onboarding_without_auth_configuration_explains_and_creates_nothing(self):
        app = create_app(_settings(supabase_service_role_key=None, public_url=None),
                         authenticator=self.authenticator)
        client = app.test_client()
        self.login(client)
        page = client.get("/tenants/onboard").get_data(as_text=True)
        self.assertIn("not configured", page)
        response = self.post("/tenants/onboard", {
            "name": "Iota", "slug": SLUG_PREFIX + "iota", "owner_full_name": "I Owner",
            "owner_email": "owner-iota" + DOMAIN, "owner_mode": "invite"}, client=client)
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(self.tenant_id("iota"))


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
class MembersTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.onboard("alpha")
        self.onboard("beta")
        self.alpha, self.beta = self.tenant_id("alpha"), self.tenant_id("beta")

    def invite_staff(self, tenant_id, name="staff-one"):
        return self.post(f"/tenants/{tenant_id}/members/invite",
                         {"full_name": name.title(), "email": name + DOMAIN, "role": "staff"})

    def membership(self, tenant_id, email):
        user = self.auth_admin.users[email]
        return next(m for m in self.members(tenant_id) if m[1] == user.id)

    def test_invite_member_to_existing_tenant(self):
        page = self.follow(self.invite_staff(self.alpha))
        self.assertIn("Invitation sent to staff-one" + DOMAIN, page)
        member = self.membership(self.alpha, "staff-one" + DOMAIN)
        self.assertEqual(member[2:], ("staff", "active"))
        self.assertIn("member.added", self.audit_actions(self.alpha))

    def test_members_listing_and_filters(self):
        self.invite_staff(self.alpha)
        page = self.client.get("/members").get_data(as_text=True)
        for text in ("owner-alpha" + DOMAIN, "owner-beta" + DOMAIN, "staff-one" + DOMAIN, "Cindy Bakes"):
            self.assertIn(text, page)
        by_search = self.client.get("/members", query_string={"q": "STAFF-ONE"}).get_data(as_text=True)
        self.assertIn("staff-one" + DOMAIN, by_search)
        self.assertNotIn("owner-alpha" + DOMAIN, by_search)
        by_name = self.client.get("/members", query_string={"q": "owner beta"}).get_data(as_text=True)
        self.assertIn("owner-beta" + DOMAIN, by_name)
        by_tenant = self.client.get("/members", query_string={"tenant_id": self.beta}).get_data(as_text=True)
        self.assertIn("owner-beta" + DOMAIN, by_tenant)
        self.assertNotIn("owner-alpha" + DOMAIN, by_tenant)
        by_role = self.client.get("/members", query_string={"role": "staff"}).get_data(as_text=True)
        self.assertIn("staff-one" + DOMAIN, by_role)
        self.assertNotIn("owner-alpha" + DOMAIN, by_role)
        by_status = self.client.get("/members", query_string={"status": "disabled"}).get_data(as_text=True)
        self.assertNotIn("staff-one" + DOMAIN, by_status)
        wildcard = self.client.get("/members", query_string={"q": "%"}).get_data(as_text=True)
        self.assertIn("No members match.", wildcard)
        bad = self.client.get("/members", query_string={"role": "superuser"}).get_data(as_text=True)
        self.assertIn("Role must be one of", bad)

    def test_role_change_and_audit(self):
        self.invite_staff(self.alpha)
        member = self.membership(self.alpha, "staff-one" + DOMAIN)
        self.post(f"/tenants/{self.alpha}/members/{member[0]}/role", {"role": "owner"})
        self.assertEqual(self.membership(self.alpha, "staff-one" + DOMAIN)[2], "owner")
        self.assertIn("member.updated", self.audit_actions(self.alpha))
        response = self.post(f"/tenants/{self.alpha}/members/{member[0]}/role", {"role": "admin"})
        self.assertIn("Role must be one of", self.follow(response))

    def test_activate_and_deactivate(self):
        self.invite_staff(self.alpha)
        member = self.membership(self.alpha, "staff-one" + DOMAIN)
        self.post(f"/tenants/{self.alpha}/members/{member[0]}/status", {"status": "disabled"})
        self.assertEqual(self.membership(self.alpha, "staff-one" + DOMAIN)[3], "disabled")
        self.post(f"/tenants/{self.alpha}/members/{member[0]}/status", {"status": "active"})
        self.assertEqual(self.membership(self.alpha, "staff-one" + DOMAIN)[3], "active")

    def test_last_active_owner_cannot_be_deactivated_demoted_or_removed(self):
        owner = self.membership(self.alpha, "owner-alpha" + DOMAIN)
        for path, data in (("status", {"status": "disabled"}), ("role", {"role": "staff"}),
                           ("remove", {"confirm": "revoke"})):
            response = self.post(f"/tenants/{self.alpha}/members/{owner[0]}/{path}", data)
            self.assertIn("at least one active owner", self.follow(response), path)
        self.assertEqual(self.membership(self.alpha, "owner-alpha" + DOMAIN)[2:], ("owner", "active"))

    def test_ownership_transfer_by_adding_a_second_owner_first(self):
        self.invite_staff(self.alpha, "new-owner")
        new_owner = self.membership(self.alpha, "new-owner" + DOMAIN)
        self.post(f"/tenants/{self.alpha}/members/{new_owner[0]}/role", {"role": "owner"})
        old_owner = self.membership(self.alpha, "owner-alpha" + DOMAIN)
        self.post(f"/tenants/{self.alpha}/members/{old_owner[0]}/status", {"status": "disabled"})
        self.assertEqual(self.membership(self.alpha, "owner-alpha" + DOMAIN)[3], "disabled")

    def test_revoke_requires_confirmation_and_keeps_the_auth_account(self):
        self.invite_staff(self.alpha)
        member = self.membership(self.alpha, "staff-one" + DOMAIN)
        response = self.post(f"/tenants/{self.alpha}/members/{member[0]}/remove", {})
        self.assertIn("confirmation", self.follow(response))
        self.assertEqual(len(self.members(self.alpha)), 2)
        response = self.post(f"/tenants/{self.alpha}/members/{member[0]}/remove", {"confirm": "revoke"})
        self.assertIn("Auth account was not deleted", self.follow(response))
        self.assertEqual(len(self.members(self.alpha)), 1)
        self.assertIn("staff-one" + DOMAIN, self.auth_admin.users)
        self.assertIn("member.removed", self.audit_actions(self.alpha))

    def test_membership_ids_cannot_cross_tenants(self):
        alpha_owner = self.membership(self.alpha, "owner-alpha" + DOMAIN)
        for path, data in (("role", {"role": "staff"}), ("status", {"status": "disabled"}),
                           ("remove", {"confirm": "revoke"}), ("resend-invite", {}), ("password-reset", {})):
            response = self.post(f"/tenants/{self.beta}/members/{alpha_owner[0]}/{path}", data)
            self.assertIn("not found", self.follow(response).lower(), path)
        self.assertEqual(self.membership(self.alpha, "owner-alpha" + DOMAIN)[2:], ("owner", "active"))
        self.assertEqual(self.auth_admin.resets, [])

    def test_duplicate_membership_is_refused(self):
        response = self.post(f"/tenants/{self.alpha}/members",
                             {"email": "owner-alpha" + DOMAIN, "role": "staff"})
        self.assertIn("already exists", self.follow(response))
        self.assertEqual(len(self.members(self.alpha)), 1)

    def test_add_existing_account_verifies_it_exists(self):
        response = self.post(f"/tenants/{self.alpha}/members", {"email": "ghost" + DOMAIN, "role": "staff"})
        self.assertIn("No Supabase Auth account uses", self.follow(response))
        response = self.post(f"/tenants/{self.alpha}/members", {"user_id": str(uuid.uuid4()), "role": "staff"})
        self.assertIn("No Supabase Auth account has that user id", self.follow(response))
        self.assertEqual(len(self.members(self.alpha)), 1)

    def test_resend_invitation_and_password_reset(self):
        owner = self.membership(self.alpha, "owner-alpha" + DOMAIN)
        page = self.follow(self.post(f"/tenants/{self.alpha}/members/{owner[0]}/resend-invite"))
        self.assertIn("Invitation re-sent", page)
        page = self.follow(self.post(f"/tenants/{self.alpha}/members/{owner[0]}/password-reset"))
        self.assertIn("Password reset email sent", page)
        self.assertEqual(self.auth_admin.resets, [("owner-alpha" + DOMAIN, CALLBACK)])

    def test_global_list_actions_return_to_the_global_list(self):
        self.invite_staff(self.alpha)
        member = self.membership(self.alpha, "staff-one" + DOMAIN)
        response = self.post(f"/tenants/{self.alpha}/members/{member[0]}/status",
                             {"status": "disabled", "return_to": "members"})
        self.assertTrue(response.headers["Location"].endswith("/members"))


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
class AuthorizationTests(OnboardingTestCase):
    def test_tenant_owner_cannot_use_admin_member_pages(self):
        self.onboard("alpha")
        owner = self.auth_admin.users["owner-alpha" + DOMAIN]
        self.authenticator.users["owner-alpha" + DOMAIN] = (PASSWORD, owner.id)
        # Admin sign-in refuses a tenant owner outright: no admin session is created.
        client = self.app.test_client()
        self.login(client, "owner-alpha" + DOMAIN)
        response = client.get("/members")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        # Even a session claiming the owner's id is refused by the per-request database check.
        for method, path in (("get", "/members"), ("post", "/tenants/onboard"),
                             ("post", f"/tenants/{self.tenant_id('alpha')}/members/invite")):
            client = self.app.test_client()
            client.get("/login")
            with client.session_transaction() as session:
                session["user_id"] = owner.id
                session["exp"] = 10**10
                token = session["csrf_token"]
            data = {"csrf_token": token, "name": "x", "slug": SLUG_PREFIX + "evil",
                    "email": "evil" + DOMAIN, "full_name": "Evil", "role": "owner"}
            response = getattr(client, method)(path, data=data if method == "post" else None)
            self.assertEqual(response.status_code, 403, path)
        self.assertIsNone(self.tenant_id("evil"))
        self.assertEqual(self.auth_admin.invites, [("owner-alpha" + DOMAIN, "Owner Alpha", CALLBACK)])

    def test_database_refuses_member_functions_and_directory_to_client_roles(self):
        import psycopg  # noqa: PLC0415

        conn = psycopg.connect(app_backend_test_url())
        try:
            for sql in ("select * from app.platform_list_members(null, null, null, null)",
                        "select * from public.platform_user_directory",
                        f"select app.platform_remove_member('{TENANT_ONE_ID}', gen_random_uuid())"):
                with self.subTest(sql=sql), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with conn.cursor() as cursor:
                        cursor.execute(sql)
                conn.rollback()
        finally:
            conn.close()
        grants = _rows(
            "select r.rolname, has_table_privilege(r.rolname, 'public.platform_user_directory', 'SELECT') "
            "from pg_roles r where r.rolname in ('anon', 'authenticated', 'app_backend', 'service_role')")
        self.assertTrue(grants)
        self.assertEqual([g for g in grants if g[1]], [])

    def test_platform_admin_check_is_enforced_inside_the_database(self):
        import psycopg  # noqa: PLC0415

        conn = psycopg.connect(platform_admin_test_url())
        try:
            with conn.cursor() as cursor:
                cursor.execute("select set_config('request.jwt.claims', %s, true)",
                               ('{"sub": "%s"}' % uuid.uuid4(),))
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    cursor.execute("select * from app.platform_list_members(null, null, null, null)")
        finally:
            conn.close()

    def test_membership_roles_never_grant_platform_admin(self):
        self.onboard("alpha")
        owner = self.auth_admin.users["owner-alpha" + DOMAIN]
        self.assertEqual(_rows("select count(*) from public.platform_admins where user_id = %s", (owner.id,)),
                         [(0,)])


# ---------------------------------------------------------------------------
# Cindy Bakes (seeded TENANT_ONE): link a real owner without touching existing data
# ---------------------------------------------------------------------------
class CindyBakesCompatibilityTests(OnboardingTestCase):
    def test_link_a_real_owner_to_the_existing_cindy_tenant_and_sign_in(self):
        real = self.auth_admin.add_existing("cindy-owner" + DOMAIN, "Cindy Owner")
        response = self.post(f"/tenants/{TENANT_ONE_ID}/members",
                             {"email": "cindy-owner" + DOMAIN, "role": "owner", "status": "active"})
        self.assertIn("Added cindy-owner" + DOMAIN + " as owner", self.follow(response))
        rows = _rows("select role, status from public.tenant_users where tenant_id = %s and user_id = %s",
                     (TENANT_ONE_ID, real.id))
        self.assertEqual(rows, [("owner", "active")])
        self.assertEqual(_rows("select count(*) from public.tenants where slug = %s", (TENANT_ONE_SLUG,)), [(1,)])

        # The owner signs in through the real tenant portal and lands on Cindy Bakes only.
        self.authenticator.users["cindy-owner" + DOMAIN] = (PASSWORD, real.id)
        portal = self.app.test_client()
        portal.get("/t/login")
        response = portal.post("/t/login", data={"csrf_token": self.token(portal),
                                                 "email": "cindy-owner" + DOMAIN, "password": PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/t/{TENANT_ONE_ID}/", response.headers["Location"])
        self.assertEqual(portal.get(f"/t/{TENANT_ONE_ID}/").status_code, 200)
        self.assertEqual(portal.get(f"/t/{TENANT_ONE_ID}/settings").status_code, 200)

        # Isolation: any other tenant is refused.
        self.onboard("other")
        self.assertEqual(portal.get(f"/t/{self.tenant_id('other')}/").status_code, 403)

    def test_cindy_members_page_shows_placeholders_without_an_account(self):
        page = self.client.get(f"/tenants/{TENANT_ONE_ID}/members").get_data(as_text=True)
        self.assertIn(TENANT_ONE_OWNER_USER_ID, page)
        self.assertIn("No Auth account on record", page)
        self.assertIn("Unverified", page)
        unverified = self.client.get("/members", query_string={"view": "unverified"}).get_data(as_text=True)
        self.assertIn(TENANT_ONE_OWNER_USER_ID, unverified)
        self.assertIn(TENANT_ONE_STAFF_USER_ID, unverified)

    def test_cindy_gets_a_real_owner_then_placeholders_can_be_retired(self):
        real = self.auth_admin.add_existing("cindy-owner" + DOMAIN, "Cindy Owner")
        self.post(f"/tenants/{TENANT_ONE_ID}/members",
                  {"email": "cindy-owner" + DOMAIN, "role": "owner", "status": "active"})
        placeholder = _rows("select id from public.tenant_users where tenant_id = %s and user_id = %s",
                            (TENANT_ONE_ID, TENANT_ONE_OWNER_USER_ID))[0][0]
        real_membership = _rows("select id from public.tenant_users where tenant_id = %s and user_id = %s",
                                (TENANT_ONE_ID, real.id))[0][0]
        try:
            response = self.post(f"/tenants/{TENANT_ONE_ID}/members/{placeholder}/status", {"status": "disabled"})
            self.assertIn("Membership deactivated", self.follow(response))
            # The real owner is now the only verified active owner, so it cannot be removed.
            response = self.post(f"/tenants/{TENANT_ONE_ID}/members/{real_membership}/status",
                                 {"status": "disabled"})
            self.assertIn("verified account", self.follow(response))
            self.assertEqual(_rows("select status from public.tenant_users where id = %s", (real_membership,)),
                             [("active",)])
        finally:
            conn = _connect()
            with conn, conn.cursor() as cursor:  # restore the seeded placeholder exactly
                cursor.execute("update public.tenant_users set status = 'active' where id = %s", (placeholder,))
            conn.close()


# ---------------------------------------------------------------------------
# H1 / M6: suspended and inactive tenants
# ---------------------------------------------------------------------------
def _set_tenant_status(tenant_id: str, status: str) -> None:
    conn = _connect()
    with conn, conn.cursor() as cursor:
        cursor.execute("update public.tenants set status = %s where id = %s", (status, tenant_id))
    conn.close()


class SuspendedTenantTests(OnboardingTestCase):
    PHONE = "onb-test-phone-001"

    def setUp(self):
        super().setUp()
        self.onboard("alpha")
        self.alpha = self.tenant_id("alpha")
        self.owner = self.auth_admin.users["owner-alpha" + DOMAIN]
        self.authenticator.users["owner-alpha" + DOMAIN] = (PASSWORD, self.owner.id)

    def portal_login(self):
        portal = self.app.test_client()
        portal.get("/t/login")
        response = portal.post("/t/login", data={"csrf_token": self.token(portal),
                                                 "email": "owner-alpha" + DOMAIN, "password": PASSWORD})
        return portal, response

    def test_draft_and_active_tenants_admit_members(self):
        for status in ("draft", "active"):
            with self.subTest(status=status):
                _set_tenant_status(self.alpha, status)
                portal, response = self.portal_login()
                self.assertEqual(response.status_code, 302)
                self.assertEqual(portal.get(f"/t/{self.alpha}/").status_code, 200)

    def test_suspended_and_inactive_tenants_refuse_sign_in_and_existing_sessions(self):
        portal, _ = self.portal_login()
        self.assertEqual(portal.get(f"/t/{self.alpha}/").status_code, 200)
        for status in ("suspended", "inactive"):
            with self.subTest(status=status):
                _set_tenant_status(self.alpha, status)
                # An existing session is re-checked on every request.
                self.assertEqual(portal.get(f"/t/{self.alpha}/").status_code, 403)
                self.assertEqual(portal.get(f"/t/{self.alpha}/settings").status_code, 403)
                reply = portal.post(f"/t/{self.alpha}/inbox/{uuid.uuid4()}/reply",
                                    data={"csrf_token": self.token(portal), "text": "hello"})
                self.assertEqual(reply.status_code, 403)
                # A new sign-in gets the same generic error as a wrong password.
                fresh, response = self.portal_login()
                self.assertEqual(response.status_code, 401)
                self.assertIn("Invalid email or password", response.get_data(as_text=True))
        _set_tenant_status(self.alpha, "active")
        portal, response = self.portal_login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(portal.get(f"/t/{self.alpha}/").status_code, 200)

    def test_suspending_one_tenant_leaves_the_members_other_tenants_open(self):
        self.onboard("beta", mode="link", email="owner-alpha" + DOMAIN)
        beta = self.tenant_id("beta")
        _set_tenant_status(self.alpha, "suspended")
        portal, response = self.portal_login()
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/t/{beta}/", response.headers["Location"])
        self.assertEqual(portal.get(f"/t/{beta}/").status_code, 200)
        self.assertEqual(portal.get(f"/t/{self.alpha}/").status_code, 403)

    def _activate_with_channel(self, channel_status="active"):
        _set_tenant_status(self.alpha, "active")
        conn = _connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "insert into public.tenant_channels (tenant_id, channel_type, phone_number_id, waba_id, "
                "display_phone_number, status, is_default, credentials_ref) "
                "values (%s, 'whatsapp', %s, 'onb-test-waba', '+254700000999', %s, true, %s)",
                (self.alpha, self.PHONE, channel_status, f"tenants/{self.alpha}/whatsapp"))
        conn.close()

    def _send(self, settings):
        from platform_admin.services import whatsapp as whatsapp_service  # noqa: PLC0415

        return whatsapp_service.send_text(
            settings, phone_number_id=self.PHONE, tenant_id=self.alpha,
            credentials_ref=f"tenants/{self.alpha}/whatsapp", recipient="254700000001", text="hi")

    def test_outbound_messages_require_an_active_tenant_and_an_active_channel(self):
        from unittest.mock import MagicMock, patch  # noqa: PLC0415

        from platform_admin.services import whatsapp as whatsapp_service  # noqa: PLC0415
        from platform_admin.services.validation import ServiceError  # noqa: PLC0415

        settings = _settings(whatsapp_send_enabled=True)
        self._activate_with_channel()
        provider = MagicMock()
        provider.return_value.__enter__.return_value.read.return_value = b'{"messages": [{"id": "wamid.T"}]}'
        with patch.object(whatsapp_service, "access_token", return_value="test-token") as token, \
                patch.object(whatsapp_service.urllib.request, "urlopen", provider):
            self.assertEqual(self._send(settings), "wamid.T")
            self.assertEqual(provider.call_count, 1)
            for status in ("suspended", "inactive"):
                with self.subTest(status=status):
                    _set_tenant_status(self.alpha, status)
                    with self.assertRaises(ServiceError) as raised:
                        self._send(settings)
                    self.assertEqual(raised.exception.status, 409)
                    # Refused before the credential lookup and before any provider call.
                    self.assertEqual(provider.call_count, 1)
            self.assertEqual(token.call_count, 1)
            # Another tenant's id with this phone number is refused too (cross-tenant).
            _set_tenant_status(self.alpha, "active")
            with self.assertRaises(ServiceError):
                whatsapp_service.send_text(
                    settings, phone_number_id=self.PHONE, tenant_id=TENANT_ONE_ID,
                    credentials_ref=f"tenants/{TENANT_ONE_ID}/whatsapp", recipient="254700000001", text="x")
            self.assertEqual(provider.call_count, 1)

    def test_a_disabled_channel_cannot_send_even_for_an_active_tenant(self):
        from platform_admin.services import whatsapp as whatsapp_service  # noqa: PLC0415
        from platform_admin.services.validation import ServiceError  # noqa: PLC0415

        self._activate_with_channel(channel_status="disabled")
        with self.assertRaises(ServiceError) as raised:
            whatsapp_service.require_sendable_channel(_settings(), self.alpha, self.PHONE)
        self.assertEqual(raised.exception.status, 409)

    def test_inbound_routing_still_requires_an_active_tenant(self):
        from platform_admin.services import whatsapp as whatsapp_service  # noqa: PLC0415

        self._activate_with_channel()
        self.assertIsNotNone(whatsapp_service.resolve_channel(_settings(), self.PHONE))
        _set_tenant_status(self.alpha, "suspended")
        self.assertIsNone(whatsapp_service.resolve_channel(_settings(), self.PHONE))


# ---------------------------------------------------------------------------
# M1: a deleted and recreated Supabase account
# ---------------------------------------------------------------------------
class RecreatedAccountTests(OnboardingTestCase):
    def test_recreated_account_is_linked_and_the_reassignment_audited(self):
        email = "owner-alpha" + DOMAIN
        self.onboard("alpha")
        old = self.auth_admin.users.pop(email)          # deleted in Supabase ...
        new = self.auth_admin.add_existing(email, "Owner Alpha")  # ... and created again
        self.assertNotEqual(old.id, new.id)

        response = self.onboard("beta", mode="link", email=email)
        self.assertEqual(response.status_code, 302)
        self.assertEqual([m[1:] for m in self.members(self.tenant_id("beta"))], [(new.id, "owner", "active")])
        self.assertEqual(_rows("select user_id::text from public.platform_user_directory where email = %s",
                               (email,)), [(new.id,)])
        self.assertEqual(self.auth_admin.invites, [(email, "Owner Alpha", CALLBACK)], "no second invitation")
        audit = _rows("select metadata from public.audit_log where action = 'directory.entry_reassigned' "
                      "and id > %s", (self.audit_baseline,))
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0][0]["previous_user_id"], old.id)
        self.assertEqual(audit[0][0]["user_id"], new.id)
        self.assertEqual(audit[0][0]["previous_user_memberships"], 1)
        self.assertNotIn("@", str(audit[0][0]))
        # The old membership is now shown as unverified and no longer satisfies readiness.
        page = self.client.get(f"/tenants/{self.tenant_id('alpha')}/members").get_data(as_text=True)
        self.assertIn("Unverified", page)

    def test_recreated_account_can_be_added_to_an_existing_tenant(self):
        email = "staff-re" + DOMAIN
        self.onboard("alpha")
        alpha = self.tenant_id("alpha")
        self.post(f"/tenants/{alpha}/members/invite", {"full_name": "Staff Re", "email": email, "role": "staff"})
        self.auth_admin.users.pop(email)
        new = self.auth_admin.add_existing(email)
        response = self.post(f"/tenants/{alpha}/members", {"email": email, "role": "staff"})
        self.assertIn("Added " + email, self.follow(response))
        self.assertIn(new.id, [m[1] for m in self.members(alpha)])
        self.assertEqual(_rows("select user_id::text from public.platform_user_directory where email = %s",
                               (email,)), [(new.id,)])

    def test_resend_to_a_recreated_account_explains_instead_of_pretending(self):
        email = "owner-alpha" + DOMAIN
        self.onboard("alpha")
        alpha = self.tenant_id("alpha")
        membership = self.members(alpha)[0][0]
        self.auth_admin.users.pop(email)   # deleted; a fresh invite creates a NEW account
        page = self.follow(self.post(f"/tenants/{alpha}/members/{membership}/resend-invite"))
        self.assertIn("created a NEW account", page)
        new = self.auth_admin.users[email]
        self.assertEqual(_rows("select user_id::text from public.platform_user_directory where email = %s",
                               (email,)), [(new.id,)])
        unattached = self.client.get("/members", query_string={"view": "unattached"}).get_data(as_text=True)
        self.assertIn(new.id, unattached)


# ---------------------------------------------------------------------------
# M2: audit completeness without personal data
# ---------------------------------------------------------------------------
class AuditCompletenessTests(OnboardingTestCase):
    def new_audit(self):
        return _rows("select action, tenant_id::text, actor_user_id::text, metadata from public.audit_log "
                     "where id > %s order by id", (self.audit_baseline,))

    def test_every_member_action_and_directory_change_is_audited_without_email_or_name(self):
        self.onboard("alpha")
        alpha = self.tenant_id("alpha")
        owner = self.auth_admin.users["owner-alpha" + DOMAIN]
        membership = self.members(alpha)[0][0]
        self.follow(self.post(f"/tenants/{alpha}/members/{membership}/resend-invite"))
        self.follow(self.post(f"/tenants/{alpha}/members/{membership}/password-reset"))

        rows = self.new_audit()
        actions = [r[0] for r in rows]
        for action in ("directory.entry_added", "tenant.provisioned", "tenant.onboarded",
                       "member.invitation_resent", "directory.entry_updated", "member.password_reset_sent"):
            self.assertIn(action, actions)
        by_action = {r[0]: r for r in rows}
        self.assertEqual(by_action["member.invitation_resent"][1], alpha)
        self.assertEqual(by_action["member.invitation_resent"][3], {"user_id": owner.id})
        self.assertEqual(by_action["member.password_reset_sent"][3], {"user_id": owner.id})
        self.assertEqual(by_action["directory.entry_updated"][3]["changed"], ["invited_at"])
        self.assertIsNone(by_action["directory.entry_added"][1], "directory events are platform-level")
        for action, _, actor, metadata in rows:
            self.assertEqual(actor, PLATFORM_ADMIN_USER_ID, action)
            if action.startswith(("directory.", "member.")):
                self.assertNotIn("@", str(metadata), action)
                self.assertNotIn("Owner Alpha", str(metadata), action)

    def test_an_unchanged_directory_entry_writes_no_audit_row(self):
        from saas import platform  # noqa: PLC0415

        self.onboard("alpha")
        owner = self.auth_admin.users["owner-alpha" + DOMAIN]
        before = len(self.new_audit())
        platform.upsert_user_directory(PLATFORM_ADMIN_USER_ID, owner.id, owner.email, "Owner Alpha", False,
                                       url=platform_admin_test_url())
        self.assertEqual(len(self.new_audit()), before)


# ---------------------------------------------------------------------------
# M3: no phantom owners
# ---------------------------------------------------------------------------
class PhantomOwnerTests(OnboardingTestCase):
    def phantom_owner(self, tenant_id) -> str:
        phantom = str(uuid.uuid4())
        conn = _connect()
        with conn, conn.cursor() as cursor:
            cursor.execute("insert into public.tenant_users (tenant_id, user_id, role, status) "
                           "values (%s, %s, 'owner', 'active') returning id", (tenant_id, phantom))
            membership = str(cursor.fetchone()[0])
        conn.close()
        return membership

    def readiness_owner(self, tenant_id) -> bool:
        from saas import platform  # noqa: PLC0415

        checks = platform.tenant_readiness(PLATFORM_ADMIN_USER_ID, tenant_id,
                                           url=platform_admin_test_url())["checks"]
        return next(c["ok"] for c in checks if c["key"] == "owner")

    def test_a_phantom_owner_never_satisfies_readiness(self):
        response = self.post("/tenants/new", {"name": "Phantom Co", "slug": SLUG_PREFIX + "phantom",
                                              "timezone": "Africa/Nairobi", "currency": "KES"})
        self.assertEqual(response.status_code, 302)
        tenant = self.tenant_id("phantom")
        self.phantom_owner(tenant)
        self.assertFalse(self.readiness_owner(tenant))
        page = self.follow(self.post(f"/tenants/{tenant}/status", {"status": "active"}))
        self.assertIn("not ready to activate", page)
        self.assertIn("owner", page)
        self.onboard("alpha")
        self.assertTrue(self.readiness_owner(self.tenant_id("alpha")))

    def test_the_verified_owner_cannot_be_removed_while_only_a_phantom_remains(self):
        self.onboard("alpha")
        alpha = self.tenant_id("alpha")
        real = self.membership_id(alpha, self.auth_admin.users["owner-alpha" + DOMAIN].id)
        phantom = self.phantom_owner(alpha)
        for path, data in (("status", {"status": "disabled"}), ("role", {"role": "staff"}),
                           ("remove", {"confirm": "revoke"})):
            page = self.follow(self.post(f"/tenants/{alpha}/members/{real}/{path}", data))
            self.assertIn("at least one active owner with a verified account", page, path)
        self.assertEqual(_rows("select role, status from public.tenant_users where id = %s", (real,)),
                         [("owner", "active")])
        # The phantom itself can be retired.
        page = self.follow(self.post(f"/tenants/{alpha}/members/{phantom}/remove", {"confirm": "revoke"}))
        self.assertIn("Tenant access revoked", page)

    def membership_id(self, tenant_id, user_id):
        return str(_rows("select id from public.tenant_users where tenant_id = %s and user_id = %s",
                         (tenant_id, user_id))[0][0])

    def test_unverified_user_ids_are_refused_without_supabase_administration(self):
        self.onboard("alpha")
        alpha = self.tenant_id("alpha")
        app = create_app(_settings(supabase_service_role_key=None, public_url=None),
                         authenticator=self.authenticator)
        client = app.test_client()
        self.login(client)
        response = self.post(f"/tenants/{alpha}/members", {"user_id": str(uuid.uuid4()), "role": "owner"},
                             client=client)
        self.assertIn("cannot be verified", self.follow(response, client))
        self.assertEqual(len(self.members(alpha)), 1)

    def test_unknown_user_ids_are_refused_with_supabase_administration(self):
        self.onboard("alpha")
        alpha = self.tenant_id("alpha")
        page = self.follow(self.post(f"/tenants/{alpha}/members", {"user_id": str(uuid.uuid4()), "role": "owner"}))
        self.assertIn("No Supabase Auth account has that user id", page)
        self.assertEqual(len(self.members(alpha)), 1)

    def test_legacy_create_page_cannot_assign_an_owner(self):
        response = self.post("/tenants/new", {"name": "Phantom Co", "slug": SLUG_PREFIX + "phantom",
                                              "owner_user_id": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.tenant_id("phantom"))


# ---------------------------------------------------------------------------
# M4: orphaned invitations
# ---------------------------------------------------------------------------
class OrphanedAccountTests(OnboardingTestCase):
    def test_an_orphaned_account_is_listed_and_linked_without_a_second_account(self):
        self.onboard("alpha")
        alpha = self.tenant_id("alpha")
        response = self.onboard("beta", plan_id=str(uuid.uuid4()))  # provisioning fails after the invite
        self.assertGreaterEqual(response.status_code, 400)
        orphan = self.auth_admin.users["owner-beta" + DOMAIN]
        page = self.client.get("/members", query_string={"view": "unattached"}).get_data(as_text=True)
        self.assertIn("owner-beta" + DOMAIN, page)
        self.assertNotIn("owner-alpha" + DOMAIN, page, "accounts with a membership are not orphans")
        searched = self.client.get("/members", query_string={"view": "unattached", "q": "nobody"}).get_data(
            as_text=True)
        self.assertNotIn("owner-beta" + DOMAIN, searched)

        response = self.post("/members/link", {"tenant_id": alpha, "email": orphan.email,
                                                "user_id": orphan.id, "role": "staff"})
        self.assertTrue(response.headers["Location"].endswith("/members?view=unattached"))
        self.assertIn(f"Added {orphan.email} as staff", self.follow(response))
        self.assertIn((orphan.id, "staff", "active"), [m[1:] for m in self.members(alpha)])
        self.assertEqual(len(self.auth_admin.invites), 2, "linking never invites again")
        self.assertEqual(sum(1 for u in self.auth_admin.users.values() if u.email == orphan.email), 1)
        page = self.client.get("/members", query_string={"view": "unattached"}).get_data(as_text=True)
        self.assertNotIn("<br>owner-beta" + DOMAIN + "<br>", page)

    def test_link_route_requires_a_real_tenant(self):
        response = self.post("/members/link", {"tenant_id": str(uuid.uuid4()), "email": "x" + DOMAIN,
                                                "role": "staff"})
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# M5: self-service password reset
# ---------------------------------------------------------------------------
class PasswordResetThrottleTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.onboard("alpha")
        self.onboard("beta")
        self.onboard("gamma")

    def claim(self, email, **caps):
        from saas import password_reset  # noqa: PLC0415

        return password_reset.claim(email, url=app_backend_test_url(), **caps)

    def test_only_active_members_of_open_tenants_are_eligible(self):
        self.assertTrue(self.claim("owner-alpha" + DOMAIN))
        self.assertTrue(self.claim("  OWNER-BETA" + DOMAIN.upper() + " "))
        self.assertFalse(self.claim("nobody" + DOMAIN))
        self.assertFalse(self.claim(""))
        _set_tenant_status(self.tenant_id("gamma"), "suspended")
        self.assertFalse(self.claim("owner-gamma" + DOMAIN))
        # A disabled membership is not eligible either (placeholder owner keeps the tenant valid).
        conn = _connect()
        with conn, conn.cursor() as cursor:
            cursor.execute("update public.tenants set status = 'active' where id = %s", (self.tenant_id("gamma"),))
            cursor.execute("insert into public.tenant_users (tenant_id, user_id, role, status) "
                           "values (%s, gen_random_uuid(), 'owner', 'active')", (self.tenant_id("gamma"),))
            cursor.execute("update public.tenant_users set status = 'disabled' where tenant_id = %s and user_id = %s",
                           (self.tenant_id("gamma"), self.auth_admin.users["owner-gamma" + DOMAIN].id))
        conn.close()
        self.assertFalse(self.claim("owner-gamma" + DOMAIN))

    def test_per_email_and_global_caps(self):
        results = [self.claim("owner-alpha" + DOMAIN) for _ in range(5)]
        self.assertEqual(results, [True, True, True, False, False], "built-in per-email cap is 3 an hour")
        self.assertTrue(self.claim("owner-beta" + DOMAIN, global_per_hour=4))
        self.assertFalse(self.claim("owner-gamma" + DOMAIN, global_per_hour=4), "global cap reached")
        # Callers can only lower the caps, never raise them.
        self.assertFalse(self.claim("owner-alpha" + DOMAIN, per_email_per_hour=1000))

    def test_caps_hold_across_concurrent_processes(self):
        import threading  # noqa: PLC0415

        results: list[bool] = []
        lock = threading.Lock()

        def worker():
            outcome = self.claim("owner-alpha" + DOMAIN)  # a new connection each time
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False] * 9 + [True] * 3)

    def test_requests_are_audited_and_stored_without_the_email(self):
        self.claim("owner-alpha" + DOMAIN)
        owner = self.auth_admin.users["owner-alpha" + DOMAIN]
        audit = _rows("select actor_type, entity_id, metadata from public.audit_log "
                      "where action = 'auth.password_reset_requested' and id > %s", (self.audit_baseline,))
        self.assertEqual(audit, [("system", owner.id, {"user_id": owner.id})])
        stored = _rows("select encode(email_hash, 'hex') from public.password_reset_requests")
        self.assertEqual(len(stored), 1)
        self.assertNotIn("owner", stored[0][0])
        self.assertEqual(len(stored[0][0]), 64)

    def test_client_roles_cannot_read_the_throttle_table(self):
        import psycopg  # noqa: PLC0415

        conn = psycopg.connect(app_backend_test_url())
        try:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with conn.cursor() as cursor:
                    cursor.execute("select * from public.password_reset_requests")
        finally:
            conn.close()
        grants = _rows(
            "select r.rolname from pg_roles r where r.rolname in ('anon', 'authenticated', 'app_backend', "
            "'service_role') and (has_table_privilege(r.rolname, 'public.password_reset_requests', 'SELECT') "
            "or has_table_privilege(r.rolname, 'public.password_reset_requests', 'INSERT'))")
        self.assertEqual(grants, [])
        callers = _rows(
            "select r.rolname from pg_roles r where r.rolname in ('anon', 'authenticated', 'service_role') "
            "and has_function_privilege(r.rolname, 'app.request_password_reset(text, integer, integer)', 'EXECUTE')")
        self.assertEqual(callers, [])

    def test_forgot_password_page_sends_only_for_eligible_accounts(self):
        from unittest.mock import patch  # noqa: PLC0415

        from platform_admin.routes import tenant_auth  # noqa: PLC0415

        pages = []
        portal = self.app.test_client()
        with patch.object(tenant_auth, "RESET_RESPONSE_FLOOR_SECONDS", 0):
            for email in ("owner-alpha" + DOMAIN, "nobody" + DOMAIN):
                portal.get("/t/forgot-password")
                response = portal.post("/t/forgot-password",
                                       data={"csrf_token": self.token(portal), "email": email})
                self.assertEqual(response.status_code, 200)
                pages.append(response.get_data(as_text=True))
        self.assertEqual(pages[0], pages[1])
        self.assertEqual(self.auth_admin.resets, [("owner-alpha" + DOMAIN, CALLBACK)])


if __name__ == "__main__":
    unittest.main()
