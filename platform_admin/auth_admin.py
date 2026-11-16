"""Server-side Supabase Auth administration: invitations, account lookup, password setup.

Every call here runs in the dashboard's server process with the project's service-role key.
The key is never rendered, logged, put in an exception message or sent to a browser, and no
password is ever stored: ``set_password`` forwards it to Supabase once and keeps nothing.

Only the GoTrue endpoints the onboarding flow needs are used:

* ``POST /auth/v1/invite``                 invite a new user by email (``inviteUserByEmail``)
* ``GET  /auth/v1/admin/users/{id}``       verify an existing account by id
* ``GET  /auth/v1/admin/users``            page through accounts to find one by email
* ``PUT  /auth/v1/admin/users/{id}``       set the password after a verified invite link
* ``POST /auth/v1/recover``                send a password-reset email (anon key)

``createUser`` is deliberately not exposed: owners always choose their own password through
the invitation link.
"""

from __future__ import annotations

import abc
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

#: Pragmatic server-side email check: one @, a dotted domain, no spaces or control characters.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
MAX_EMAIL_LENGTH = 320
#: Upper bound on account pages scanned when looking an email up (200 per page).
MAX_LOOKUP_PAGES = 50
LOOKUP_PAGE_SIZE = 200
#: Supabase hashes with bcrypt, which ignores bytes beyond 72.
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_BYTES = 72


class AuthAdminError(Exception):
    """A Supabase Auth administration call failed. ``str(error)`` is safe to show an admin."""

    def __init__(self, message: str, code: str = "auth_admin_error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str
    confirmed: bool = False
    invited_at: str | None = None
    last_sign_in_at: str | None = None
    full_name: str | None = None


def normalize_email(value: Any) -> str:
    """Lower-cased, trimmed email, or AuthAdminError when it is not a plausible address."""
    email = str(value or "").strip().lower()
    if not email or len(email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.fullmatch(email):
        raise AuthAdminError("Enter a valid email address.", "invalid_email")
    return email


def check_password(password: str, confirm: str) -> None:
    if password != confirm:
        raise AuthAdminError("The two passwords do not match.", "password_mismatch")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthAdminError(
            f"Use at least {MIN_PASSWORD_LENGTH} characters.", "weak_password"
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise AuthAdminError("Use at most 72 bytes.", "weak_password")


class AuthAdmin(abc.ABC):
    """What the dashboard needs from the identity provider. Tests use an in-memory fake."""

    @abc.abstractmethod
    def invite(self, email: str, full_name: str | None, redirect_to: str) -> AuthUser:
        """Create the account and email an invitation; AuthAdminError('email_exists') if taken."""

    @abc.abstractmethod
    def get_user(self, user_id: str) -> AuthUser | None:
        """The account with this id, or None."""

    @abc.abstractmethod
    def find_user_by_email(self, email: str) -> AuthUser | None:
        """The account with this email, or None."""

    @abc.abstractmethod
    def set_password(self, user_id: str, password: str) -> None:
        """Set the account's password (after its owner proved control of the email)."""

    @abc.abstractmethod
    def send_password_reset(self, email: str, redirect_to: str) -> None:
        """Email a password-reset link. Must not reveal whether the account exists."""


class UnconfiguredAuthAdmin(AuthAdmin):
    MESSAGE = (
        "Supabase Auth administration is not configured. Set SUPABASE_SERVICE_ROLE_KEY and "
        "PLATFORM_PUBLIC_URL in the server environment."
    )

    def _fail(self, *_args, **_kwargs):
        raise AuthAdminError(self.MESSAGE, "not_configured")

    invite = get_user = find_user_by_email = set_password = send_password_reset = _fail


def _user_from_payload(payload: dict[str, Any]) -> AuthUser:
    metadata = payload.get("user_metadata") or {}
    return AuthUser(
        id=str(uuid.UUID(str(payload["id"]))),
        email=str(payload.get("email") or "").lower(),
        confirmed=bool(payload.get("email_confirmed_at") or payload.get("confirmed_at")),
        invited_at=payload.get("invited_at"),
        last_sign_in_at=payload.get("last_sign_in_at"),
        full_name=metadata.get("full_name") if isinstance(metadata, dict) else None,
    )


class SupabaseAuthAdmin(AuthAdmin):
    def __init__(
        self,
        supabase_url: str,
        service_role_key: str,
        anon_key: str,
        *,
        allowed_redirect_prefix: str,
        timeout: float = 15.0,
    ):
        self._base = supabase_url.rstrip("/")
        self.__key = service_role_key
        self._anon_key = anon_key
        self._allowed_redirect_prefix = allowed_redirect_prefix
        self._timeout = timeout

    def __repr__(self) -> str:  # never show the key
        return f"SupabaseAuthAdmin(base={self._base!r})"

    # -- transport ---------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        anon: bool = False,
    ) -> Any:
        url = f"{self._base}/auth/v1{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        key = self._anon_key if anon else self.__key
        headers = {"apikey": key, "Content-Type": "application/json"}
        if not anon:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise self._translate(error) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise AuthAdminError(
                "Supabase Auth could not be reached. Nothing was changed; try again.", "unreachable"
            ) from None
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise AuthAdminError("Supabase Auth returned an unexpected response.", "bad_response") from None

    @staticmethod
    def _translate(error: urllib.error.HTTPError) -> AuthAdminError:
        try:
            payload = json.loads(error.read().decode("utf-8") or "{}")
        except (ValueError, OSError):
            payload = {}
        code = str(payload.get("error_code") or payload.get("code") or "")
        message = str(payload.get("msg") or payload.get("message") or payload.get("error_description") or "")
        if code in ("email_exists", "user_already_exists") or "already been registered" in message:
            return AuthAdminError("An account with this email already exists.", "email_exists")
        if error.code == 404 or code == "user_not_found":
            return AuthAdminError("No such account.", "user_not_found")
        if error.code == 429 or "rate limit" in message.lower():
            return AuthAdminError(
                "Supabase is rate-limiting emails. Wait a few minutes and try again.", "rate_limited"
            )
        if error.code in (401, 403):
            return AuthAdminError(
                "Supabase rejected the server's credentials. Check SUPABASE_SERVICE_ROLE_KEY.",
                "forbidden",
            )
        if code == "weak_password":
            return AuthAdminError("That password is too weak. Choose a longer one.", "weak_password")
        # The upstream message describes the request, never the key, but is kept short.
        detail = f" ({message[:160]})" if message else ""
        return AuthAdminError(f"Supabase Auth refused the request{detail}.", code or "auth_admin_error")

    def _check_redirect(self, redirect_to: str) -> str:
        if not redirect_to.startswith(self._allowed_redirect_prefix):
            raise AuthAdminError("Refusing an invitation link to an unapproved address.", "bad_redirect")
        return redirect_to

    # -- operations --------------------------------------------------------------
    def invite(self, email: str, full_name: str | None, redirect_to: str) -> AuthUser:
        email = normalize_email(email)
        data = {"full_name": full_name} if full_name else {}
        payload = self._request(
            "POST", "/invite",
            body={"email": email, "data": data},
            query={"redirect_to": self._check_redirect(redirect_to)},
        )
        if not isinstance(payload, dict) or "id" not in payload:
            raise AuthAdminError("Supabase Auth returned an unexpected response.", "bad_response")
        return _user_from_payload(payload)

    def get_user(self, user_id: str) -> AuthUser | None:
        user_id = str(uuid.UUID(str(user_id)))
        try:
            payload = self._request("GET", f"/admin/users/{user_id}")
        except AuthAdminError as error:
            if error.code == "user_not_found":
                return None
            raise
        return _user_from_payload(payload) if isinstance(payload, dict) and "id" in payload else None

    def find_user_by_email(self, email: str) -> AuthUser | None:
        email = normalize_email(email)
        for page in range(1, MAX_LOOKUP_PAGES + 1):
            payload = self._request(
                "GET", "/admin/users", query={"page": page, "per_page": LOOKUP_PAGE_SIZE}
            )
            users = (payload or {}).get("users") or []
            for entry in users:
                if str(entry.get("email") or "").lower() == email:
                    return _user_from_payload(entry)
            if len(users) < LOOKUP_PAGE_SIZE:
                return None
        raise AuthAdminError(
            "Too many accounts to search by email; enter the account's user id instead.",
            "lookup_limit",
        )

    def set_password(self, user_id: str, password: str) -> None:
        user_id = str(uuid.UUID(str(user_id)))
        self._request("PUT", f"/admin/users/{user_id}", body={"password": password})

    def send_password_reset(self, email: str, redirect_to: str) -> None:
        email = normalize_email(email)
        self._request(
            "POST", "/recover", body={"email": email},
            query={"redirect_to": self._check_redirect(redirect_to)}, anon=True,
        )


def auth_callback_url(public_url: str) -> str:
    """Where Supabase sends the user after an invitation or reset link is opened."""
    return public_url.rstrip("/") + "/t/auth/callback"


def build_auth_admin(settings) -> AuthAdmin:
    if not settings.auth_admin_configured:
        return UnconfiguredAuthAdmin()
    return SupabaseAuthAdmin(
        settings.supabase_url,
        settings.supabase_service_role_key,
        settings.supabase_anon_key or "",
        allowed_redirect_prefix=auth_callback_url(settings.public_url),
    )
