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
        conn.close()
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
        self.assertEqual(_rows("select count(*) from public.platform_user_directory where email = %s",
                               ("owner-eta" + DOMAIN,)), [(0,)])
        account = self.auth_admin.users["owner-eta" + DOMAIN]

        retry = self.onboard("eta", mode="link")
        self.assertEqual(retry.status_code, 302)
        self.assertEqual(len(self.auth_admin.invites), 1, "the retry must not invite again")
        self.assertEqual([m[1:] for m in self.members(self.tenant_id("eta"))],
                         [(account.id, "owner", "active")])

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


if __name__ == "__main__":
    unittest.main()
