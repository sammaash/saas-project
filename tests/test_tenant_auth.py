"""Local tests for the tenant-portal authentication MVP (Stage 7C).

Everything runs against stubbed database access and fake authenticators; no live
database is contacted and no SQL is executed anywhere. Migration 0038 is validated
as text only (it is created as a file and never applied by these tests).
"""

from __future__ import annotations

import json
import re
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from platform_admin.app import create_app
from platform_admin.auth import AuthError, Authenticator, Identity, LoginRateLimiter
from platform_admin.config import Settings
from platform_admin.routes import tenant_auth as tenant_auth_module
from saas import tenant_membership

REPO = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO / "db" / "migrations"

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())

GENERIC_ERROR = "Invalid email or password."


def _settings(**overrides):
    return Settings(secret_key="s" * 40, cookie_secure=False, **overrides)


class _AcceptAll(Authenticator):
    def login(self, email, password):
        return Identity(str(uuid.uuid4()), email)


class _RejectAll(Authenticator):
    def login(self, email, password):
        raise AuthError("bad")


class _TenantAppTestCase(unittest.TestCase):
    """Shared helpers: seeded sessions, CSRF extraction, default test app."""

    def setUp(self):
        tenant_auth_module._TENANT_LIMITER = LoginRateLimiter()
        # No test may open a real database connection; membership lookups are
        # always patched explicitly, this guard makes any miss fail loudly.
        self._no_db = patch(
            "saas.tenant_membership.connect",
            side_effect=AssertionError("tests must not touch a database"),
        )
        self._no_db.start()
        self.addCleanup(self._no_db.stop)
        self.app = create_app(_settings(), authenticator=_AcceptAll())
        self.client = self.app.test_client()

    def _seed_tenant_session(self, *, user_id=None, exp=None):
        with self.client.session_transaction() as session:
            session["t_user_id"] = user_id or str(uuid.uuid4())
            session["t_email"] = "member@example.test"
            session["t_exp"] = 2**40 if exp is None else exp

    def _seed_admin_session(self):
        with self.client.session_transaction() as session:
            session["user_id"] = "admin-1"
            session["email"] = "admin@example.test"
            session["exp"] = 2**40

    def _csrf(self, path="/t/login"):
        # A valid token is one the session already knows: install a known value.
        token = "test-csrf-token"
        with self.client.session_transaction() as session:
            session["csrf_token"] = token
        return token

    @staticmethod
    def _error_text(response):
        match = re.search(
            r'<p class="flash error">(.*?)</p>', response.get_data(as_text=True), re.S
        )
        return match.group(1).strip() if match else None


class SessionNamespaceTests(_TenantAppTestCase):
    """The ``t_*`` tenant namespace and the admin namespace never collide."""

    def test_tenant_login_preserves_admin_session_keys(self):
        self._seed_admin_session()
        with patch(
            "saas.tenant_membership.list_my_active_tenants",
            return_value=[{"tenant_id": TENANT_A, "role": "owner"}],
        ):
            response = self.client.post(
                "/t/login",
                data={
                    "email": "owner@example.test",
                    "password": "pw",
                    "csrf_token": self._csrf("/t/login"),
                },
            )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("user_id"), "admin-1")
            self.assertEqual(session.get("email"), "admin@example.test")
            self.assertEqual(session.get("exp"), 2**40)
            self.assertTrue(session.get("t_user_id"))
            self.assertEqual(session.get("t_tenant_id"), TENANT_A)

    def test_tenant_logout_only_clears_tenant_keys(self):
        self._seed_admin_session()
        self._seed_tenant_session()
        response = self.client.post("/t/logout", data={"csrf_token": self._csrf("/t/")})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/t/login"))
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("t_user_id"))
            self.assertIsNone(session.get("t_tenant_id"))
            self.assertEqual(session.get("user_id"), "admin-1")
            self.assertEqual(session.get("exp"), 2**40)

    def test_tenant_login_rotates_the_csrf_token(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "pre-login-token"
        with patch(
            "saas.tenant_membership.list_my_active_tenants",
            return_value=[{"tenant_id": TENANT_A, "role": "staff"}],
        ):
            self.client.post(
                "/t/login",
                data={
                    "email": "member@example.test",
                    "password": "pw",
                    "csrf_token": "pre-login-token",
                },
            )
        with self.client.session_transaction() as session:
            self.assertNotEqual(session.get("csrf_token"), "pre-login-token")

    def test_no_supabase_token_is_ever_stored_in_the_session(self):
        with patch(
            "saas.tenant_membership.list_my_active_tenants",
            return_value=[{"tenant_id": TENANT_A, "role": "owner"}],
        ):
            self.client.post(
                "/t/login",
                data={
                    "email": "owner@example.test",
                    "password": "pw",
                    "csrf_token": self._csrf("/t/login"),
                },
            )
        with self.client.session_transaction() as session:
            self.assertLessEqual(
                set(session.keys()),
                {"csrf_token", "t_user_id", "t_email", "t_exp", "t_tenant_id"},
            )
            for value in session.values():
                self.assertNotIn("eyJ", str(value))  # no JWT payload shape

    def test_admin_login_flow_is_unchanged_and_still_works(self):
        """Regression: the admin flow keeps its historical ``session.clear``."""
        self._seed_tenant_session()
        token = self._csrf("/login")
        with patch("saas.platform.is_platform_admin", return_value=True):
            response = self.client.post(
                "/login",
                data={
                    "email": "admin@example.test",
                    "password": "pw",
                    "csrf_token": token,
                },
            )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertIsNotNone(session.get("user_id"))
            self.assertIsNone(session.get("t_user_id"))  # unchanged admin semantics

    def test_tenant_code_never_calls_session_clear(self):
        for name in ("tenant_auth.py", "tenant_dashboard.py"):
            source = (REPO / "platform_admin" / "routes" / name).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("session.clear()", source, name)


class LoginSemanticsTests(_TenantAppTestCase):
    def _login(self, email="member@example.test", password="pw"):
        return self.client.post(
            "/t/login",
            data={
                "email": email,
                "password": password,
                "csrf_token": self._csrf("/t/login"),
            },
        )

    def test_single_membership_signs_in_and_selects_its_tenant(self):
        with patch(
            "saas.tenant_membership.list_my_active_tenants",
            return_value=[{"tenant_id": TENANT_A, "role": "owner"}],
        ) as listing:
            response = self._login()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/t/{TENANT_A}/"))
        self.assertTrue(listing.called)
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("t_tenant_id"), TENANT_A)
            self.assertTrue(session.get("t_user_id"))

    def test_multiple_memberships_offer_the_picker(self):
        memberships = [
            {"tenant_id": TENANT_A, "role": "owner"},
            {"tenant_id": TENANT_B, "role": "staff"},
        ]
        with patch(
            "saas.tenant_membership.list_my_active_tenants", return_value=memberships
        ):
            response = self._login()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/t/"))
        with patch(
            "saas.tenant_membership.list_my_active_tenants", return_value=memberships
        ):
            page = self.client.get("/t/")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn(TENANT_A, body)
        self.assertIn(TENANT_B, body)

    def test_zero_memberships_gets_the_same_generic_error_as_a_wrong_password(self):
        with patch("saas.tenant_membership.list_my_active_tenants", return_value=[]):
            no_membership = self._login()
        rejected_client = create_app(
            _settings(), authenticator=_RejectAll()
        ).test_client()
        with rejected_client.session_transaction() as session:
            session["csrf_token"] = "tok"
        bad_password = rejected_client.post(
            "/t/login",
            data={"email": "member@example.test", "password": "pw", "csrf_token": "tok"},
        )
        self.assertEqual(no_membership.status_code, 401)
        self.assertEqual(bad_password.status_code, 401)
        self.assertEqual(self._error_text(no_membership), GENERIC_ERROR)
        self.assertEqual(self._error_text(bad_password), GENERIC_ERROR)

    def test_login_errors_never_reveal_account_or_membership_state(self):
        with patch("saas.tenant_membership.list_my_active_tenants", return_value=[]):
            response = self._login(email="someone@example.test")
        body = response.get_data(as_text=True).lower()
        for hint in ("membership", "not found", "no tenant", "invited", "disabled"):
            self.assertNotIn(hint, body)

    def test_platform_admin_without_tenant_membership_cannot_sign_in(self):
        with patch("saas.tenant_membership.list_my_active_tenants", return_value=[]):
            response = self._login(email="admin@example.test")
        self.assertEqual(response.status_code, 401)
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("t_user_id"))

    def test_database_failure_during_membership_listing_fails_closed(self):
        with patch(
            "saas.tenant_membership.list_my_active_tenants",
            side_effect=RuntimeError("db down"),
        ):
            response = self._login()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("db down", response.get_data(as_text=True))

    def test_rate_limited_signin_returns_429_without_user_detail(self):
        with patch(
            "platform_admin.routes.tenant_auth._TENANT_LIMITER.allow", return_value=False
        ):
            response = self._login()
        self.assertEqual(response.status_code, 429)

    def test_signin_post_requires_csrf(self):
        response = self.client.post("/t/login", data={"email": "a@b.c", "password": "pw"})
        self.assertEqual(response.status_code, 400)


class AuthorizationTests(_TenantAppTestCase):
    """Per-request membership, role and expiry checks on every protected route."""

    def setUp(self):
        super().setUp()
        self._seed_tenant_session()

    def test_member_routes_allow_active_members(self):
        with patch(
            "saas.tenant_membership.membership_for", return_value={"role": "staff"}
        ), patch(
            "platform_admin.routes.tenant_dashboard.quotations_service.list_quotations",
            return_value=[],
        ):
            response = self.client.get(f"/t/{TENANT_A}/quotations")
        self.assertEqual(response.status_code, 200)

    def test_member_routes_deny_non_members(self):
        with patch("saas.tenant_membership.membership_for", return_value=None):
            response = self.client.get(f"/t/{TENANT_A}/quotations")
        self.assertEqual(response.status_code, 403)

    def test_non_active_memberships_are_denied(self):
        # ``membership_for`` returns None for invited/disabled rows: the SQL
        # predicates only accept ``status = 'active'`` (see MigrationSQLTests).
        for label in ("invited", "disabled", "suspended"):
            with self.subTest(status=label), patch(
                "saas.tenant_membership.membership_for", return_value=None
            ):
                response = self.client.get(f"/t/{TENANT_A}/quotations")
            self.assertEqual(response.status_code, 403)

    def test_owner_routes_deny_staff(self):
        with patch(
            "saas.tenant_membership.membership_for", return_value={"role": "staff"}
        ):
            response = self.client.get(f"/t/{TENANT_A}/settings")
        self.assertEqual(response.status_code, 403)

    def test_owner_routes_allow_owners(self):
        with patch(
            "saas.tenant_membership.membership_for", return_value={"role": "owner"}
        ), patch(
            "platform_admin.routes.tenant_dashboard.tenants_service.get_profile",
            return_value={
                "tenant": {"id": TENANT_A, "name": "T", "slug": "t", "status": "active"},
                "settings": {"timezone": "UTC", "currency": "USD"},
            },
        ):
            response = self.client.get(f"/t/{TENANT_A}/settings")
        self.assertEqual(response.status_code, 200)

    def test_cross_tenant_url_is_denied_whatever_the_session_tenant_says(self):
        with self.client.session_transaction() as session:
            session["t_tenant_id"] = TENANT_A  # "selected" tenant A
        with patch("saas.tenant_membership.membership_for", return_value=None) as check:
            response = self.client.get(f"/t/{TENANT_B}/quotations")
        self.assertEqual(response.status_code, 403)
        # The server-side check ran against the URL's tenant, not the session's.
        self.assertEqual(check.call_args[0][1], TENANT_B)

    def test_expired_session_redirects_to_login_and_clears(self):
        self._seed_tenant_session(exp=1)  # long past
        response = self.client.get(f"/t/{TENANT_A}/quotations")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/t/login"))
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("t_user_id"))

    def test_forged_session_without_expiry_is_not_trusted(self):
        with self.client.session_transaction() as session:
            session["t_user_id"] = str(uuid.uuid4())
            session.pop("t_exp", None)
        response = self.client.get(f"/t/{TENANT_A}/quotations")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/t/login"))

    def test_anonymous_users_are_redirected_to_tenant_login(self):
        self.client.post("/t/logout", data={"csrf_token": self._csrf()})  # drop seeded session
        for path in ("/t/", "/t/select", f"/t/{TENANT_A}/", f"/t/{TENANT_A}/catalogue",
                     f"/t/{TENANT_A}/enquiries", f"/t/{TENANT_A}/quotations",
                     f"/t/{TENANT_A}/handovers", f"/t/{TENANT_A}/settings"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].endswith("/t/login"))

    def test_membership_database_failure_denies_access(self):
        with patch(
            "saas.tenant_membership.membership_for", side_effect=RuntimeError("db down")
        ):
            response = self.client.get(f"/t/{TENANT_A}/quotations")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("db down", response.get_data(as_text=True))

    def test_invalid_tenant_id_in_url_is_rejected(self):
        response = self.client.get("/t/not-a-uuid/quotations")
        self.assertEqual(response.status_code, 404)


class TenantSelectionTests(_TenantAppTestCase):
    def setUp(self):
        super().setUp()
        self._seed_tenant_session()

    def _select(self, tenant_id, token=None):
        return self.client.post(
            "/t/select",
            data={
                "tenant_id": tenant_id,
                "csrf_token": token or self._csrf("/t/select"),
            },
        )

    def test_select_sets_the_session_tenant_after_validation(self):
        with patch(
            "saas.tenant_membership.membership_for", return_value={"role": "staff"}
        ) as check:
            response = self._select(TENANT_A)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/t/{TENANT_A}/"))
        self.assertEqual(check.call_args[0][1], TENANT_A)
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("t_tenant_id"), TENANT_A)

    def test_select_for_another_tenant_is_denied_and_changes_nothing(self):
        with self.client.session_transaction() as session:
            session["t_tenant_id"] = TENANT_A
        with patch("saas.tenant_membership.membership_for", return_value=None):
            response = self._select(TENANT_B)
        self.assertEqual(response.status_code, 403)
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("t_tenant_id"), TENANT_A)  # unchanged

    def test_select_with_invalid_tenant_id_is_rejected(self):
        with patch("saas.tenant_membership.membership_for") as check:
            response = self._select("not-a-uuid")
        self.assertEqual(response.status_code, 400)
        check.assert_not_called()

    def test_select_requires_csrf(self):
        response = self.client.post("/t/select", data={"tenant_id": TENANT_A})
        self.assertEqual(response.status_code, 400)

    def test_home_revoked_membership_clears_the_session(self):
        with patch("saas.tenant_membership.list_my_active_tenants", return_value=[]):
            response = self.client.get("/t/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/t/login"))
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("t_user_id"))

    def test_home_with_one_membership_goes_straight_to_the_tenant(self):
        with patch(
            "saas.tenant_membership.list_my_active_tenants",
            return_value=[{"tenant_id": TENANT_A, "role": "staff"}],
        ):
            response = self.client.get("/t/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/t/{TENANT_A}/"))


class DashboardReadTests(_TenantAppTestCase):
    PROFILE = {
        "tenant": {"id": TENANT_A, "name": "Parts Co", "slug": "parts", "status": "active"},
        "settings": {"timezone": "UTC", "currency": "USD"},
    }

    def setUp(self):
        super().setUp()
        self._seed_tenant_session()

    def _member(self, role="staff"):
        return patch("saas.tenant_membership.membership_for", return_value={"role": role})

    def test_overview_renders(self):
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.tenants_service.get_profile",
            return_value=self.PROFILE,
        ), patch(
            "platform_admin.routes.tenant_dashboard.handover_service.status_summary",
            return_value={"pending": 1, "resolved": 2},
        ):
            response = self.client.get(f"/t/{TENANT_A}/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Parts Co", response.get_data(as_text=True))

    def test_catalogue_renders(self):
        products = [
            {
                "id": str(uuid.uuid4()),
                "name": "Widget",
                "description": None,
                "category": None,
                "is_active": True,
                "variants": [
                    {"name": "Large", "sku": "W-L", "price": 10,
                     "availability": "in_stock", "stock_quantity": 3, "is_active": True}
                ],
            }
        ]
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.catalogue_service.list_catalogue",
            return_value=products,
        ):
            response = self.client.get(f"/t/{TENANT_A}/catalogue")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Widget", response.get_data(as_text=True))

    def test_enquiries_render(self):
        rows = [
            {"id": str(uuid.uuid4()), "name": "Customer", "wa_id": "123",
             "email": None, "last_interaction_at": None}
        ]
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.list_customers",
            return_value=(rows, 1),
        ):
            response = self.client.get(f"/t/{TENANT_A}/enquiries")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Customer", response.get_data(as_text=True))

    def test_enquiry_detail_and_conversation_render(self):
        customer_id = str(uuid.uuid4())
        conversation_id = str(uuid.uuid4())
        customer = {"id": customer_id, "name": "Customer", "wa_id": "123", "email": None}
        conversation = {
            "id": conversation_id, "customer_id": customer_id, "status": "open",
            "created_at": "2026-01-01", "last_message_at": None,
        }
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_customer",
            return_value=customer,
        ), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.list_conversations",
            return_value=([conversation], 1),
        ):
            detail = self.client.get(f"/t/{TENANT_A}/enquiries/{customer_id}")
        self.assertEqual(detail.status_code, 200)
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_conversation",
            return_value=conversation,
        ), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.list_messages",
            return_value=(
                [{"created_at": "2026-01-01", "direction": "inbound", "sender_type": "customer",
                  "body_text": "hello", "delivery_status": "received"}],
                1,
            ),
        ):
            thread = self.client.get(
                f"/t/{TENANT_A}/enquiries/{customer_id}/conversations/{conversation_id}"
            )
        self.assertEqual(thread.status_code, 200)
        self.assertIn("hello", thread.get_data(as_text=True))

    def test_unknown_customer_is_404(self):
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_customer",
            return_value=None,
        ):
            response = self.client.get(f"/t/{TENANT_A}/enquiries/{uuid.uuid4()}")
        self.assertEqual(response.status_code, 404)

    def test_conversation_of_another_customer_is_404(self):
        conversation_id = str(uuid.uuid4())
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_conversation",
            return_value={"id": conversation_id, "customer_id": str(uuid.uuid4()),
                          "status": "open", "created_at": "2026-01-01"},
        ):
            response = self.client.get(
                f"/t/{TENANT_A}/enquiries/{uuid.uuid4()}/conversations/{conversation_id}"
            )
        self.assertEqual(response.status_code, 404)

    def test_quotations_render(self):
        quotation_id = str(uuid.uuid4())
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.quotations_service.list_quotations",
            return_value=[
                {"id": quotation_id, "quotation_number": "Q-0001", "customer_name": "C",
                 "issue_date": "2026-01-01", "expiry_date": None, "currency": "USD",
                 "total": 10, "status": "issued"}
            ],
        ):
            response = self.client.get(f"/t/{TENANT_A}/quotations")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Q-0001", response.get_data(as_text=True))

    def test_quotation_detail_renders(self):
        quotation_id = str(uuid.uuid4())
        quotation = {
            "id": quotation_id, "quotation_number": "Q-0001", "customer_name": "C",
            "customer_contact": None, "issue_date": "2026-01-01", "expiry_date": None,
            "currency": "USD", "subtotal": 10, "tax_rate": 0, "tax_amount": 0,
            "total": 10, "tax_treatment": None, "terms": None, "status": "issued",
            "business_name": "Parts Co",
            "lines": [{"sku": "W-L", "description": "Widget", "quantity": 1,
                       "unit_price": 10, "subtotal": 10}],
        }
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.quotations_service.get_quotation",
            return_value=quotation,
        ):
            response = self.client.get(f"/t/{TENANT_A}/quotations/{quotation_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Q-0001", response.get_data(as_text=True))

    def test_handovers_render(self):
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.handover_service.tenant_requests",
            return_value=[
                {"customer_name": "C", "requested_product": "Widget",
                 "reason": "custom_order", "status": "pending", "created_at": "2026-01-01"}
            ],
        ), patch(
            "platform_admin.routes.tenant_dashboard.handover_service.status_summary",
            return_value={"pending": 1},
        ):
            response = self.client.get(f"/t/{TENANT_A}/handovers")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Widget", response.get_data(as_text=True))

    def test_service_database_failure_returns_a_safe_error(self):
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.quotations_service.list_quotations",
            side_effect=RuntimeError("relation quotations does not exist"),
        ):
            response = self.client.get(f"/t/{TENANT_A}/quotations")
        self.assertEqual(response.status_code, 500)
        body = response.get_data(as_text=True)
        self.assertIn("Something went wrong.", body)
        for leak in ("Traceback", "relation", "psycopg", "sqlstate"):
            self.assertNotIn(leak, body)


class _Column:
    def __init__(self, name):
        self.name = name


class _StubCursor:
    """Scripted cursor: every execute() consumes the next scripted result."""

    def __init__(self, results):
        self._results = list(results)
        self.executed = []
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        result = (
            self._results.pop(0)
            if self._results
            else {"columns": ("ok",), "rows": [("ok",)]}
        )
        if isinstance(result, Exception):
            raise result
        self.description = [_Column(name) for name in result["columns"]]
        self._rows = list(result["rows"])

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _StubConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class MembershipLookupTests(unittest.TestCase):
    """``saas.tenant_membership`` passes only the verified subject via trusted context."""

    def _run(self, results, call):
        cursor = _StubCursor(results)
        connection = _StubConnection(cursor)
        with patch("saas.tenant_membership.connect", return_value=connection):
            value = call()
        return value, cursor, connection

    def test_membership_for_owner_staff_and_denial(self):
        cases = [
            ({"columns": ("m", "o"), "rows": [(True, True)]}, {"role": "owner"}),
            ({"columns": ("m", "o"), "rows": [(True, False)]}, {"role": "staff"}),
            ({"columns": ("m", "o"), "rows": [(False, False)]}, None),
            ({"columns": ("m", "o"), "rows": [(None, None)]}, None),  # fail closed
        ]
        for result, expected in cases:
            with self.subTest(expected=expected):
                value, _, _ = self._run(
                    [{"columns": ("ok",), "rows": [("ok",)]}, result],  # set_config, then check
                    lambda: tenant_membership.membership_for("user-1", TENANT_A),
                )
                self.assertEqual(value, expected)

    def test_membership_for_sends_the_verified_subject_as_trusted_context(self):
        _, cursor, _ = self._run(
            [
                {"columns": ("ok",), "rows": [("ok",)]},
                {"columns": ("m", "o"), "rows": [(True, False)]},
            ],
            lambda: tenant_membership.membership_for("user-1", TENANT_A),
        )
        claims_sql, claims_params = cursor.executed[0]
        self.assertIn("request.jwt.claims", claims_sql)
        self.assertEqual(json.loads(claims_params[0]), {"sub": "user-1"})
        predicate_sql, predicate_params = cursor.executed[1]
        self.assertIn("app.is_tenant_member", predicate_sql)
        self.assertEqual(predicate_params, (TENANT_A, TENANT_A))

    def test_membership_for_requires_a_user_id(self):
        with patch("saas.tenant_membership.connect") as connect:
            with self.assertRaises(ValueError):
                tenant_membership.membership_for("", TENANT_A)
            connect.assert_not_called()

    def test_database_errors_propagate_to_fail_closed(self):
        cursor = _StubCursor(
            [{"columns": ("ok",), "rows": [("ok",)]}, RuntimeError("db down")]
        )
        connection = _StubConnection(cursor)
        with patch("saas.tenant_membership.connect", return_value=connection):
            with self.assertRaises(RuntimeError):
                tenant_membership.membership_for("user-1", TENANT_A)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_list_my_active_tenants_normalizes_ids_to_strings(self):
        other = uuid.uuid4()
        value, _, connection = self._run(
            [
                {"columns": ("ok",), "rows": [("ok",)]},
                {
                    "columns": ("tenant_id", "role"),
                    "rows": [(uuid.UUID(TENANT_A), "owner"), (other, "staff")],
                },
            ],
            lambda: tenant_membership.list_my_active_tenants("user-1"),
        )
        self.assertEqual(
            value,
            [
                {"tenant_id": TENANT_A, "role": "owner"},
                {"tenant_id": str(other), "role": "staff"},
            ],
        )
        self.assertTrue(connection.committed and connection.closed)


class MigrationSQLTests(unittest.TestCase):
    """Migration 0038 is a local file only; its structure and privilege model are
    validated as text. Nothing here connects to a database."""

    def setUp(self):
        self.path = MIGRATIONS / "0038_my_active_tenants.sql"
        self.sql = self.path.read_text(encoding="utf-8")
        self.lower = self.sql.lower()

    def test_migration_is_a_new_last_file_with_the_naming_convention(self):
        files = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
        self.assertIn("0038_my_active_tenants.sql", files)
        self.assertRegex(files[-1], r"^\d{4}_[a-z0-9_]+\.sql$")

    def test_function_takes_no_caller_supplied_user_id(self):
        match = re.search(
            r"create or replace function app\.my_active_tenants\(([^)]*)\)", self.sql
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).strip(), "")
        self.assertNotRegex(self.lower, r"function app\.my_active_tenants\(\s*user_id")

    def test_returns_only_active_memberships_of_the_jwt_subject(self):
        self.assertIn("returns table (tenant_id uuid, role text)", self.lower)
        self.assertIn("app.current_user_id()", self.sql)
        self.assertIn("tu.status = 'active'", self.sql)
        self.assertIn("from public.tenant_users tu", self.sql)

    def test_security_definer_with_pinned_search_path_and_qualified_references(self):
        self.assertIn("security definer", self.lower)
        self.assertIn("set search_path = pg_catalog, public, pg_temp", self.lower)
        self.assertIn("public.tenant_users", self.sql)  # fully qualified

    def test_execution_is_restricted_to_app_backend(self):
        self.assertRegex(
            self.lower, r"revoke all on function app\.my_active_tenants\(\) from public;"
        )
        self.assertIn(
            "revoke execute on function app.my_active_tenants() from anon, authenticated;",
            self.lower,
        )
        self.assertIn(
            "grant execute on function app.my_active_tenants() to app_backend;", self.lower
        )
        self.assertNotIn("my_active_tenants() to authenticated", self.lower)
        self.assertNotIn("my_active_tenants() to anon", self.lower)

    def test_ownership_follows_the_provisioner_function_pattern(self):
        self.assertIn(
            "alter function app.my_active_tenants() owner to saas_platform_provisioner;",
            self.lower,
        )

    def test_provisioner_privileges_support_that_ownership(self):
        hardening = next(MIGRATIONS.glob("0013_*.sql")).read_text(encoding="utf-8").lower()
        provisioning = next(MIGRATIONS.glob("0021_*.sql")).read_text(encoding="utf-8").lower()
        guard = next(MIGRATIONS.glob("0014_*.sql")).read_text(encoding="utf-8").lower()
        # The provisioner is NOLOGIN/NOINHERIT/NOBYPASSRLS and may read tenant_users.
        self.assertIn(
            "create role saas_platform_provisioner nologin noinherit nobypassrls;", hardening
        )
        self.assertRegex(
            hardening,
            r"grant select on[^;]*public\.tenant_users[^;]*to saas_platform_provisioner",
        )
        self.assertRegex(
            hardening,
            r"create policy tenant_users_platform_provisioner_select on public\.tenant_users\s+"
            r"for select to saas_platform_provisioner using \(true\)",
        )
        # It can evaluate app.current_user_id() and already owns SECURITY DEFINER helpers.
        self.assertIn(
            "grant execute on function app.current_user_id() to saas_platform_provisioner;",
            provisioning,
        )
        self.assertIn("owner to saas_platform_provisioner", guard)

    def test_membership_predicates_require_an_active_status(self):
        guard = next(MIGRATIONS.glob("0014_*.sql")).read_text(encoding="utf-8").lower()
        self.assertIn("status = 'active'", guard)  # invited/disabled never match


if __name__ == "__main__":
    unittest.main()