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
SAAS_REQUIRE_TEST_DATABASE=1, which turns missing configuration into a failure.
"""

from __future__ import annotations

import contextlib
import json
import os
import unittest
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

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
def test_database_url() -> str | None:
    """Connection string for the isolation tests (CI points this at its service container)."""
    if os.getenv("SAAS_REQUIRE_TEST_DATABASE") == "1":
        return os.getenv("SAAS_TEST_DATABASE_URL")
    return os.getenv("SAAS_TEST_DATABASE_URL") or os.getenv("SAAS_DATABASE_URL")


def app_backend_test_url() -> str | None:
    """Local test DSN for the app_backend role configured by db/local/."""
    base = test_database_url()
    if not base:
        return None
    parts = urlsplit(base)
    if not parts.hostname:
        return None
    hostname = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"app_backend:dev_app_backend_password@{hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def database_available() -> bool:
    return bool(test_database_url())


def _connect(url: str | None = None) -> Any:
    try:
        import psycopg  # noqa: PLC0415  (optional dependency, imported lazily)
    except ModuleNotFoundError as error:  # pragma: no cover - developer feedback path
        raise RuntimeError(
            "psycopg is not installed. Run: pip install -r requirements.txt"
        ) from error
    return psycopg.connect(url or test_database_url())


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


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------
class SaasDatabaseTestCase(unittest.TestCase):
    """Base class for Phase 1 database tests."""

    @classmethod
    def setUpClass(cls) -> None:
        if not database_available():
            if os.getenv("SAAS_REQUIRE_TEST_DATABASE") == "1":
                raise AssertionError(
                    "SAAS_REQUIRE_TEST_DATABASE=1 but neither "
                    "SAAS_TEST_DATABASE_URL nor SAAS_DATABASE_URL is configured."
                )
            raise unittest.SkipTest(
                "No SAAS_TEST_DATABASE_URL / SAAS_DATABASE_URL configured, so the "
                "PostgreSQL isolation tests are skipped. See "
                "docs/PHASE_1_SAAS_FOUNDATION.md for how to run them."
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