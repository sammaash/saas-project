"""Environment configuration for the Phase 1 SaaS layer.

Mirrors the existing application's convention (plain ``os.getenv`` reads, values
resolved at call time rather than import time), so the repository does not grow a second
configuration style. See ``whatsapp_webhook.py`` for the existing pattern.

None of these variables are required by the current production deployment: if they are
unset, nothing in the existing application changes behaviour.
"""

from __future__ import annotations

import os

# The role the backend connects as for tenant-scoped queries. It must never be the table
# owner and must never be Supabase's service_role, because service_role BYPASSES RLS and
# would make every policy in db/migrations/0011 decorative.
APP_DB_ROLE = "app_backend"

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


def app_db_role() -> str:
    """The fixed role required for tenant-scoped SaaS connections."""
    return APP_DB_ROLE