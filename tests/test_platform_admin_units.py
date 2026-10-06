"""Unit tests for the platform admin dashboard that need no database."""

from __future__ import annotations

import os
import re
import time
import unittest
import uuid
from pathlib import Path

import jwt

from platform_admin.app import create_app
from platform_admin.auth import AuthError, Authenticator, Identity, LoginRateLimiter, SupabaseAuthenticator
from platform_admin.config import Settings
from platform_admin.meta.fake import FakeAccount, FakeMetaProvider
from platform_admin.meta.provider import (
    BusinessInfo,
    MetaError,
    MetaNotConfigured,
    PhoneNumberInfo,
    TenantMetaContext,
    UnavailableMetaProvider,
    WabaInfo,
)
from platform_admin.meta.secrets import (
    CredentialScopeError,
    NullSecretResolver,
    Secret,
    SecretNotConfigured,
    StaticSecretResolver,
    assert_ref_belongs_to_tenant,
    tenant_credentials_ref,
)
from platform_admin.services import audit
from platform_admin.services import validation as v

TENANT_A = "11111111-aaaa-4aaa-8aaa-111111111111"
TENANT_B = "22222222-bbbb-4bbb-8bbb-222222222222"
SECRET_VALUE = "EAAG-super-secret-token-value"
REPO = Path(__file__).resolve().parent.parent


class SecretTests(unittest.TestCase):
    def test_secret_never_prints(self):
        secret = Secret(SECRET_VALUE)
        self.assertNotIn(SECRET_VALUE, repr(secret))
        self.assertNotIn(SECRET_VALUE, str(secret))
        self.assertNotIn(SECRET_VALUE, f"{secret}")
        self.assertEqual(secret.reveal(), SECRET_VALUE)

    def test_pointer_is_scoped_to_the_tenant(self):
        ref = tenant_credentials_ref(TENANT_A)
        self.assertEqual(ref, f"tenants/{TENANT_A}/whatsapp")
        self.assertEqual(assert_ref_belongs_to_tenant(TENANT_A, ref), ref)

    def test_another_tenants_pointer_is_rejected(self):
        with self.assertRaises(CredentialScopeError):
            assert_ref_belongs_to_tenant(TENANT_B, tenant_credentials_ref(TENANT_A))

    def test_empty_or_bare_prefix_pointer_is_rejected(self):
        for bad in (None, "", f"tenants/{TENANT_A}/", "dev/x"):
            with self.subTest(ref=bad), self.assertRaises(CredentialScopeError):
                assert_ref_belongs_to_tenant(TENANT_A, bad)

    def test_default_resolver_fails_closed(self):
        with self.assertRaises(SecretNotConfigured):
            NullSecretResolver().resolve_for_tenant(TENANT_A, tenant_credentials_ref(TENANT_A))

    def test_resolver_refuses_a_foreign_pointer_before_resolving(self):
        foreign = tenant_credentials_ref(TENANT_A)
        resolver = StaticSecretResolver({foreign: SECRET_VALUE})
        with self.assertRaises(CredentialScopeError):
            resolver.resolve_for_tenant(TENANT_B, foreign)


class FakeMetaProviderTests(unittest.TestCase):
    def setUp(self):
        self.ref_a = tenant_credentials_ref(TENANT_A)
        self.ref_b = tenant_credentials_ref(TENANT_B)
        self.secrets = StaticSecretResolver({self.ref_a: SECRET_VALUE, self.ref_b: SECRET_VALUE + "-b"})
        account_a = FakeAccount(
            businesses=[BusinessInfo("b1", "Biz A")],
            wabas=[WabaInfo("w1", "WABA A", "b1")],
            phone_numbers=[PhoneNumberInfo("p1", "w1", "+254700000010")],
        )
        self.provider = FakeMetaProvider({self.ref_a: account_a, self.ref_b: FakeAccount()}, self.secrets)

    def test_operations_name_the_tenant(self):
        ctx = TenantMetaContext(TENANT_A, self.ref_a)
        self.assertEqual(self.provider.list_businesses(ctx)[0].business_id, "b1")
        self.assertEqual(self.provider.list_wabas(ctx, "b1")[0].waba_id, "w1")
        self.assertEqual(self.provider.list_phone_numbers(ctx, "w1")[0].phone_number_id, "p1")
        self.assertEqual(self.provider.get_phone_number(ctx, "p1").display_phone_number, "+254700000010")
        self.assertTrue(all(tenant == TENANT_A for _op, tenant in self.provider.calls))

    def test_provider_refuses_another_tenants_credentials(self):
        ctx = TenantMetaContext(TENANT_B, self.ref_a)
        with self.assertRaises(CredentialScopeError):
            self.provider.list_businesses(ctx)

    def test_missing_secret_is_reported_without_a_value(self):
        provider = FakeMetaProvider({}, NullSecretResolver())
        with self.assertRaises(SecretNotConfigured):
            provider.list_businesses(TenantMetaContext(TENANT_A, self.ref_a))

    def test_results_never_contain_the_secret(self):
        ctx = TenantMetaContext(TENANT_A, self.ref_a)
        results = [
            self.provider.list_businesses(ctx),
            self.provider.list_wabas(ctx, "b1"),
            self.provider.get_waba(ctx, "w1"),
            self.provider.check_webhook(ctx, "w1"),
            self.provider.register_number(ctx, "w1", "+254700000011"),
        ]
        self.assertNotIn(SECRET_VALUE, repr(results))

    def test_unknown_asset_is_a_provider_error(self):
        with self.assertRaises(MetaError):
            self.provider.get_waba(TenantMetaContext(TENANT_A, self.ref_a), "nope")

    def test_default_provider_is_unavailable(self):
        with self.assertRaises(MetaNotConfigured):
            UnavailableMetaProvider().list_businesses(TenantMetaContext(TENANT_A, None))


class AuditRedactionTests(unittest.TestCase):
    def test_secret_like_keys_are_redacted_recursively(self):
        redacted = audit.redact({
            "access_token": SECRET_VALUE,
            "nested": {"Client_Secret": "x", "password": "y", "ok": 1},
            "items": [{"api_key": "z", "name": "n"}],
            "credentials_ref": "tenants/t/whatsapp",
        })
        self.assertEqual(redacted["access_token"], audit.REDACTED)
        self.assertEqual(redacted["nested"], {"Client_Secret": audit.REDACTED, "password": audit.REDACTED, "ok": 1})
        self.assertEqual(redacted["items"], [{"api_key": audit.REDACTED, "name": "n"}])
        self.assertEqual(redacted["credentials_ref"], "tenants/t/whatsapp")


class ValidationTests(unittest.TestCase):
    def test_json_object_accepts_objects_only(self):
        self.assertEqual(v.json_object('{"a": 1}', "Rules"), {"a": 1})
        self.assertEqual(v.json_object("", "Rules"), {})
        for bad in ("[1]", '"x"', "{bad", "3"):
            with self.subTest(value=bad), self.assertRaises(v.ServiceError):
                v.json_object(bad, "Rules")

    def test_price_rules(self):
        self.assertEqual(str(v.price("1500")), "1500.00")
        self.assertEqual(str(v.price("0")), "0.00")
        for bad in ("-1", "abc", "1.234", "NaN", "Infinity", "1e12", ""):
            with self.subTest(value=bad), self.assertRaises(v.ServiceError):
                v.price(bad)

    def test_slug_rules(self):
        self.assertEqual(v.slug("good-slug-1"), "good-slug-1")
        for bad in ("ab", "Upper", "-lead", "has space", "x" * 41):
            with self.subTest(value=bad), self.assertRaises(v.ServiceError):
                v.slug(bad)

    def test_currency_timezone_uuid(self):
        self.assertEqual(v.currency("kes"), "KES")
        with self.assertRaises(v.ServiceError):
            v.currency("KE")
        self.assertEqual(v.timezone("Africa/Nairobi"), "Africa/Nairobi")
        with self.assertRaises(v.ServiceError):
            v.timezone("Not/AZone")
        with self.assertRaises(v.ServiceError):
            v.uuid_value("not-a-uuid", "User id")
        self.assertIsNone(v.optional_uuid("  ", "Owner"))


def _token(secret, **overrides):
    claims = {
        "sub": str(uuid.uuid4()), "aud": "authenticated", "iss": "https://proj.supabase.co/auth/v1",
        "exp": int(time.time()) + 300, "email": "admin@example.com",
    }
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, secret, algorithm="HS256")


class SupabaseTokenTests(unittest.TestCase):
    SECRET = "a-long-enough-hs256-secret-for-tests-1234567890"

    def setUp(self):
        self.auth = SupabaseAuthenticator("https://proj.supabase.co", "anon-key", self.SECRET)

    def test_valid_token_yields_the_verified_subject(self):
        subject = str(uuid.uuid4())
        identity = self.auth.verify_token(_token(self.SECRET, sub=subject))
        self.assertEqual(identity, Identity(subject, "admin@example.com"))

    def test_invalid_tokens_are_rejected(self):
        cases = {
            "expired": _token(self.SECRET, exp=int(time.time()) - 10),
            "wrong audience": _token(self.SECRET, aud="anon"),
            "wrong issuer": _token(self.SECRET, iss="https://evil.example/auth/v1"),
            "bad signature": _token("another-secret-that-is-also-long-enough-123456"),
            "missing subject": _token(self.SECRET, sub=None),
            "non-uuid subject": _token(self.SECRET, sub="not-a-uuid"),
            "garbage": "not.a.jwt",
        }
        for label, token in cases.items():
            with self.subTest(case=label), self.assertRaises(AuthError):
                self.auth.verify_token(token)

    def test_unsigned_tokens_are_rejected(self):
        header = jwt.encode({"sub": str(uuid.uuid4())}, key=None, algorithm="none")
        with self.assertRaises(AuthError):
            self.auth.verify_token(header)


class RateLimiterTests(unittest.TestCase):
    def test_blocks_after_repeated_failures_and_resets(self):
        limiter = LoginRateLimiter(attempts=3, window_seconds=60)
        for _ in range(3):
            self.assertTrue(limiter.allow("k"))
            limiter.record_failure("k")
        self.assertFalse(limiter.allow("k"))
        self.assertTrue(limiter.allow("other"))
        limiter.reset("k")
        self.assertTrue(limiter.allow("k"))


class _AcceptAll(Authenticator):
    def login(self, email, password):
        return Identity(str(uuid.uuid4()), email)


def _settings(**overrides):
    return Settings(secret_key="s" * 40, cookie_secure=False, **overrides)


class AppSecurityTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_settings(), authenticator=_AcceptAll())
        self.client = self.app.test_client()

    def test_every_admin_page_redirects_anonymous_users_to_login(self):
        for path in ("/", "/tenants", "/tenants/new", f"/tenants/{TENANT_A}", f"/tenants/{TENANT_A}/catalogue",
                     f"/tenants/{TENANT_A}/whatsapp", f"/tenants/{TENANT_A}/audit"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_login_page_is_the_only_public_page(self):
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_post_without_csrf_token_is_rejected(self):
        for path in ("/login", "/logout", f"/tenants/{TENANT_A}/status", "/tenants/new"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, data={"email": "a@b.c"}).status_code, 400)

    def test_post_with_wrong_csrf_token_is_rejected(self):
        self.client.get("/login")
        response = self.client.post("/login", data={"csrf_token": "wrong", "email": "a@b.c", "password": "x"})
        self.assertEqual(response.status_code, 400)

    def test_forged_session_without_expiry_is_not_trusted(self):
        with self.client.session_transaction() as session:
            session["user_id"] = str(uuid.uuid4())
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_security_headers_and_cookie_flags(self):
        response = self.client.get("/login")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_secure_cookie_flag_is_the_default(self):
        app = create_app(Settings(secret_key="s" * 40), authenticator=_AcceptAll())
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])

    def test_unconfigured_authentication_cannot_log_anyone_in(self):
        app = create_app(_settings())
        client = app.test_client()
        token = self._csrf(client)
        response = client.post("/login", data={"csrf_token": token, "email": "a@b.c", "password": "x"})
        self.assertEqual(response.status_code, 401)

    @staticmethod
    def _csrf(client):
        page = client.get("/login").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


class ConfigTests(unittest.TestCase):
    def test_secret_key_is_required(self):
        saved = os.environ.pop("PLATFORM_SECRET_KEY", None)
        try:
            with self.assertRaises(RuntimeError):
                Settings.from_env()
            os.environ["PLATFORM_SECRET_KEY"] = "short"
            with self.assertRaises(RuntimeError):
                Settings.from_env()
        finally:
            os.environ.pop("PLATFORM_SECRET_KEY", None)
            if saved is not None:
                os.environ["PLATFORM_SECRET_KEY"] = saved


class GenericCodeTests(unittest.TestCase):
    """Onboarding is data: no source file may name a specific business."""

    def test_dashboard_and_platform_code_name_no_business(self):
        roots = [REPO / "platform_admin", REPO / "saas" / "platform.py", REPO / "saas" / "tenant_admin.py",
                 REPO / "db" / "migrations" / "0019_platform_admins.sql",
                 REPO / "db" / "migrations" / "0020_tenant_draft_status_and_channel_guard.sql",
                 REPO / "db" / "migrations" / "0021_platform_provisioning.sql",
                 REPO / "db" / "migrations" / "0022_channel_credentials_ref_scope.sql"]
        offenders = []
        for root in roots:
            files = [root] if root.is_file() else [p for p in root.rglob("*") if p.suffix in {".py", ".html", ".css"}]
            for path in files:
                if re.search(r"cindy|bakes|bakery", path.read_text(encoding="utf-8"), re.I):
                    offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [])

    def test_no_sql_in_routes(self):
        source = (REPO / "platform_admin" / "routes" / "__init__.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\b(select|insert into|update|delete from)\b\s", source, re.I))

    def test_no_raw_token_storage_in_schema_or_code(self):
        for path in (REPO / "platform_admin").rglob("*.py"):
            self.assertNotRegex(path.read_text(encoding="utf-8"), r"access_token\s*=\s*['\"]")


if __name__ == "__main__":
    unittest.main()
