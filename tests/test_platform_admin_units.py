"""Unit tests for the platform admin dashboard that need no database."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import unittest
import uuid
from pathlib import Path

import jwt

from platform_admin.app import create_app
from platform_admin.auth import AuthError, Authenticator, Identity, LoginRateLimiter, SupabaseAuthenticator
from platform_admin.config import ConfigError, Settings
from platform_admin.services import audit
from platform_admin.services import validation as v

TENANT_A = "11111111-aaaa-4aaa-8aaa-111111111111"
REPO = Path(__file__).resolve().parent.parent


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _fake_jwt(role: str) -> str:
    return f"{_b64({'alg': 'HS256'})}.{_b64({'role': role})}.signature"


class AuditRedactionTests(unittest.TestCase):
    def test_secret_like_keys_are_redacted_recursively(self):
        redacted = audit.redact({
            "access_token": "EAAG-secret",
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

    def test_nul_characters_are_rejected_everywhere(self):
        with self.assertRaises(v.ServiceError):
            v.json_object('{"a": "\\u0000"}', "Rules")
        with self.assertRaises(v.ServiceError):
            v.json_object('{"a": "\x00"}', "Rules")
        with self.assertRaises(v.ServiceError):
            v.text("bad\x00name", "Name")

    def test_integer_is_bounded_to_the_database_range(self):
        self.assertEqual(v.integer("5", "Sort order"), 5)
        self.assertEqual(v.integer("", "Sort order"), 0)
        for bad in ("-1", "99999999999", "x", "1.5"):
            with self.subTest(value=bad), self.assertRaises(v.ServiceError):
                v.integer(bad, "Sort order")

    def test_price_rules(self):
        self.assertEqual(str(v.price("1500")), "1500.00")
        for bad in ("-1", "abc", "1.234", "NaN", "Infinity", "1e12", ""):
            with self.subTest(value=bad), self.assertRaises(v.ServiceError):
                v.price(bad)

    def test_slug_currency_timezone_uuid(self):
        self.assertEqual(v.slug("good-slug-1"), "good-slug-1")
        for bad in ("ab", "Upper", "-lead", "has space", "x" * 41):
            with self.subTest(value=bad), self.assertRaises(v.ServiceError):
                v.slug(bad)
        self.assertEqual(v.currency("kes"), "KES")
        with self.assertRaises(v.ServiceError):
            v.currency("KE")
        with self.assertRaises(v.ServiceError):
            v.timezone("Not/AZone")
        with self.assertRaises(v.ServiceError):
            v.uuid_value("not-a-uuid", "User id")
        self.assertIsNone(v.optional_uuid("  ", "Owner"))


class DatabaseErrorMappingTests(unittest.TestCase):
    def setUp(self):
        import psycopg.errors as pg

        self.pg = pg

    def test_expected_database_errors_become_friendly_responses(self):
        cases = (
            (self.pg.NumericValueOutOfRange("out of range"), 400),
            (self.pg.UntranslatableCharacter("\\u0000 cannot be converted"), 400),
            (self.pg.InvalidTextRepresentation("bad input"), 400),
            (self.pg.InsufficientPrivilege("denied"), 403),
            (self.pg.UniqueViolation("dup"), 409),
            (self.pg.ForeignKeyViolation("fk"), 400),
            (self.pg.CheckViolation("check"), 400),
            (self.pg.NoDataFound("not found"), 404),
            (self.pg.InvalidParameterValue("status must be active"), 400),
        )
        for error, status in cases:
            with self.subTest(error=type(error).__name__):
                friendly = v.translate_db_error(error)
                self.assertIsNotNone(friendly)
                self.assertEqual(friendly.status, status)
                self.assertNotIn("\\u0000", friendly.message)

    def test_unknown_errors_are_not_translated(self):
        self.assertIsNone(v.translate_db_error(RuntimeError("boom")))
        self.assertIsNone(v.translate_db_error(self.pg.OperationalError("down")))


def _token(secret, **overrides):
    claims = {
        "sub": str(uuid.uuid4()), "aud": "authenticated", "iss": "https://proj.supabase.co/auth/v1",
        "exp": int(time.time()) + 300, "email": "admin@example.com",
    }
    claims.update(overrides)
    return jwt.encode({k: val for k, val in claims.items() if val is not None}, secret, algorithm="HS256")


class SupabaseTokenTests(unittest.TestCase):
    SECRET = "a-long-enough-hs256-secret-for-tests-1234567890"

    def setUp(self):
        self.auth = SupabaseAuthenticator("https://proj.supabase.co", "anon-key", self.SECRET)

    def test_valid_token_yields_the_verified_subject(self):
        subject = str(uuid.uuid4())
        self.assertEqual(self.auth.verify_token(_token(self.SECRET, sub=subject)),
                         Identity(subject, "admin@example.com"))

    def test_invalid_tokens_are_rejected(self):
        cases = {
            "expired": _token(self.SECRET, exp=int(time.time()) - 10),
            "wrong audience": _token(self.SECRET, aud="anon"),
            "wrong issuer": _token(self.SECRET, iss="https://evil.example/auth/v1"),
            "bad signature": _token("another-secret-that-is-also-long-enough-123456"),
            "missing subject": _token(self.SECRET, sub=None),
            "missing expiry": _token(self.SECRET, exp=None),
            "non-uuid subject": _token(self.SECRET, sub="not-a-uuid"),
            "garbage": "not.a.jwt",
        }
        for label, token in cases.items():
            with self.subTest(case=label), self.assertRaises(AuthError):
                self.auth.verify_token(token)

    def test_unsigned_tokens_are_rejected(self):
        with self.assertRaises(AuthError):
            self.auth.verify_token(jwt.encode({"sub": str(uuid.uuid4())}, key=None, algorithm="none"))

    def test_plain_http_supabase_url_is_refused(self):
        for url in ("http://proj.supabase.co", "http://localhost:54321", "ftp://x.example", "proj.supabase.co"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                SupabaseAuthenticator(url, "anon")

    def test_http_loopback_needs_the_explicit_dev_override(self):
        SupabaseAuthenticator("http://localhost:54321", "anon", allow_http_loopback=True)
        with self.assertRaises(ConfigError):
            SupabaseAuthenticator("http://proj.supabase.co", "anon", allow_http_loopback=True)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RateLimiterTests(unittest.TestCase):
    def test_blocks_after_repeated_failures_for_one_address_and_email(self):
        limiter = LoginRateLimiter(attempts=3)
        for _ in range(3):
            self.assertTrue(limiter.allow("1.1.1.1", "a@x.io"))
            limiter.record_failure("1.1.1.1", "a@x.io")
        self.assertFalse(limiter.allow("1.1.1.1", "a@x.io"))
        self.assertTrue(limiter.allow("1.1.1.1", "other@x.io"))
        self.assertTrue(limiter.allow("2.2.2.2", "a@x.io"))
        limiter.reset("1.1.1.1", "a@x.io")
        self.assertTrue(limiter.allow("1.1.1.1", "a@x.io"))

    def test_per_ip_limit_applies_across_emails(self):
        limiter = LoginRateLimiter(attempts=5, per_ip=4)
        for i in range(4):
            limiter.record_failure("9.9.9.9", f"user{i}@x.io")
        self.assertFalse(limiter.allow("9.9.9.9", "fresh@x.io"))
        self.assertTrue(limiter.allow("8.8.8.8", "fresh@x.io"))

    def test_global_cap_blocks_every_login_until_the_window_passes(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(window_seconds=100, global_failures=6, per_ip=99, clock=clock)
        for i in range(6):
            limiter.record_failure(f"10.0.0.{i}", f"u{i}@x.io")
        self.assertFalse(limiter.allow("203.0.113.1", "innocent@x.io"))
        clock.now += 101
        self.assertTrue(limiter.allow("203.0.113.1", "innocent@x.io"))

    def test_expired_entries_are_pruned(self):
        clock = FakeClock()
        limiter = LoginRateLimiter(window_seconds=100, clock=clock)
        for i in range(50):
            limiter.record_failure(f"10.0.1.{i}", "a@x.io")
        self.assertGreater(limiter.tracked_keys(), 0)
        clock.now += 1000
        limiter.allow("1.2.3.4", "a@x.io")
        self.assertEqual(limiter.tracked_keys(), 0)

    def test_memory_is_bounded_when_addresses_rotate(self):
        limiter = LoginRateLimiter(max_keys=100, global_failures=10**9, per_ip=10**9)
        for i in range(5000):
            limiter.record_failure(f"ip{i}", f"user{i}@x.io")
        self.assertLessEqual(limiter.tracked_keys(), 100)

    def test_a_successful_login_does_not_clear_other_counters(self):
        limiter = LoginRateLimiter(attempts=2)
        limiter.record_failure("1.1.1.1", "a@x.io")
        limiter.record_failure("1.1.1.1", "a@x.io")
        limiter.reset("1.1.1.1", "b@x.io")
        self.assertFalse(limiter.allow("1.1.1.1", "a@x.io"))


GOOD_ENV = {
    "PLATFORM_SECRET_KEY": "s" * 40,
    "SUPABASE_URL": "https://proj.supabase.co",
    "SUPABASE_ANON_KEY": _fake_jwt("anon"),
    "SAAS_DATABASE_URL": "postgresql://app_backend:pw@db.example.com:5432/postgres",
    "SAAS_PLATFORM_DATABASE_URL": "postgresql://saas_platform_admin:pw@db.example.com:5432/postgres",
}


class ConfigTests(unittest.TestCase):
    def env(self, **changes):
        env = dict(GOOD_ENV)
        env.update(changes)
        return {k: val for k, val in env.items() if val is not None}

    def test_valid_configuration_fails_closed_by_default(self):
        settings = Settings.from_env(self.env())
        self.assertTrue(settings.cookie_secure)
        self.assertFalse(settings.allow_active_channels)
        self.assertEqual(settings.trusted_proxies, 0)

    def test_every_required_variable_is_enforced(self):
        for name in GOOD_ENV:
            with self.subTest(missing=name):
                with self.assertRaises(ConfigError) as raised:
                    Settings.from_env(self.env(**{name: None}))
                self.assertIn(name, str(raised.exception))
            with self.subTest(blank=name), self.assertRaises(ConfigError):
                Settings.from_env(self.env(**{name: "   "}))

    def test_weak_secret_key_is_rejected(self):
        with self.assertRaises(ConfigError):
            Settings.from_env(self.env(PLATFORM_SECRET_KEY="short"))

    def test_supabase_url_must_be_https(self):
        for url in ("http://proj.supabase.co", "http://localhost:54321", "proj.supabase.co", "ftp://x.io"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                Settings.from_env(self.env(SUPABASE_URL=url))

    def test_http_loopback_supabase_needs_dev_mode(self):
        Settings.from_env(self.env(SUPABASE_URL="http://localhost:54321", PLATFORM_DEV_MODE="1"))
        with self.assertRaises(ConfigError):
            Settings.from_env(self.env(SUPABASE_URL="http://proj.supabase.co", PLATFORM_DEV_MODE="1"))

    def test_insecure_cookies_need_dev_mode(self):
        with self.assertRaises(ConfigError):
            Settings.from_env(self.env(PLATFORM_INSECURE_COOKIES="1"))
        settings = Settings.from_env(self.env(PLATFORM_INSECURE_COOKIES="1", PLATFORM_DEV_MODE="1"))
        self.assertFalse(settings.cookie_secure)

    def test_service_role_and_secret_keys_are_rejected(self):
        for key in (_fake_jwt("service_role"), "sb_secret_abc123"):
            with self.subTest(key=key[:12]), self.assertRaises(ConfigError):
                Settings.from_env(self.env(SUPABASE_ANON_KEY=key))
        Settings.from_env(self.env(SUPABASE_ANON_KEY="sb_publishable_abc123"))

    def test_privileged_database_logins_are_rejected(self):
        for user in ("service_role", "postgres", "supabase_admin", "service_role.projectref"):
            for name in ("SAAS_DATABASE_URL", "SAAS_PLATFORM_DATABASE_URL"):
                with self.subTest(user=user, name=name), self.assertRaises(ConfigError):
                    Settings.from_env(self.env(**{name: f"postgresql://{user}:pw@db.example.com/postgres"}))
        Settings.from_env(self.env(SAAS_DATABASE_URL="postgresql://app_backend.projectref:pw@pooler.example.com/postgres"))

    def test_numeric_settings_are_validated(self):
        for name, value in (("PLATFORM_SESSION_MINUTES", "0"), ("PLATFORM_SESSION_MINUTES", "x"),
                            ("PLATFORM_SESSION_MINUTES", "100000"), ("PLATFORM_TRUSTED_PROXIES", "-1"),
                            ("PLATFORM_TRUSTED_PROXIES", "9")):
            with self.subTest(name=name, value=value), self.assertRaises(ConfigError):
                Settings.from_env(self.env(**{name: value}))
        self.assertEqual(Settings.from_env(self.env(PLATFORM_TRUSTED_PROXIES="1")).trusted_proxies, 1)

    def test_activating_channels_requires_an_explicit_opt_in(self):
        self.assertTrue(Settings.from_env(self.env(PLATFORM_ALLOW_ACTIVE_CHANNELS="1")).allow_active_channels)
        self.assertFalse(Settings.from_env(self.env(PLATFORM_ALLOW_ACTIVE_CHANNELS="true")).allow_active_channels)


class _AcceptAll(Authenticator):
    def login(self, email, password):
        return Identity(str(uuid.uuid4()), email)


class _RejectAll(Authenticator):
    def login(self, email, password):
        raise AuthError("bad")


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
        for path in ("/login", "/logout", f"/tenants/{TENANT_A}/status", "/tenants/new",
                     f"/tenants/{TENANT_A}/whatsapp/connect"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, data={"email": "a@b.c"}).status_code, 400)

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
        client = create_app(_settings()).test_client()
        token = self._csrf(client)
        response = client.post("/login", data={"csrf_token": token, "email": "a@b.c", "password": "x"})
        self.assertEqual(response.status_code, 401)

    def test_unexpected_errors_return_a_generic_page_and_log_no_values(self):
        app = create_app(_settings(), authenticator=_AcceptAll())

        @app.get("/boom")
        def boom():
            raise RuntimeError("tenant-secret-value 12345")

        with self.assertLogs("platform_admin", level="ERROR") as logs:
            response = app.test_client().get("/boom")
        self.assertEqual(response.status_code, 500)
        body = response.get_data(as_text=True)
        self.assertIn("Something went wrong", body)
        self.assertNotIn("tenant-secret-value", body + "\n".join(logs.output))
        self.assertNotIn("Traceback", body + "\n".join(logs.output))

    def test_database_errors_become_friendly_responses_not_500s(self):
        import psycopg.errors as pg

        app = create_app(_settings(), authenticator=_AcceptAll())

        @app.get("/range")
        def out_of_range():
            raise pg.NumericValueOutOfRange("integer out of range: secret-parameter-value")

        response = app.test_client().get("/range")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("secret-parameter-value", response.get_data(as_text=True))

    @staticmethod
    def _csrf(client):
        page = client.get("/login").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def _failed_login_key(self, app, headers):
        client = app.test_client()
        token = self._csrf(client)
        client.post("/login", data={"csrf_token": token, "email": "a@b.c", "password": "x"}, headers=headers)
        return sorted(app.extensions["platform"].limiter._events)

    def test_forwarded_for_is_ignored_without_trusted_proxies(self):
        app = create_app(_settings(), authenticator=_RejectAll())
        keys = self._failed_login_key(app, {"X-Forwarded-For": "203.0.113.50"})
        self.assertTrue(all("203.0.113.50" not in key for key in keys))

    def test_forwarded_for_is_honoured_for_the_configured_proxy_count(self):
        app = create_app(_settings(trusted_proxies=1), authenticator=_RejectAll())
        keys = self._failed_login_key(app, {"X-Forwarded-For": "203.0.113.50"})
        self.assertIn("i:203.0.113.50", keys)

    def test_a_spoofed_extra_forwarded_hop_is_not_trusted(self):
        app = create_app(_settings(trusted_proxies=1), authenticator=_RejectAll())
        keys = self._failed_login_key(app, {"X-Forwarded-For": "198.51.100.7, 203.0.113.50"})
        self.assertIn("i:203.0.113.50", keys)
        self.assertNotIn("i:198.51.100.7", keys)


class NoMetaIntegrationTests(unittest.TestCase):
    """The platform never talks to Meta: no provider, MCP, Graph API or token handling."""

    PATTERN = re.compile(r"mcp|graph\.facebook|graph_api|MetaProvider|meta_provider|FakeMeta|SecretResolver|whatsapp_business_tools",
                         re.I)

    def _sources(self):
        for root in ("platform_admin", "saas", "db"):
            for path in (REPO / root).rglob("*"):
                if path.suffix in {".py", ".html", ".sql", ".css"} and "__pycache__" not in path.parts:
                    yield path

    def test_no_meta_or_mcp_code_remains(self):
        offenders = [str(p.relative_to(REPO)) for p in self._sources()
                     if self.PATTERN.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])

    def test_the_meta_package_is_gone(self):
        self.assertFalse((REPO / "platform_admin" / "meta").exists())

    def test_no_mcp_configuration_or_dependency(self):
        self.assertFalse((REPO / ".mcp.json").exists())
        requirements = (REPO / "requirements.txt").read_text(encoding="utf-8").lower()
        for word in ("mcp", "facebook", "meta"):
            self.assertNotIn(word, requirements)

    def test_only_the_supabase_login_handles_an_access_token(self):
        offenders = [str(p.relative_to(REPO)) for p in self._sources()
                     if p.suffix == ".py" and "access_token" in p.read_text(encoding="utf-8")
                     and p.name != "auth.py"]
        self.assertEqual(offenders, [])

    def test_the_dashboard_exposes_no_meta_routes(self):
        app = create_app(_settings(), authenticator=_AcceptAll())
        rules = [rule.rule for rule in app.url_map.iter_rules()]
        self.assertFalse([r for r in rules if "meta" in r.lower() or "discover" in r.lower()])


class GenericCodeTests(unittest.TestCase):
    """Onboarding is data: no source file may name a specific business."""

    def test_dashboard_and_platform_code_name_no_business(self):
        roots = [REPO / "platform_admin", REPO / "saas" / "platform.py", REPO / "saas" / "tenant_admin.py",
                 *sorted((REPO / "db" / "migrations").glob("00[12][0-9]_*.sql"))[-6:]]
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


if __name__ == "__main__":
    unittest.main()
