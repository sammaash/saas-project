"""Supabase Auth administration and the tenant invitation / password flows, without a network.

Every HTTP call to Supabase is intercepted (``urllib.request.urlopen`` is patched), so these
tests never create a real account and never send an email.
"""

from __future__ import annotations

import base64
import io
import json
import time
import unittest
import urllib.error
from unittest.mock import patch

from platform_admin.app import create_app
from platform_admin.auth import AuthError, Authenticator, Identity
from platform_admin.auth_admin import (
    AuthAdmin,
    AuthAdminError,
    AuthUser,
    SupabaseAuthAdmin,
    UnconfiguredAuthAdmin,
    auth_callback_url,
    build_auth_admin,
    check_password,
    normalize_email,
)
from platform_admin.config import ConfigError, Settings, validate_public_url, validate_service_role_key

BASE = "https://abcdefghijklmnopqrst.supabase.co"
PUBLIC = "https://admin.example.test"
CALLBACK = PUBLIC + "/t/auth/callback"
USER_ID = "3f0c1f9e-6c1a-4c62-9a55-1a2b3c4d5e6f"


def _jwt(claims: dict) -> str:
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{part({'alg': 'HS256'})}.{part(claims)}.signature"


SERVICE_KEY = _jwt({"role": "service_role", "ref": "abcdefghijklmnopqrst"})
ANON_KEY = _jwt({"role": "anon", "ref": "abcdefghijklmnopqrst"})


class _Response:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode() if payload is not None else b""

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(status: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://x", status, "error", {}, io.BytesIO(json.dumps(payload).encode())
    )


class RecordingTransport:
    """Stands in for urlopen: records each request and replays scripted responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode()) if request.data else None
        self.requests.append({
            "method": request.get_method(), "url": request.full_url,
            "headers": {k.lower(): v for k, v in request.header_items()}, "body": body,
        })
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _Response(response)


def _client():
    return SupabaseAuthAdmin(BASE, SERVICE_KEY, ANON_KEY, allowed_redirect_prefix=CALLBACK)


class SupabaseAuthAdminTests(unittest.TestCase):
    def test_invite_posts_to_the_invite_endpoint_with_the_service_key_server_side(self):
        transport = RecordingTransport({"id": USER_ID, "email": "owner@bakery.test",
                                        "invited_at": "2026-11-17T10:00:00Z"})
        with patch("urllib.request.urlopen", transport):
            user = _client().invite("Owner@Bakery.test ", "Ann Owner", CALLBACK)
        self.assertEqual(user.id, USER_ID)
        self.assertEqual(user.email, "owner@bakery.test")
        sent = transport.requests[0]
        self.assertEqual(sent["method"], "POST")
        self.assertTrue(sent["url"].startswith(BASE + "/auth/v1/invite?redirect_to="))
        self.assertIn("redirect_to=https%3A%2F%2Fadmin.example.test%2Ft%2Fauth%2Fcallback", sent["url"])
        self.assertEqual(sent["body"], {"email": "owner@bakery.test", "data": {"full_name": "Ann Owner"}})
        self.assertEqual(sent["headers"]["authorization"], f"Bearer {SERVICE_KEY}")

    def test_invite_refuses_a_redirect_outside_the_approved_callback(self):
        transport = RecordingTransport()
        with patch("urllib.request.urlopen", transport):
            for bad in ("https://evil.example/t/auth/callback", PUBLIC + "/elsewhere"):
                with self.assertRaises(AuthAdminError) as caught:
                    _client().invite("owner@bakery.test", None, bad)
                self.assertEqual(caught.exception.code, "bad_redirect")
        self.assertEqual(transport.requests, [], "nothing may be sent for a bad redirect")

    def test_existing_email_is_reported_as_email_exists(self):
        for payload in ({"code": 422, "error_code": "email_exists", "msg": "exists"},
                        {"code": 422, "msg": "A user with this email address has already been registered"}):
            transport = RecordingTransport(_http_error(422, payload))
            with patch("urllib.request.urlopen", transport), self.assertRaises(AuthAdminError) as caught:
                _client().invite("owner@bakery.test", None, CALLBACK)
            self.assertEqual(caught.exception.code, "email_exists")

    def test_errors_never_contain_the_service_key(self):
        cases = (_http_error(401, {"msg": "Invalid API key"}), _http_error(500, {"msg": "boom"}),
                 urllib.error.URLError("down"))
        for failure in cases:
            transport = RecordingTransport(failure)
            with patch("urllib.request.urlopen", transport), self.assertRaises(AuthAdminError) as caught:
                _client().invite("owner@bakery.test", None, CALLBACK)
            self.assertNotIn(SERVICE_KEY, str(caught.exception))
            self.assertNotIn(SERVICE_KEY, repr(caught.exception))
        self.assertNotIn(SERVICE_KEY, repr(_client()))

    def test_rate_limit_is_reported_clearly(self):
        transport = RecordingTransport(_http_error(429, {"msg": "email rate limit exceeded"}))
        with patch("urllib.request.urlopen", transport), self.assertRaises(AuthAdminError) as caught:
            _client().invite("owner@bakery.test", None, CALLBACK)
        self.assertEqual(caught.exception.code, "rate_limited")

    def test_get_user_returns_none_for_unknown_id(self):
        transport = RecordingTransport(_http_error(404, {"msg": "User not found"}))
        with patch("urllib.request.urlopen", transport):
            self.assertIsNone(_client().get_user(USER_ID))
        self.assertTrue(transport.requests[0]["url"].endswith(f"/auth/v1/admin/users/{USER_ID}"))

    def test_find_user_by_email_pages_until_found_or_exhausted(self):
        page1 = {"users": [{"id": f"00000000-0000-4000-8000-{i:012d}", "email": f"u{i}@x.test"}
                           for i in range(200)]}
        page2 = {"users": [{"id": USER_ID, "email": "Owner@Bakery.test"}]}
        transport = RecordingTransport(page1, page2)
        with patch("urllib.request.urlopen", transport):
            found = _client().find_user_by_email("owner@bakery.test")
        self.assertEqual(found.id, USER_ID)
        self.assertIn("page=2", transport.requests[1]["url"])

        transport = RecordingTransport({"users": [{"id": USER_ID, "email": "other@x.test"}]})
        with patch("urllib.request.urlopen", transport):
            self.assertIsNone(_client().find_user_by_email("owner@bakery.test"))

    def test_set_password_uses_the_admin_update_endpoint(self):
        transport = RecordingTransport({"id": USER_ID})
        with patch("urllib.request.urlopen", transport):
            _client().set_password(USER_ID, "a long new password")
        sent = transport.requests[0]
        self.assertEqual(sent["method"], "PUT")
        self.assertTrue(sent["url"].endswith(f"/auth/v1/admin/users/{USER_ID}"))
        self.assertEqual(sent["body"], {"password": "a long new password"})

    def test_password_reset_uses_the_public_key_not_the_service_key(self):
        transport = RecordingTransport(None)
        with patch("urllib.request.urlopen", transport):
            _client().send_password_reset("owner@bakery.test", CALLBACK)
        sent = transport.requests[0]
        self.assertTrue(sent["url"].startswith(BASE + "/auth/v1/recover?redirect_to="))
        self.assertEqual(sent["headers"]["apikey"], ANON_KEY)
        self.assertNotIn("authorization", sent["headers"])

    def test_create_user_is_not_exposed(self):
        self.assertFalse(hasattr(SupabaseAuthAdmin, "create_user"))

    def test_unconfigured_admin_fails_closed(self):
        admin = UnconfiguredAuthAdmin()
        for call in (lambda: admin.invite("a@b.test", None, CALLBACK), lambda: admin.get_user(USER_ID),
                     lambda: admin.find_user_by_email("a@b.test")):
            with self.assertRaises(AuthAdminError) as caught:
                call()
            self.assertEqual(caught.exception.code, "not_configured")
        self.assertIsInstance(build_auth_admin(Settings(secret_key="k" * 40)), UnconfiguredAuthAdmin)


class ValidationHelperTests(unittest.TestCase):
    def test_email_validation(self):
        self.assertEqual(normalize_email("  Owner@Bakery.TEST "), "owner@bakery.test")
        for bad in ("", "no-at-sign", "a@b", "a b@c.test", "a@b\n.test", "x" * 320 + "@a.test"):
            with self.assertRaises(AuthAdminError):
                normalize_email(bad)

    def test_password_rules(self):
        check_password("a long password", "a long password")
        for password, confirm in (("short", "short"), ("a long password", "different one!"),
                                  ("é" * 40, "é" * 40)):
            with self.assertRaises(AuthAdminError):
                check_password(password, confirm)

    def test_public_url_must_be_a_bare_https_origin(self):
        self.assertEqual(validate_public_url("https://Admin.Example.test/"), "https://admin.example.test")
        self.assertEqual(validate_public_url("http://localhost:5000", dev_mode=True), "http://localhost:5000")
        for bad in ("http://admin.example.test", "https://admin.example.test/path",
                    "https://admin.example.test/?next=x", "https://user:pw@admin.example.test",
                    "https://admin.example.test/#frag", "admin.example.test"):
            with self.assertRaises(ConfigError):
                validate_public_url(bad)

    def test_service_key_must_be_a_service_key_for_the_same_project(self):
        self.assertEqual(validate_service_role_key(SERVICE_KEY, BASE, ANON_KEY), SERVICE_KEY)
        self.assertEqual(validate_service_role_key("sb_secret_abc", BASE, ANON_KEY), "sb_secret_abc")
        for bad in (ANON_KEY, _jwt({"role": "authenticated"}), "plain-text",
                    _jwt({"role": "service_role", "ref": "zzzzzzzzzzzzzzzzzzzz"})):
            with self.assertRaises(ConfigError):
                validate_service_role_key(bad, BASE, ANON_KEY)

    def test_settings_read_the_new_variables_and_keep_them_out_of_repr(self):
        environ = {
            "PLATFORM_SECRET_KEY": "s" * 40, "SUPABASE_URL": BASE, "SUPABASE_ANON_KEY": ANON_KEY,
            "SAAS_DATABASE_URL": "postgresql://app_backend@db.invalid:5432/postgres",
            "SAAS_PLATFORM_DATABASE_URL": "postgresql://saas_platform_admin@db.invalid:5432/postgres",
            "SUPABASE_SERVICE_ROLE_KEY": SERVICE_KEY, "PLATFORM_PUBLIC_URL": PUBLIC,
        }
        settings = Settings.from_env(environ)
        self.assertTrue(settings.auth_admin_configured)
        self.assertEqual(settings.public_url, PUBLIC)
        self.assertNotIn(SERVICE_KEY, repr(settings))
        self.assertNotIn("s" * 40, repr(settings))
        environ.pop("SUPABASE_SERVICE_ROLE_KEY")
        self.assertFalse(Settings.from_env(environ).auth_admin_configured)

    def test_callback_url(self):
        self.assertEqual(auth_callback_url(PUBLIC + "/"), CALLBACK)


# ---------------------------------------------------------------------------
# Tenant portal: invitation link -> choose password -> sign in
# ---------------------------------------------------------------------------
class TokenAuthenticator(Authenticator):
    def __init__(self, tokens: dict[str, Identity]):
        self.tokens = tokens

    def login(self, email, password):
        raise AuthError("Invalid email or password.")

    def verify_token(self, token):
        if token not in self.tokens:
            raise AuthError("Invalid or expired credentials.")
        return self.tokens[token]


class RecordingAuthAdmin(AuthAdmin):
    def __init__(self):
        self.passwords: list[tuple[str, str]] = []
        self.resets: list[tuple[str, str]] = []

    def invite(self, email, full_name, redirect_to):  # pragma: no cover - not used here
        raise AssertionError("no invitations in portal tests")

    def get_user(self, user_id):  # pragma: no cover
        return None

    def find_user_by_email(self, email):  # pragma: no cover
        return None

    def set_password(self, user_id, password):
        self.passwords.append((user_id, password))

    def send_password_reset(self, email, redirect_to):
        self.resets.append((email, redirect_to))
        if email == "missing@bakery.test":
            raise AuthAdminError("No such account.", "user_not_found")


class PasswordSetupFlowTests(unittest.TestCase):
    def setUp(self):
        self.admin = RecordingAuthAdmin()
        self.authenticator = TokenAuthenticator({"good-token": Identity(USER_ID, "owner@bakery.test")})
        self.app = create_app(
            Settings(secret_key="k" * 40, cookie_secure=False, public_url=PUBLIC),
            authenticator=self.authenticator, auth_admin=self.admin,
        )
        self.client = self.app.test_client()

    def _csrf(self):
        self.client.get("/t/auth/callback")
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def _open_link(self, token="good-token"):
        return self.client.post("/t/auth/callback", data={"csrf_token": self._csrf(), "access_token": token})

    def test_callback_page_loads_the_same_origin_script_and_no_token_reaches_the_server_by_get(self):
        page = self.client.get("/t/auth/callback").get_data(as_text=True)
        self.assertIn("/static/auth_callback.js", page)
        self.assertIn('name="csrf_token"', page)

    def test_valid_link_then_password_then_sign_in_page(self):
        response = self._open_link()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/t/set-password"))
        with self.client.session_transaction() as session:
            self.assertEqual(session["t_pw_user_id"], USER_ID)
            self.assertNotIn("good-token", json.dumps(dict(session)))
            self.assertNotIn("t_user_id", session, "opening a link must not sign anyone in")
            token = session["csrf_token"]
        page = self.client.get("/t/set-password").get_data(as_text=True)
        self.assertIn("owner@bakery.test", page)
        response = self.client.post("/t/set-password", data={
            "csrf_token": token, "password": "a long new password", "confirm": "a long new password"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/t/login?password=set", response.headers["Location"])
        self.assertEqual(self.admin.passwords, [(USER_ID, "a long new password")])
        with self.client.session_transaction() as session:
            self.assertNotIn("t_pw_user_id", session)
        self.assertIn("Your password is set", self.client.get("/t/login?password=set").get_data(as_text=True))

    def test_invalid_or_missing_token_is_refused(self):
        for token in ("forged-token", ""):
            response = self._open_link(token)
            self.assertEqual(response.status_code, 400)
            self.assertIn("invalid or has expired", response.get_data(as_text=True))
        self.assertEqual(self.client.get("/t/set-password").status_code, 400)

    def test_weak_or_mismatched_password_is_rejected_without_calling_supabase(self):
        self._open_link()
        with self.client.session_transaction() as session:
            token = session["csrf_token"]
        for password, confirm in (("short", "short"), ("a long new password", "another long one")):
            response = self.client.post("/t/set-password", data={
                "csrf_token": token, "password": password, "confirm": confirm})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.admin.passwords, [])

    def test_password_window_expires(self):
        self._open_link()
        with self.client.session_transaction() as session:
            session["t_pw_exp"] = int(time.time()) - 1
        self.assertEqual(self.client.get("/t/set-password").status_code, 400)

    def test_set_password_requires_csrf(self):
        self._open_link()
        response = self.client.post("/t/set-password", data={
            "password": "a long new password", "confirm": "a long new password"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.admin.passwords, [])

    def test_forgot_password_gives_the_same_answer_for_any_email(self):
        answers = []
        for email in ("owner@bakery.test", "missing@bakery.test"):
            token = self._csrf()
            response = self.client.post("/t/forgot-password", data={"csrf_token": token, "email": email})
            self.assertEqual(response.status_code, 200)
            answers.append(response.get_data(as_text=True).count("If an account exists"))
        self.assertEqual(answers, [1, 1])
        self.assertEqual([r[1] for r in self.admin.resets], [CALLBACK, CALLBACK])

    def test_login_page_links_to_password_reset(self):
        self.assertIn("/t/forgot-password", self.client.get("/t/login").get_data(as_text=True))


class OnboardingRoutesRequireAdminTests(unittest.TestCase):
    """Without an admin session every onboarding and member route redirects to sign-in."""

    def test_routes_are_admin_only(self):
        app = create_app(Settings(secret_key="k" * 40, cookie_secure=False), auth_admin=RecordingAuthAdmin())
        client = app.test_client()
        tenant = "11111111-1111-4111-8111-111111111111"
        member = "a1000000-0000-4000-8000-000000000001"
        for path in ("/tenants/onboard", "/members", f"/tenants/{tenant}/members"):
            response = client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/login", response.headers["Location"])
        for action in ("invite", f"{member}/role", f"{member}/status", f"{member}/remove",
                       f"{member}/resend-invite", f"{member}/password-reset"):
            client.get("/login")
            with client.session_transaction() as session:
                token = session["csrf_token"]
            response = client.post(f"/tenants/{tenant}/members/{action}", data={"csrf_token": token})
            self.assertEqual(response.status_code, 302, action)
            self.assertIn("/login", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
