"""WhatsApp ``phone_number_id`` -> ``tenant_id`` routing.

Target flow (NOT wired in Phase 1 -- the production webhook is deliberately untouched)::

    WhatsApp webhook POST
        |
        v
    metadata.phone_number_id
        |
        v
    app.resolve_tenant_by_phone_number_id(...)      <- database function, returns one uuid
        |
        v
    tenant_id
        |
        v
    tenant_transaction(tenant_id)                   <- RLS now confines every query
        |
        v
    tenant-scoped processing

Why a database function exists at all: an inbound webhook has to identify its tenant
*before* any tenant context can exist, and RLS (correctly) blocks that lookup. It is the
only elevated read path in Phase 1, and it is deliberately narrow -- it returns a single
uuid, never row data, and only for an exact match on an active WhatsApp channel.

An unknown or disabled number returns ``None``. That must be treated as "quarantine and
alert", never as "pick a tenant".
"""

from __future__ import annotations

from .config import require_database_url
from .db import connection

# Function is SECURITY DEFINER and EXECUTE is granted only to app_backend, so this call
# is available to the webhook/backend process and not to browser sessions.
RESOLVE_SQL = "select app.resolve_tenant_by_phone_number_id(%s)"


def resolve_tenant_id_by_phone_number_id(
    phone_number_id: str | None, url: str | None = None
) -> str | None:
    """Return the tenant id owning this WhatsApp phone_number_id, or None if unroutable."""
    if not phone_number_id:
        return None

    with connection(url or require_database_url()) as conn:
        with conn.cursor() as cursor:
            cursor.execute(RESOLVE_SQL, (str(phone_number_id),))
            row = cursor.fetchone()

    return str(row[0]) if row and row[0] is not None else None


def is_routable_channel(phone_number_id: str | None, url: str | None = None) -> bool:
    """True when the number resolves to a tenant. Convenience wrapper for callers that
    only need the boolean (for example to decide whether to quarantine a payload)."""
    return resolve_tenant_id_by_phone_number_id(phone_number_id, url) is not None