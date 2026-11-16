"""Supabase Auth: server-side password login and JWT verification.

The browser never holds the Supabase access token: the server verifies it, keeps only the
verified ``sub`` in a signed HttpOnly session cookie, and discards the token.
"""

from __future__ import annotations

import abc
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable

from .config import require_https


class AuthError(Exception):
    """Authentication failed. The message is safe to show to the user."""


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str | None = None


#: Supabase ``amr`` methods that prove the person just used an emailed link or code.
#: An ordinary password sign-in (``password``), a refresh-only session, OAuth or SSO
#: never qualifies: those must not be able to set a new password without the old one.
LINK_AUTH_METHODS = frozenset({"invite", "recovery", "otp"})
#: An invitation or reset link must have been used within this many seconds.
LINK_TOKEN_MAX_AGE_SECONDS = 15 * 60
_CLOCK_SKEW_SECONDS = 60


def check_link_claims(
    claims: dict, *, now: float | None = None, max_age: int = LINK_TOKEN_MAX_AGE_SECONDS
) -> None:
    """Raise AuthError unless the claims come from a fresh invitation/recovery/OTP link.

    Requirements: an ``amr`` list with an ``invite``, ``recovery`` or ``otp`` entry whose
    timestamp is at most ``max_age`` seconds old, no ``password`` entry, and an ``iat``
    that is also at most ``max_age`` seconds old.
    """
    now = time.time() if now is None else now
    error = AuthError("Invalid or expired credentials.")
    amr = claims.get("amr")
    if not isinstance(amr, list) or not amr:
        raise error
    methods: list[tuple[str, float]] = []
    for entry in amr:
        if not isinstance(entry, dict):
            raise error
        method, stamp = entry.get("method"), entry.get("timestamp")
        if not isinstance(method, str) or isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            raise error
        methods.append((method, float(stamp)))
    if any(method == "password" for method, _ in methods):
        raise error
    if not any(
        method in LINK_AUTH_METHODS and now - max_age <= stamp <= now + _CLOCK_SKEW_SECONDS
        for method, stamp in methods
    ):
        raise error
    issued = claims.get("iat")
    if isinstance(issued, bool) or not isinstance(issued, (int, float)):
        raise error
    if not now - max_age <= float(issued) <= now + _CLOCK_SKEW_SECONDS:
        raise error


class Authenticator(abc.ABC):
    @abc.abstractmethod
    def login(self, email: str, password: str) -> Identity:
        """Verify credentials and return the verified identity, or raise AuthError."""

    def verify_token(self, token: str) -> Identity:
        """Verify a Supabase access token."""
        raise AuthError("Invalid or expired credentials.")

    def verify_link_token(self, token: str) -> Identity:
        """Verify a token from an invitation or reset link (see ``check_link_claims``).

        Fails closed: an authenticator that cannot inspect the claims accepts nothing.
        """
        raise AuthError("Invalid or expired credentials.")


class SupabaseAuthenticator(Authenticator):
    AUDIENCE = "authenticated"

    def __init__(
        self,
        supabase_url: str,
        anon_key: str,
        jwt_secret: str | None = None,
        timeout: float = 10.0,
        allow_http_loopback: bool = False,
    ):
        # The password is posted to this URL, so plain http is refused (config.ConfigError).
        self._base = require_https(supabase_url, dev_mode=allow_http_loopback)
        self._anon_key = anon_key
        self._jwt_secret = jwt_secret
        self._timeout = timeout
        self._jwks = None

    # -- token verification (also used directly by tests) ----------------------
    def verify_token(self, token: str) -> Identity:
        return self._identity(self._claims(token))

    def verify_link_token(self, token: str) -> Identity:
        claims = self._claims(token)
        check_link_claims(claims)
        return self._identity(claims)

    @staticmethod
    def _identity(claims: dict) -> Identity:
        try:
            user_id = str(uuid.UUID(str(claims["sub"])))
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise AuthError("Invalid or expired credentials.") from error
        email = claims.get("email")
        return Identity(user_id=user_id, email=email if isinstance(email, str) else None)

    def _claims(self, token: str) -> dict:
        import jwt  # noqa: PLC0415  (lazy optional dependency)

        try:
            if self._jwt_secret:
                key, algorithms = self._jwt_secret, ["HS256"]
            else:
                if self._jwks is None:
                    self._jwks = jwt.PyJWKClient(f"{self._base}/auth/v1/.well-known/jwks.json")
                key = self._jwks.get_signing_key_from_jwt(token).key
                algorithms = ["RS256", "ES256"]
            claims = jwt.decode(
                token,
                key,
                algorithms=algorithms,
                audience=self.AUDIENCE,
                issuer=f"{self._base}/auth/v1",
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except Exception as error:  # noqa: BLE001 - never leak verification detail
            raise AuthError("Invalid or expired credentials.") from error
        if not isinstance(claims, dict):
            raise AuthError("Invalid or expired credentials.")
        return claims

    # -- login -------------------------------------------------------------------
    def login(self, email: str, password: str) -> Identity:
        request = urllib.request.Request(
            f"{self._base}/auth/v1/token?grant_type=password",
            data=json.dumps({"email": email, "password": password}).encode("utf-8"),
            headers={"apikey": self._anon_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
            token = payload["access_token"]
        except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as error:
            raise AuthError("Invalid email or password.") from error
        return self.verify_token(token)


class LoginRateLimiter:
    """Sliding-window login throttle with bounded memory (process-local).

    Limits failures per ``ip|email``, per IP, and globally. Expired entries are pruned, and
    the number of tracked keys is capped (oldest evicted), so an attacker cannot grow memory
    by rotating addresses; the global failure cap bounds what eviction could let through.
    When the global cap is reached every login is refused until the window passes.
    """

    def __init__(
        self,
        attempts: int = 5,
        window_seconds: int = 900,
        per_ip: int = 20,
        global_failures: int = 200,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._attempts = attempts
        self._window = window_seconds
        self._per_ip = per_ip
        self._global_limit = global_failures
        self._max_keys = max_keys
        self._clock = clock
        self._events: "OrderedDict[str, deque[float]]" = OrderedDict()
        self._global: deque[float] = deque()
        self._lock = threading.Lock()
        self._last_prune = clock()

    @staticmethod
    def _keys(ip: str, email: str) -> tuple[str, str]:
        return f"i:{ip}", f"k:{ip}|{email}"

    def _expire(self, events: "deque[float]", now: float) -> None:
        while events and now - events[0] > self._window:
            events.popleft()

    def _prune(self, now: float) -> None:
        self._expire(self._global, now)
        for key in [k for k, events in self._events.items() if (self._expire(events, now) or not events)]:
            del self._events[key]
        self._last_prune = now

    def tracked_keys(self) -> int:
        return len(self._events)

    def allow(self, ip: str, email: str) -> bool:
        now = self._clock()
        with self._lock:
            if now - self._last_prune > self._window / 10:
                self._prune(now)
            self._expire(self._global, now)
            if len(self._global) >= self._global_limit:
                return False
            ip_key, pair_key = self._keys(ip, email)
            for key, limit in ((ip_key, self._per_ip), (pair_key, self._attempts)):
                events = self._events.get(key)
                if events is not None:
                    self._expire(events, now)
                    if len(events) >= limit:
                        return False
            return True

    def record_failure(self, ip: str, email: str) -> None:
        now = self._clock()
        with self._lock:
            self._global.append(now)
            for key in self._keys(ip, email):
                events = self._events.setdefault(key, deque())
                events.append(now)
                self._events.move_to_end(key)
            if len(self._events) > self._max_keys:
                self._prune(now)
            while len(self._events) > self._max_keys:
                self._events.popitem(last=False)

    def reset(self, ip: str, email: str) -> None:
        """A successful login clears the per-address-and-email counter only."""
        with self._lock:
            self._events.pop(self._keys(ip, email)[1], None)
