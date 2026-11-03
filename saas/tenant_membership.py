"""Active tenant-membership lookups for the tenant portal.

The application connects as ``app_backend`` and passes the *server-verified* JWT
subject through ``request.jwt.claims`` — the same trusted-server-context model as
:mod:`saas.platform`. Membership rows are read only through the RLS predicate
functions (``app.is_tenant_member`` / ``app.is_tenant_owner``) and, at login time,
through ``app.my_active_tenants()`` (migration 0038), which is SECURITY DEFINER and
returns only the caller's own active rows.

No Supabase access or refresh token is handled here, and no user id ever comes from
the request: callers pass the identity their own authentication step verified.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import Any, Iterator

from .db import connect


@contextlib.contextmanager
def member_transaction(user_id: str, url: str | None = None) -> Iterator[Any]:
    """Yield an ``app_backend`` cursor bound to one verified user identity.

    Raises ValueError when the identity is missing: a membership check without a
    verified subject must fail loudly, never default to "allow".
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("A verified user_id is required for membership operations.")
    conn = connect(url)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": user_id}),),
            )
            yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_my_active_tenants(
    user_id: str, url: str | None = None
) -> list[dict[str, str]]:
    """The verified user's active memberships as ``[{"tenant_id", "role"}, ...]``.

    Empty for an unknown user or an unauthenticated session (``app.my_active_tenants``
    returns zero rows when no jwt claims are set).
    """
    with member_transaction(user_id, url) as cursor:
        cursor.execute("select * from app.my_active_tenants()")
        columns = [column.name for column in cursor.description]
        memberships = [
            dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
        ]
    for membership in memberships:
        membership["tenant_id"] = str(membership["tenant_id"])
    return memberships


def membership_for(
    user_id: str, tenant_id: str, url: str | None = None
) -> dict[str, str] | None:
    """``{"role": "owner" | "staff"}`` for an active membership, else ``None``.

    Non-active statuses (``invited``, ``disabled``, …) are denied here because the
    RLS predicates only accept ``status = 'active'``. The tenant id comes from the
    request URL but is only ever compared server-side against the caller's own rows.
    """
    if not tenant_id or not isinstance(tenant_id, str):
        raise ValueError("A tenant_id is required for membership checks.")
    with member_transaction(user_id, url) as cursor:
        cursor.execute(
            "select app.is_tenant_member(%s), app.is_tenant_owner(%s)",
            (tenant_id, tenant_id),
        )
        row = cursor.fetchone()
    if row is None or not row[0]:
        return None
    return {"role": "owner" if row[1] else "staff"}