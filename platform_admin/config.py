"""Dashboard configuration, read from the server environment only.

``Settings.from_env()`` validates everything at startup and refuses to start (fails
closed) when required production configuration is missing or unsafe.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

REQUIRED_ENV = (
    "PLATFORM_SECRET_KEY",
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SAAS_DATABASE_URL",
    "SAAS_PLATFORM_DATABASE_URL",
)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
FORBIDDEN_DB_USERS = frozenset({"service_role", "postgres", "supabase_admin"})


class ConfigError(RuntimeError):
    """The environment is missing required configuration or contains an unsafe value."""


def _flag(environ: dict[str, str], name: str) -> bool:
    return environ.get(name, "0") == "1"


def require_https(url: str, *, dev_mode: bool = False) -> str:
    """Return ``url`` if it is https (http only for loopback in explicit dev mode)."""
    parts = urlsplit(url)
    if not parts.hostname:
        raise ConfigError("SUPABASE_URL must be an absolute URL.")
    if parts.scheme == "https":
        return url.rstrip("/")
    if dev_mode and parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
        return url.rstrip("/")
    raise ConfigError("SUPABASE_URL must use https.")


def _jwt_role(token: str) -> str | None:
    """Unverified role claim of a JWT-shaped key, for rejecting service_role keys only."""
    pieces = token.split(".")
    if len(pieces) != 3:
        return None
    try:
        padded = pieces[1] + "=" * (-len(pieces[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded)).get("role")
    except (ValueError, TypeError, AttributeError):
        return None


def reject_service_role(anon_key: str) -> None:
    if _jwt_role(anon_key) == "service_role" or anon_key.startswith("sb_secret_"):
        raise ConfigError("SUPABASE_ANON_KEY must be the public anon key, never a service_role/secret key.")


def _database_user(url: str) -> str | None:
    try:
        return urlsplit(url).username
    except ValueError:
        return None


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
    allow_active_channels: bool = False
    trusted_proxies: int = 0
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    whatsapp_verify_token: str | None = None
    whatsapp_app_secret: str | None = None
    whatsapp_graph_api_version: str = "v21.0"
    whatsapp_send_enabled: bool = False

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        environ = dict(os.environ if environ is None else environ)

        missing = [name for name in REQUIRED_ENV if not environ.get(name, "").strip()]
        if missing:
            raise ConfigError("Missing required configuration: " + ", ".join(missing))

        secret_key = environ["PLATFORM_SECRET_KEY"]
        if len(secret_key) < 32:
            raise ConfigError("PLATFORM_SECRET_KEY must be a random value of 32+ characters.")

        dev_mode = _flag(environ, "PLATFORM_DEV_MODE")
        insecure_cookies = _flag(environ, "PLATFORM_INSECURE_COOKIES")
        if insecure_cookies and not dev_mode:
            raise ConfigError("PLATFORM_INSECURE_COOKIES requires PLATFORM_DEV_MODE=1.")

        supabase_url = require_https(environ["SUPABASE_URL"], dev_mode=dev_mode)
        reject_service_role(environ["SUPABASE_ANON_KEY"])

        for name in ("SAAS_DATABASE_URL", "SAAS_PLATFORM_DATABASE_URL"):
            if (_database_user(environ[name]) or "").lower().split(".")[0] in FORBIDDEN_DB_USERS:
                raise ConfigError(f"{name} must use the dedicated role, not a privileged or service_role login.")

        try:
            session_minutes = int(environ.get("PLATFORM_SESSION_MINUTES", "60"))
            trusted_proxies = int(environ.get("PLATFORM_TRUSTED_PROXIES", "0"))
        except ValueError:
            raise ConfigError("PLATFORM_SESSION_MINUTES and PLATFORM_TRUSTED_PROXIES must be integers.") from None
        if not 1 <= session_minutes <= 720:
            raise ConfigError("PLATFORM_SESSION_MINUTES must be between 1 and 720.")
        if not 0 <= trusted_proxies <= 5:
            raise ConfigError("PLATFORM_TRUSTED_PROXIES must be between 0 and 5.")

        return cls(
            secret_key=secret_key,
            supabase_url=supabase_url,
            supabase_anon_key=environ["SUPABASE_ANON_KEY"],
            supabase_jwt_secret=environ.get("SUPABASE_JWT_SECRET") or None,
            app_database_url=environ["SAAS_DATABASE_URL"],
            platform_database_url=environ["SAAS_PLATFORM_DATABASE_URL"],
            cookie_secure=not insecure_cookies,
            session_minutes=session_minutes,
            allow_active_channels=_flag(environ, "PLATFORM_ALLOW_ACTIVE_CHANNELS"),
            trusted_proxies=trusted_proxies,
            openai_api_key=environ.get("OPENAI_API_KEY") or None,
            openai_model=environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
            whatsapp_verify_token=environ.get("WHATSAPP_VERIFY_TOKEN") or None,
            whatsapp_app_secret=environ.get("WHATSAPP_APP_SECRET") or None,
            whatsapp_graph_api_version=(
                environ.get("WHATSAPP_GRAPH_API_VERSION", "v21.0").strip() or "v21.0"
            ),
            whatsapp_send_enabled=_flag(environ, "WHATSAPP_SEND_ENABLED"),
        )


def verify_database_roles(settings: Settings) -> None:
    """Connect once at startup and prove both logins are the expected unprivileged roles."""
    from saas.db import connect, connect_platform  # noqa: PLC0415

    connect(settings.app_database_url).close()
    connect_platform(settings.platform_database_url).close()


# Schema the application database must already provide. Each entry is a table and the
# columns this code selects from it; a table with no columns listed is required to exist.
# Keep in step with the migrations that introduce them (0017, 0026, 0028, 0032, 0033, 0034).
# Note: this contract can only name tables and columns. Objects a migration creates beside
# them -- indexes, triggers, policies -- are covered by the structural tests instead.
REQUIRED_SCHEMA: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("public.tenant_settings", ("catalogue_version",)),
    ("public.product_variants", ("sku", "availability", "stock_quantity", "reorder_level")),
    ("public.quotations", ()),
    ("public.quotation_lines", ()),
    ("public.catalogue_import_batches", ()),
    ("public.whatsapp_webhook_events", ()),
    ("public.whatsapp_conversations", ()),
    ("public.customers", ()),
    ("public.conversations", ()),
    ("public.messages", ()),
    ("public.tenant_order_sequences", ()),
    ("public.orders", ()),
    ("public.order_items", ()),
    ("public.order_payments", ()),
    ("public.handover_requests", ()),
)


def missing_schema_objects(conn) -> list[str]:
    """Names of required tables/columns the connected database does not provide.

    Reads only ``pg_catalog``, so a missing grant cannot be mistaken for a missing object.
    """
    missing: list[str] = []
    with conn.cursor() as cursor:
        for table, columns in REQUIRED_SCHEMA:
            cursor.execute("select to_regclass(%s)", (table,))
            if cursor.fetchone()[0] is None:
                missing.append(f"{table} (table)")
                continue
            if not columns:
                continue
            cursor.execute(
                "select attname from pg_catalog.pg_attribute "
                "where attrelid = to_regclass(%s) and attnum > 0 and not attisdropped",
                (table,),
            )
            present = {row[0] for row in cursor.fetchall()}
            missing += [f"{table}.{name} (column)" for name in columns if name not in present]
    return missing


def verify_database_schema(settings: Settings) -> None:
    """Fail closed at startup when the application database is missing required migrations.

    Without this check an out-of-date database starts cleanly and then returns an opaque
    500 from every page that uses the missing objects, which hides the real cause.
    """
    from saas.db import connect  # noqa: PLC0415

    conn = connect(settings.app_database_url)
    try:
        missing = missing_schema_objects(conn)
    finally:
        conn.close()
    if missing:
        raise ConfigError(
            "The application database is missing required schema: "
            + ", ".join(missing)
            + ". Apply the pending migrations with db/run_migrations.py before starting."
        )
