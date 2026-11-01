"""Dashboard configuration, read from the server environment only.

``Settings.from_env()`` validates deployment configuration and refuses to start (fails
closed) when required production configuration is missing or unsafe.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

REQUIRED_ENV = (
    "PLATFORM_SECRET_KEY",
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SAAS_DATABASE_URL",
    "SAAS_PLATFORM_DATABASE_URL",
)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
FORBIDDEN_DB_USERS = frozenset({"service_role", "postgres", "supabase_admin"})

#: Declared deployment environment. Absent means production, so an existing deployment that
#: has never set it keeps booting exactly as before.
ENVIRONMENTS = ("production", "staging")

#: A Supabase project reference appears as a host label (``db.<ref>.supabase.co``) or as the
#: username suffix (``app_backend.<ref>``, the pooled connection form).
_PROJECT_REF_RE = re.compile(r"^[a-z0-9]{16,32}$")


class ConfigError(RuntimeError):
    """The environment is missing required configuration or contains an unsafe value."""


def _flag(environ: dict[str, str], name: str) -> bool:
    return environ.get(name, "0") == "1"


#: This opt-in is deliberately strict: only the literal boolean ``true`` enables remote
#: access. Conventional false spellings are accepted as false; anything else fails closed.
_TRUE_FLAGS = frozenset({"true"})
_FALSE_FLAGS = frozenset({"false", "0", "no", "off"})


def _strict_flag(environ: dict[str, str], name: str, *, default: bool = False) -> bool:
    """Read a boolean environment flag, failing closed on anything unrecognised."""
    raw = environ.get(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in _TRUE_FLAGS:
        return True
    if value in _FALSE_FLAGS:
        return False
    raise ConfigError(f"{name} must be exactly 'true' or 'false'.")


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


def _database_host(url: str) -> str | None:
    """Lower-cased hostname of a DSN, or ``None`` when it cannot be determined."""
    try:
        return (urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None


def dsn_override_reason(url: str) -> str | None:
    """Why ``url`` is unusable for staging, or ``None`` when it is acceptable.

    libpq accepts connection parameters in the URI query string, so ``?host=`` and
    ``?hostaddr=`` silently redirect the connection to a different server and ``?user=``
    overrides the login -- all while ``urlsplit(url).hostname`` keeps reporting the host the
    DSN appears to name. A query string or fragment therefore makes the declared host and role
    unverifiable, so staging refuses the whole class rather than trying to enumerate the
    dangerous parameter names. Nothing in the staging contract needs either: the database is
    reached over the private internal hostname with the default libpq settings.

    Only parameter *names* appear in the reason, never a value, so a diagnostic can be shown
    without echoing any part of the connection string.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "it cannot be parsed"
    if parts.fragment:
        return "it carries a URL fragment"
    names: list[str] = []
    for part in parts.query.split("&"):
        name = unquote(part.split("=", 1)[0]).strip().lower()
        if name and name not in names:
            names.append(name)
    if names:
        return "it carries query parameters (" + ", ".join(sorted(names)) + ")"
    return None


def project_refs(url: str | None) -> set[str]:
    """Tokens in a DSN that could name a Supabase project.

    A reference appears either as a host label (``db.<ref>.supabase.co``) or as the username
    suffix (``app_backend.<ref>``, the pooled connection form). Pooled *hosts* such as
    ``aws-0-eu-central-1.pooler.supabase.com`` carry it only in the username.

    Returning every candidate -- rather than one guess -- means a staging boot fails closed
    even if the operator recorded the production reference in a different shape from the one
    the DSN happens to use.
    """
    if not url:
        return set()
    try:
        parts = urlsplit(url)
    except ValueError:
        return set()

    candidates = {
        label.lower()
        for label in (parts.hostname or "").split(".")
        if _PROJECT_REF_RE.match(label.lower())
    }
    username = (parts.username or "").lower()
    if "." in username:
        suffix = username.rsplit(".", 1)[-1]
        if _PROJECT_REF_RE.match(suffix):
            candidates.add(suffix)
    return candidates


def _staging_guard(environ: dict[str, str], app_url: str, platform_url: str) -> str:
    """Refuse a staging boot that could reach production. Returns the environment name.

    Production is untouched: with ``SAAS_ENVIRONMENT`` unset (or ``production``) this returns
    immediately, so no existing deployment changes behaviour. When staging is declared, the
    checks are mandatory rather than advisory -- a staging process that resolves to the
    production project must not start at all.
    """
    environment = (environ.get("SAAS_ENVIRONMENT") or "production").strip().lower()
    if environment not in ENVIRONMENTS:
        raise ConfigError(
            "SAAS_ENVIRONMENT must be one of: " + ", ".join(ENVIRONMENTS) + "."
        )
    if environment != "staging":
        return environment

    production_ref = (environ.get("SAAS_PRODUCTION_PROJECT_REF") or "").strip().lower()
    if not production_ref:
        raise ConfigError(
            "SAAS_PRODUCTION_PROJECT_REF is required when SAAS_ENVIRONMENT=staging. "
            "Naming the production project is what allows the staging boot to prove it is "
            "pointing somewhere else."
        )

    for name, url in (
        ("SAAS_DATABASE_URL", app_url),
        ("SAAS_PLATFORM_DATABASE_URL", platform_url),
        ("SUPABASE_URL", environ.get("SUPABASE_URL") or ""),
    ):
        if production_ref in project_refs(url):
            raise ConfigError(
                f"{name} resolves to the production Supabase project ({production_ref}). "
                "Staging must use its own project."
            )

    # A Supabase project reference cannot identify a non-Supabase host, so the host itself is
    # declared and matched. Without this the checks above are vacuous for a Railway database:
    # they would pass for *any* host that carries no project reference.
    expected_host = (environ.get("SAAS_STAGING_DB_HOST") or "").strip().lower()
    if not expected_host:
        raise ConfigError(
            "SAAS_STAGING_DB_HOST is required when SAAS_ENVIRONMENT=staging. Name the "
            "private hostname of the staging database so the boot can prove which database "
            "it is about to use; a project reference cannot do that for a non-Supabase host."
        )

    # Comparing each DSN against the declared host also guarantees the two DSNs agree with
    # each other, so no separate "same host" comparison is needed.
    resolved: list[tuple[str, str]] = []
    for name, url in (
        ("SAAS_DATABASE_URL", app_url),
        ("SAAS_PLATFORM_DATABASE_URL", platform_url),
    ):
        reason = dsn_override_reason(url)
        if reason:
            raise ConfigError(
                f"{name} is refused for staging because {reason}. libpq would use those "
                "parameters instead of the host and login this configuration declares, so the "
                "staging database identity could not be verified. Remove the query string and "
                "the fragment."
            )
        host = _database_host(url)
        if not host:
            raise ConfigError(
                f"{name} must include a hostname when SAAS_ENVIRONMENT=staging, so it can "
                "be checked against SAAS_STAGING_DB_HOST."
            )
        resolved.append((name, host))

    for name, host in resolved:
        if host != expected_host:
            raise ConfigError(
                f"{name} does not use the host named by SAAS_STAGING_DB_HOST. Staging must "
                "point at its own database host."
            )

    if not (environ.get("WHATSAPP_SEND_ENABLED") or "").strip():
        raise ConfigError(
            "WHATSAPP_SEND_ENABLED must be set explicitly when SAAS_ENVIRONMENT=staging "
            "(use 0 unless a dedicated staging test number is configured)."
        )

    if (environ.get("SAAS_OPERATOR_DATABASE_URL") or "").strip():
        raise ConfigError(
            "SAAS_OPERATOR_DATABASE_URL must not be present on the staging web service. "
            "Run migrations and bootstrap_platform_admin.py from an operator shell instead."
        )

    if _flag(environ, "PLATFORM_DEV_MODE") or _flag(environ, "PLATFORM_INSECURE_COOKIES"):
        raise ConfigError(
            "PLATFORM_DEV_MODE and PLATFORM_INSECURE_COOKIES must be 0 when "
            "SAAS_ENVIRONMENT=staging."
        )
    return environment


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
    #: Explicit opt-in for the Menu Simulator to use a non-loopback configured database.
    #: Default false keeps the simulator local-only.
    menu_simulator_allow_remote_database: bool = False
    #: ``production`` unless ``SAAS_ENVIRONMENT`` declares otherwise. Only ``staging`` adds
    #: the isolation checks in :func:`_staging_guard`.
    environment: str = "production"
    startup_database_checks: bool = True

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

        # Refuses a staging boot that could reach production. Inert unless SAAS_ENVIRONMENT
        # says staging, so an existing production deployment boots exactly as before.
        environment = _staging_guard(
            environ, environ["SAAS_DATABASE_URL"], environ["SAAS_PLATFORM_DATABASE_URL"]
        )
        startup_database_checks = _strict_flag(
            environ, "SAAS_STARTUP_DATABASE_CHECKS", default=True
        )
        if not startup_database_checks and environment != "staging":
            raise ConfigError(
                "SAAS_STARTUP_DATABASE_CHECKS may be false only when "
                "SAAS_ENVIRONMENT=staging."
            )

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
            menu_simulator_allow_remote_database=_strict_flag(
                environ, "MENU_SIMULATOR_ALLOW_REMOTE_DATABASE"
            ),
            environment=environment,
            startup_database_checks=startup_database_checks,
        )


def verify_database_roles(settings: Settings) -> None:
    """Connect once at startup and prove both logins are the expected unprivileged roles."""
    from saas.db import connect, connect_platform  # noqa: PLC0415

    connect(settings.app_database_url).close()
    connect_platform(settings.platform_database_url).close()


# Schema the application database must already provide. Each entry is a table and the
# columns this code selects from it; a table with no columns listed is required to exist.
# Keep in step with the migrations that introduce them
# (0006 tenant_settings, 0017 product_variants, 0026 catalogue imports + catalogue_version +
# quotations, 0028 whatsapp webhook tables, 0031 product_variants fields, 0032 orders and
# payments, 0033 customers/conversations/messages, 0034 handover_requests, 0037 quotation
# briefs and deliveries).
# Note: this contract can only name tables and columns. Objects a migration creates beside
# them -- indexes, triggers, policies -- are covered by the structural tests instead.
REQUIRED_SCHEMA: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("public.tenant_settings", ("catalogue_version",)),
    ("public.product_variants", ("sku", "availability", "stock_quantity", "reorder_level")),
    ("public.quotations", ()),
    ("public.quotation_lines", ()),
    ("public.quotation_briefs", ()),
    ("public.quotation_deliveries", ()),
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


def verify_startup_database(settings: Settings) -> None:
    """Run database startup probes unless the staging-only opt-out is enabled."""
    if not settings.startup_database_checks:
        return
    verify_database_roles(settings)
    verify_database_schema(settings)
