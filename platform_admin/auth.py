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
from collections import defaultdict, deque
from dataclasses import dataclass


class AuthError(Exception):
    """Authentication failed. The message is safe to show to the user."""


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str | None = None


class Authenticator(abc.ABC):
    @abc.abstractmethod
    def login(self, email: str, password: str) -> Identity:
        """Verify credentials and return the verified identity, or raise AuthError."""


class SupabaseAuthenticator(Authenticator):
    AUDIENCE = "authenticated"

    def __init__(
        self,
        supabase_url: str,
        anon_key: str,
        jwt_secret: str | None = None,
        timeout: float = 10.0,
    ):
        self._base = supabase_url.rstrip("/")
        self._anon_key = anon_key
        self._jwt_secret = jwt_secret
        self._timeout = timeout
        self._jwks = None

    # -- token verification (also used directly by tests) ----------------------
    def verify_token(self, token: str) -> Identity:
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
            user_id = str(uuid.UUID(str(claims["sub"])))
        except Exception as error:  # noqa: BLE001 - never leak verification detail
            raise AuthError("Invalid or expired credentials.") from error
        return Identity(user_id=user_id, email=claims.get("email"))

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
    """Per-key sliding window limiter (process-local; adequate for one admin service)."""

    def __init__(self, attempts: int = 5, window_seconds: int = 900):
        self._attempts = attempts
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events[key]
            while events and now - events[0] > self._window:
                events.popleft()
            return len(events) < self._attempts

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._events[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
