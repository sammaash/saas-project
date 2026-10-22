"""Environment configuration for the Phase 1 SaaS layer.

Mirrors the existing application's convention (plain ``os.getenv`` reads, values
resolved at call time rather than import time), so the repository does not grow a second
configuration style. See ``whatsapp_webhook.py`` for the existing pattern.

None of these variables are required by the current production deployment: if they are
unset, nothing in the existing application changes behaviour.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

# The role the backend connects as for tenant-scoped queries. It must never be the table
# owner and must never be Supabase's service_role, because service_role BYPASSES RLS and
# would make every policy in db/migrations/0011 decorative.
APP_DB_ROLE = "app_backend"

# The login used only by the platform admin service. It holds no table privileges and can
# only execute the app.platform_* functions, each of which checks app.is_platform_admin().
PLATFORM_DB_ROLE = "saas_platform_admin"

# The setting that carries tenant context. Read by app.current_tenant_id().
TENANT_CONTEXT_SETTING = "app.tenant_id"


def database_url() -> str | None:
    """Backend connection string for tenant-scoped application queries."""
    return os.getenv("SAAS_DATABASE_URL")


def test_database_url() -> str | None:
    """Connection string used by the isolation tests.

    Falls back to the development database so that a single local database can be used
    for both, while CI can point the tests at its disposable service container.
    """
    return os.getenv("SAAS_TEST_DATABASE_URL") or os.getenv("SAAS_DATABASE_URL")


def require_database_url() -> str:
    """Return the configured connection string or explain exactly what is missing."""
    url = database_url()
    if not url:
        raise RuntimeError(
            "SAAS_DATABASE_URL is not set. The Phase 1 SaaS layer needs a PostgreSQL "
            "connection string; the existing SQLite application does not use this setting."
        )
    return url


def platform_database_url() -> str | None:
    """Connection string for the platform admin login (saas_platform_admin)."""
    return os.getenv("SAAS_PLATFORM_DATABASE_URL")


def require_platform_database_url() -> str:
    url = platform_database_url()
    if not url:
        raise RuntimeError(
            "SAAS_PLATFORM_DATABASE_URL is not set. The platform admin service needs a "
            "connection string for the saas_platform_admin login (configured out of band)."
        )
    return url


def platform_db_role() -> str:
    """The fixed role required for platform admin connections."""
    return PLATFORM_DB_ROLE


def app_db_role() -> str:
    """The fixed role required for tenant-scoped SaaS connections."""
    return APP_DB_ROLE


# ---------------------------------------------------------------------------
# Order policy configuration
# ---------------------------------------------------------------------------

DEFAULT_DEPOSIT_RATE = Decimal("0.5")   # 50% deposit required by default
DEFAULT_DELIVERY_FEE_KES = Decimal("0")  # no delivery fee by default


def order_policy_from_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Extract the order policy from tenant settings.

    The policy lives in the ``business_rules`` JSON document on
    ``public.tenant_settings``. Falls back to sensible defaults when unset so
    that a tenant can operate without configuring anything, while still being
    able to override deposit rate and delivery fee.
    """
    business_rules: dict[str, Any] = {}
    if settings and isinstance(settings.get("business_rules"), dict):
        business_rules = settings["business_rules"]

    deposit_rate_raw = business_rules.get("deposit_rate")
    delivery_fee_raw = business_rules.get("delivery_fee")

    try:
        deposit_rate = Decimal(str(deposit_rate_raw)) if deposit_rate_raw is not None else DEFAULT_DEPOSIT_RATE
    except (InvalidOperation, TypeError, ValueError):
        deposit_rate = DEFAULT_DEPOSIT_RATE

    try:
        delivery_fee = Decimal(str(delivery_fee_raw)) if delivery_fee_raw is not None else DEFAULT_DELIVERY_FEE_KES
    except (InvalidOperation, TypeError, ValueError):
        delivery_fee = DEFAULT_DELIVERY_FEE_KES

    return {
        "deposit_rate": deposit_rate,
        "delivery_fee": delivery_fee,
        "currency": settings.get("currency", "KES") if settings else "KES",
    }