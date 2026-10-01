"""Multi-tenant SaaS platform foundation — Phase 1 (shared schema + tenant_id + RLS).

This repository is **platform-first**. Businesses are onboarded as *data* — rows in
``public.tenants`` — and never as code. There is no per-business special case anywhere in
this package: every tenant is treated identically, including the first one.

Phase 1 delivers the tenancy model and isolation layer only. There is no web framework, no
WhatsApp client and no gateway integration yet; see ``docs/PHASE_1_SAAS_FOUNDATION.md``.

The PostgreSQL driver (psycopg) is imported lazily inside functions, so importing this
package can never fail because of a missing optional dependency.

Modules:

    config          environment configuration
    db              PostgreSQL connection helpers
    tenant_context  server-side tenant scoping: set_config('app.tenant_id', <uuid>, true)
    routing         phone_number_id -> tenant_id resolution for inbound WhatsApp webhooks
"""

from __future__ import annotations

__all__ = ["config", "db", "routing", "tenant_context"]
