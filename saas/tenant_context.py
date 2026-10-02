"""Server-side tenant context.

The application must never accept a tenant id from a browser or any other client. The
backend derives the tenant itself:

    WhatsApp  -> metadata.phone_number_id -> tenant_channels -> tenant_id  (see routing)
    dashboard -> authenticated session    -> tenant_users membership       (RLS policies)

...and then scopes all work with the transaction-local setting that the RLS policies
read.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator

from .config import TENANT_CONTEXT_SETTING
from .db import connect

# set_config(name, value, is_local) is the parameterised equivalent of SET LOCAL:
#   * `SET LOCAL app.tenant_id = '<uuid>'` cannot take a bound parameter, and
#   * the third argument `true` is what makes the setting transaction-local.
# A session-level setting would survive the end of the transaction and leak into the next
# request served by the same pooled connection -- potentially another tenant's. The
# transaction-local form is therefore the only form used anywhere in this codebase.
SET_TENANT_CONTEXT_SQL = "select set_config(%s, %s, true)"


@contextlib.contextmanager
def tenant_transaction(tenant_id: str | None, url: str | None = None) -> Iterator[Any]:
    """Yield a cursor scoped to exactly one tenant. Commits on success, rolls back on error.

    Passing an empty tenant id raises immediately rather than running without context.
    That is deliberate: a query with no tenant context returns zero rows, which looks
    like "no data" instead of the real problem ("this code path forgot the tenant"), and
    silently returning nothing in the middle of an order flow is worse than failing loudly.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required; refusing to run without tenant context.")

    conn = connect(url)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                SET_TENANT_CONTEXT_SQL, (TENANT_CONTEXT_SETTING, str(tenant_id))
            )
            # Inside this transaction app.current_tenant_id() returns the value above, so
            # every RLS policy is filtered to this tenant for the rest of the block.
            yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def current_tenant_id(cursor: Any) -> str | None:
    """Return the tenant context the database currently sees.

    Diagnostics and tests only -- never a basis for a security decision, because a client
    must not be able to influence it.
    """
    cursor.execute("select app.current_tenant_id()")
    row = cursor.fetchone()
    return str(row[0]) if row and row[0] is not None else None