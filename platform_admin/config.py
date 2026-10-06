"""Dashboard configuration, read from the server environment only."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0") == "1"


@dataclass(frozen=True)
class Settings:
    secret_key: str
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_jwt_secret: str | None = None
    app_database_url: str | None = None
    platform_database_url: str | None = None
    cookie_secure: bool = True
    session_minutes: int = 60
    blocked_phone_number_ids: frozenset[str] = field(default_factory=frozenset)
    allow_active_channels: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        secret_key = os.getenv("PLATFORM_SECRET_KEY", "")
        if len(secret_key) < 32:
            raise RuntimeError("PLATFORM_SECRET_KEY must be set to a random value of 32+ characters.")
        blocked = {
            value.strip()
            for value in os.getenv("META_BLOCKED_PHONE_NUMBER_IDS", "").split(",")
            if value.strip()
        }
        return cls(
            secret_key=secret_key,
            supabase_url=os.getenv("SUPABASE_URL"),
            supabase_anon_key=os.getenv("SUPABASE_ANON_KEY"),
            supabase_jwt_secret=os.getenv("SUPABASE_JWT_SECRET"),
            app_database_url=os.getenv("SAAS_DATABASE_URL"),
            platform_database_url=os.getenv("SAAS_PLATFORM_DATABASE_URL"),
            cookie_secure=not _flag("PLATFORM_INSECURE_COOKIES"),
            session_minutes=int(os.getenv("PLATFORM_SESSION_MINUTES", "60")),
            blocked_phone_number_ids=frozenset(blocked),
            allow_active_channels=_flag("PLATFORM_ALLOW_ACTIVE_CHANNELS"),
        )
