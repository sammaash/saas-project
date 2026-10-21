"""Shared helpers for the Phase 1 SaaS isolation tests.

HOW IDENTITY IS SIMULATED
-------------------------
Each test connects as the PostgreSQL superuser (the role the migrations were applied as)
and then switches identity *inside a transaction*, exactly the way the platform does at
runtime:

    app_backend    SET LOCAL ROLE app_backend
                   select set_config('app.tenant_id', <uuid>, true)

    authenticated  SET LOCAL ROLE authenticated
                   select set_config('request.jwt.claims', '{"sub":"<user id>"}', true)

The second form is what Supabase's PostgREST does for a dashboard request: switch to the
`authenticated` role and expose the verified JWT claims. So these tests exercise the real
RLS path, not a simulation of it.

Two properties make this safe:

  * `SET LOCAL` is transaction-scoped, so no role or tenant context can leak between
    tests or (in production) between requests on a pooled connection.
  * Every helper rolls its transaction back, so the suite never mutates seeded data.

The suite skips without a database for local convenience. CI sets
SAAS_REQUIRE_TEST_DATABASE=1, which turns missing configuration into a failure. The test
DSN is read **only** from SAAS_TEST_DATABASE_URL: application/operator DSNs are never
fallbacks, and a Supabase, non-local or non-test-named target is rejected before connecting.
"""

from __future__ import annotations

import contextlib
import json
import os
import unittest
from typing import Any, Iterator
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Deterministic seed identifiers -- kept in step with db/seeds/0001_dev_seed.sql.
# ---------------------------------------------------------------------------
TENANT_ONE_ID = "11111111-1111-4111-8111-111111111111"
TENANT_ONE_NAME = "Cindy Bakes"
TENANT_ONE_SLUG = "cindy-bakes"
TENANT_ONE_CHANNEL_ID = "c0000000-0000-4000-8000-000000000001"
TENANT_ONE_PHONE_NUMBER_ID = "dev-phone-id-cindy-001"
TENANT_ONE_OWNER_USER_ID = "aaaaaaaa-0000-4000-8000-000000000001"
TENANT_ONE_STAFF_USER_ID = "aaaaaaaa-0000-4000-8000-000000000002"

TENANT_TWO_ID = "22222222-2222-4222-8222-222222222222"
TENANT_TWO_NAME = "Test Bakery"
TENANT_TWO_SLUG = "test-bakery"
TENANT_TWO_CHANNEL_ID = "c0000000-0000-4000-8000-000000000002"
TENANT_TWO_PHONE_NUMBER_ID = "dev-phone-id-testbakery-002"
TENANT_TWO_OWNER_USER_ID = "bbbbbbbb-0000-4000-8000-000000000001"

OUTSIDER_USER_ID = "cccccccc-0000-4000-8000-000000000001"
INVITED_USER_ID = "cccccccc-0000-4000-8000-000000000002"
SECONDARY_OWNER_USER_ID = "aaaaaaaa-0000-4000-8000-000000000003"

PLAN_ID = "d0000000-0000-4000-8000-000000000001"
SUBSCRIPTION_ONE_ID = "e0000000-0000-4000-8000-000000000001"
SUBSCRIPTION_TWO_ID = "e0000000-0000-4000-8000-000000000002"

# Every Phase 1 tenant-scoped table, with the column that carries the tenant identity.
# `tenants` uses its primary key (the row IS the tenant); `plans` is platform-owned and
# therefore absent by design. Table-driven so that adding a table without updating the
# suite fails the structural test instead of silently escaping it.
TENANT_SCOPED_TABLES: tuple[tuple[str, str], ...] = (
    ("tenants", "id"),
    ("tenant_channels", "tenant_id"),
    ("tenant_users", "tenant_id"),
    ("tenant_settings", "tenant_id"),
    ("subscriptions", "tenant_id"),
    ("audit_log", "tenant_id"),
    ("products", "tenant_id"),
    ("product_variants", "tenant_id"),
    ("catalogue_import_batches", "tenant_id"),
    ("quotations", "tenant_id"),
    ("quotation_lines", "tenant_id"),
    ("whatsapp_webhook_events", "tenant_id"),
    ("whatsapp_conversations", "tenant_id"),
    ("orders", "tenant_id"),
    ("order_items", "tenant_id"),
    ("order_payments", "tenant_id"),
    ("customers", "tenant_id"),
    ("conversations", "tenant_id"),
    ("messages", "tenant_id"),
    ("handover_requests", "tenant_id"),
    ("tenant_order_sequences", "tenant_id"),
)

# Tables that the `authenticated` dashboard role holds no grant on at all, so a dashboard
# session cannot read or enumerate them (Postgres refuses the query outright, which is
# stronger than RLS filtering it to zero rows).
#
# Webhook processing, order/payment ledgers and customer message history are backend-only:
# migrations after 0030 grant to `app_backend` alone (see the privilege note at the top of
# 0032/0033), and the hardening test asserts no later migration re-grants to a
# Supabase-exposed role. The isolation suite must therefore skip these tables when it acts
# as a dashboard session instead of expecting zero rows.
AUTHENTICATED_INACCESSIBLE_TABLES = frozenset(
    {
        "whatsapp_webhook_events",
        "whatsapp_conversations",
        "orders",
        "order_items",
        "order_payments",
        "customers",
        "conversations",
        "messages",
        "tenant_order_sequences",
        "handover_requests",
    }
)

# Tables where FORCE ROW LEVEL SECURITY is deliberately NOT applied, because the
# SECURITY DEFINER helpers in migration 0010 read them. The tests separately prove that
# these tables are still filtered for application roles.
NOT_FORCED_BY_DESIGN = frozenset({"tenant_channels", "tenant_users"})

# Platform-owned tables: deliberately NOT tenant-scoped, so they must have no tenant_id.
PLATFORM_OWNED_TABLES = ("plans",)

# The one documented exception to tenant_id NOT NULL (requirement for audit_log): a
# platform-level event has no tenant, so its tenant_id is legitimately NULL. Every other
# tenant-owned table must be NOT NULL.
TABLES_WITH_NULLABLE_TENANT_ID = ("audit_log",)

# ---------------------------------------------------------------------------
# Connections and identity switching
# ---------------------------------------------------------------------------
class TestDatabaseConfigurationError(RuntimeError):
    """The test database configuration is missing or unsafe to use."""


#: Any host containing one of these is a hosted Supabase endpoint, never a test database.
SUPABASE_HOST_MARKERS = ("supabase.co", "supabase.com", "pooler.supabase.com")
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
NON_DISPOSABLE_NAMES = frozenset({"postgres", "template0", "template1"})


def _is_loopback(host: str) -> bool:
    return host.lower() in LOOPBACK_HOSTS or host.startswith("/")


def _validate_test_database_url(url: str) -> str:
    """Refuse anything that is not an explicitly disposable local test database.

    The suite once fell back to ``SAAS_DATABASE_URL``; when that pointed at the production
    pooler, database tests tried to authenticate against production. This guard makes such a
    configuration an error instead of a silent connection attempt.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise TestDatabaseConfigurationError(
            "SAAS_TEST_DATABASE_URL must be a postgres:// or postgresql:// URL."
        )
    host = (parts.hostname or "").lower()
    if not host:
        raise TestDatabaseConfigurationError(
            "SAAS_TEST_DATABASE_URL must include an explicit host."
        )
    if any(marker in host for marker in SUPABASE_HOST_MARKERS):
        raise TestDatabaseConfigurationError(
            "Refusing a Supabase host as a test database; use a disposable local PostgreSQL."
        )
    username = (parts.username or "").lower()
    production_ref = (os.getenv("SAAS_PRODUCTION_PROJECT_REF") or "").strip().lower()
    if production_ref and production_ref in url.lower():
        raise TestDatabaseConfigurationError(
            "Refusing a URL that names the production project reference."
        )
    if "." in username and len(username.rsplit(".", 1)[1]) >= 16:
        raise TestDatabaseConfigurationError(
            "Refusing a pooled Supabase username as a test database login."
        )
    if not _is_loopback(host) and os.getenv("SAAS_TEST_DATABASE_ALLOW_REMOTE") != "1":
        raise TestDatabaseConfigurationError(
            "Refusing a non-local test database without SAAS_TEST_DATABASE_ALLOW_REMOTE=1."
        )
    database = (parts.path or "").lstrip("/")
    if not database:
        raise TestDatabaseConfigurationError(
            "SAAS_TEST_DATABASE_URL must name a database."
        )
    # libpq query parameters can silently override the visible host/database. A URL that
    # looks local but carries ?host=... or ?dbname=... is not a safe test target.
    query = parse_qs(parts.query, keep_blank_values=True)
    redirecting = {"host", "hostaddr", "port", "dbname", "service", "options", "user"}
    if redirecting.intersection(key.lower() for key in query):
        raise TestDatabaseConfigurationError(
            "Refusing a test URL whose query parameters can redirect the connection."
        )
    if database.lower() in NON_DISPOSABLE_NAMES:
        raise TestDatabaseConfigurationError(
            f"Refusing the shared '{database}' database as a test target."
        )
    confirmed = os.getenv("SAAS_TEST_DATABASE_CONFIRM") == database
    if "test" not in database.lower() and not confirmed:
        raise TestDatabaseConfigurationError(
            "The test database name must contain 'test' or exactly match "
            "SAAS_TEST_DATABASE_CONFIRM."
        )
    return url


def test_database_url() -> str | None:
    """The explicitly configured disposable test database, or None.

    There is deliberately **no** fallback to SAAS_DATABASE_URL or
    SAAS_OPERATOR_DATABASE_URL: those are application/operator credentials and may point at
    production. An unsafe explicit value raises rather than being ignored.
    """
    explicit = os.getenv("SAAS_TEST_DATABASE_URL")
    if not explicit:
        return None
    return _validate_test_database_url(explicit)


def require_test_database_url() -> str:
    """The test database URL, or a clear failure. Used immediately before connecting."""
    url = test_database_url()
    if not url:
        raise TestDatabaseConfigurationError(
            "SAAS_TEST_DATABASE_URL is required for database tests. Provision a disposable "
            "local PostgreSQL (for example a postgres:16 container), apply the migrations "
            "to it, and export SAAS_TEST_DATABASE_URL for that database only."
        )
    return url


def app_backend_test_url() -> str | None:
    """Test DSN for app_backend, never derived from a production-looking target.

    Prefer ``SAAS_TEST_APP_DATABASE_URL``. The local development password is used only
    for a loopback database; a remote test database must provide its own credential.
    """
    explicit = os.getenv("SAAS_TEST_APP_DATABASE_URL")
    base = test_database_url()
    if explicit:
        _validate_test_database_url(explicit)
        if base:
            expected, actual = urlsplit(base), urlsplit(explicit)
            if (expected.hostname, expected.port, expected.path) != (
                actual.hostname,
                actual.port,
                actual.path,
            ):
                raise TestDatabaseConfigurationError(
                    "SAAS_TEST_APP_DATABASE_URL must target the same test database."
                )
        return explicit
    if not base:
        return None
    parts = urlsplit(base)
    host = parts.hostname or ""
    if not _is_loopback(host):
        raise TestDatabaseConfigurationError(
            "Set SAAS_TEST_APP_DATABASE_URL for a non-local test database; the development "
            "password is only allowed on loopback."
        )
    password = os.getenv("SAAS_TEST_APP_PASSWORD") or "dev_app_backend_password"
    hostname = f"[{host}]" if ":" in host else host
    netloc = f"app_backend:{quote(password, safe='')}@{hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def database_available() -> bool:
    return test_database_url() is not None


def _connect(url: str | None = None) -> Any:
    try:
        import psycopg  # noqa: PLC0415  (optional dependency, imported lazily)
    except ModuleNotFoundError as error:  # pragma: no cover - developer feedback path
        raise RuntimeError(
            "psycopg is not installed. Run: pip install -r requirements.txt"
        ) from error
    return psycopg.connect(url or require_test_database_url())


@contextlib.contextmanager
def as_superuser(url: str | None = None) -> Iterator[Any]:
    """Migration-level connection. Bypasses RLS, so it is used only for catalog checks
    and for reading seed data -- never to prove isolation (that would prove nothing)."""
    conn = _connect(url)
    try:
        with conn.cursor() as cursor:
            yield cursor
    finally:
        # Always roll back: tests must never modify seeded rows.
        conn.rollback()
        conn.close()


@contextlib.contextmanager
def as_backend(tenant_id: str | None = None, url: str | None = None) -> Iterator[Any]:
    """Act as the Flask backend / bot: the app_backend role, optionally tenant-scoped.

    ``tenant_id=None`` is meaningful, not a default accident: it reproduces the
    "context missing" case, which must return no rows rather than all rows.
    """
    conn = _connect(url)
    try:
        with conn.cursor() as cursor:
            cursor.execute("set local role app_backend")
            if tenant_id:
                # The same call the SaaS layer makes in saas/tenant_context.py.
                cursor.execute(
                    "select set_config('app.tenant_id', %s, true)", (str(tenant_id),)
                )
            yield cursor
    finally:
        conn.rollback()
        conn.close()


@contextlib.contextmanager
def as_user(user_id: str | None, url: str | None = None) -> Iterator[Any]:
    """Act as a dashboard session: authenticated role plus verified JWT claims.

    This mirrors PostgREST exactly -- switch to `authenticated`, expose the JWT claims --
    so policies depending on app.current_user_id() are exercised for real.
    """
    conn = _connect(url)
    try:
        with conn.cursor() as cursor:
            cursor.execute("set local role authenticated")
            claims = json.dumps({"sub": str(user_id)}) if user_id else "{}"
            cursor.execute(
                "select set_config('request.jwt.claims', %s, true)", (claims,)
            )
            yield cursor
    finally:
        conn.rollback()
        conn.close()


def platform_admin_test_url() -> str | None:
    """Test DSN for saas_platform_admin, never derived from a production-looking target."""
    explicit = os.getenv("SAAS_TEST_PLATFORM_ADMIN_DATABASE_URL")
    base = test_database_url()
    if explicit:
        _validate_test_database_url(explicit)
        if base:
            expected, actual = urlsplit(base), urlsplit(explicit)
            if (expected.hostname, expected.port, expected.path) != (
                actual.hostname,
                actual.port,
                actual.path,
            ):
                raise TestDatabaseConfigurationError(
                    "SAAS_TEST_PLATFORM_ADMIN_DATABASE_URL must target the test database."
                )
        return explicit
    if not base:
        return None
    parts = urlsplit(base)
    host = parts.hostname or ""
    if not _is_loopback(host):
        raise TestDatabaseConfigurationError(
            "Set SAAS_TEST_PLATFORM_ADMIN_DATABASE_URL for a non-local test database."
        )
    password = (
        os.getenv("SAAS_TEST_PLATFORM_ADMIN_PASSWORD") or "dev_platform_admin_password"
    )
    hostname = f"[{host}]" if ":" in host else host
    netloc = f"saas_platform_admin:{quote(password, safe='')}@{hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


PLATFORM_ADMIN_USER_ID = "f0000000-0000-4000-8000-000000000001"


@contextlib.contextmanager
def as_platform(
    user_id: str | None, register: bool = True, url: str | None = None
) -> Iterator[Any]:
    """Act as the platform API: saas_platform_admin plus verified JWT claims.

    ``register`` inserts the identity into platform_admins first (rolled back afterwards),
    so a test can prove both the allowed and the denied case.
    """
    conn = _connect(url)
    try:
        with conn.cursor() as cursor:
            if user_id and register:
                cursor.execute(
                    "insert into public.platform_admins (user_id) values (%s) "
                    "on conflict (user_id) do update set status = 'active'",
                    (user_id,),
                )
            cursor.execute("set local role saas_platform_admin")
            claims = json.dumps({"sub": str(user_id)}) if user_id else "{}"
            cursor.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
            yield cursor
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------
class SaasDatabaseTestCase(unittest.TestCase):
    """Base class for Phase 1 database tests."""

    @classmethod
    def setUpClass(cls) -> None:
        url = test_database_url()
        if not url:
            if os.getenv("SAAS_REQUIRE_TEST_DATABASE") == "1":
                raise AssertionError(
                    "SAAS_REQUIRE_TEST_DATABASE=1 but SAAS_TEST_DATABASE_URL is not "
                    "configured. Database tests never fall back to SAAS_DATABASE_URL."
                )
            raise unittest.SkipTest(
                "SAAS_TEST_DATABASE_URL is not configured; PostgreSQL isolation tests are "
                "skipped. Use a disposable local database, never an application DSN."
            )
        cls._assert_foundation_present()

    @classmethod
    def _assert_foundation_present(cls) -> None:
        """Fail with an actionable message when the database is up but not prepared."""
        try:
            with as_superuser() as cursor:
                cursor.execute("select to_regclass('public.tenants')")
                if cursor.fetchone()[0] is None:
                    raise AssertionError(
                        "public.tenants does not exist. Apply the Phase 1 migrations first:\n"
                        "    python db/run_migrations.py --include-local --seed"
                    )
                cursor.execute(
                    "select count(*) from public.tenants where id = %s", (TENANT_ONE_ID,)
                )
                if cursor.fetchone()[0] != 1:
                    raise AssertionError(
                        "Seed tenant 001 (Cindy Bakes) is missing. Load the development seed:\n"
                        "    python db/run_migrations.py --seed"
                    )
        except AssertionError:
            raise
        except Exception as error:  # pragma: no cover - connection failures
            raise AssertionError(
                f"Could not prepare the test database ({type(error).__name__}: {error}). "
                "Check SAAS_TEST_DATABASE_URL and that PostgreSQL is running."
            ) from error

    # -- convenience wrappers -------------------------------------------------
    def backend(self, tenant_id: str | None):
        """Backend/bot identity scoped to a tenant (None = no context at all)."""
        return as_backend(tenant_id)

    def user(self, user_id: str | None):
        """Dashboard identity with verified JWT claims."""
        return as_user(user_id)

    def superuser(self):
        """RLS-bypassing connection for catalog and fixture reads."""
        return as_superuser()

    @staticmethod
    def count_rows(cursor: Any, table: str, tenant_column: str, tenant_id: str) -> int:
        """Count rows for one tenant in a table.

        Table and column names come only from the module constants above (never from
        input), which is why interpolation is acceptable here.
        """
        cursor.execute(
            f"select count(*) from public.{table} where {tenant_column} = %s", (tenant_id,)
        )
        return cursor.fetchone()[0]
