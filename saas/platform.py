"""Platform-level data access: the app.platform_* functions, via saas_platform_admin.

Every call carries the verified administrator identity as ``request.jwt.claims`` for the
current transaction only. The database re-checks it (``app.require_platform_admin()``), so
authorization does not rest on the web layer alone. This role has no table privileges.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, Iterator

from .db import connect_platform


@contextlib.contextmanager
def platform_transaction(admin_user_id: str, url: str | None = None) -> Iterator[Any]:
    """Yield a cursor bound to one verified administrator identity; commit or roll back."""
    if not admin_user_id:
        raise ValueError("admin_user_id is required for platform operations.")
    conn = connect_platform(url)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": str(admin_user_id)}),),
            )
            yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _rows(cursor: Any) -> list[dict[str, Any]]:
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def is_platform_admin(user_id: str, url: str | None = None) -> bool:
    """True only for an active platform administrator. Any failure is treated as False."""
    if not user_id:
        return False
    with platform_transaction(user_id, url) as cursor:
        cursor.execute("select app.is_platform_admin()")
        return bool(cursor.fetchone()[0])


def provision_tenant(
    admin_user_id: str,
    name: str,
    slug: str,
    timezone: str = "Africa/Nairobi",
    currency: str = "KES",
    owner_user_id: str | None = None,
    initial_plan_id: str | None = None,
    url: str | None = None,
) -> str:
    with platform_transaction(admin_user_id, url) as cursor:
        cursor.execute(
            "select app.platform_provision_tenant(%s, %s, %s, %s, %s, %s)",
            (name, slug, timezone, currency, owner_user_id, initial_plan_id),
        )
        return str(cursor.fetchone()[0])


def set_tenant_status(
    admin_user_id: str,
    tenant_id: str,
    status: str,
    waive_owner: bool = False,
    url: str | None = None,
) -> str:
    with platform_transaction(admin_user_id, url) as cursor:
        cursor.execute(
            "select app.platform_set_tenant_status(%s, %s, %s)",
            (tenant_id, status, waive_owner),
        )
        return cursor.fetchone()[0]


def tenant_readiness(admin_user_id: str, tenant_id: str, url: str | None = None) -> dict[str, Any]:
    with platform_transaction(admin_user_id, url) as cursor:
        cursor.execute("select app.platform_tenant_readiness(%s)", (tenant_id,))
        return cursor.fetchone()[0]


def list_tenants(
    admin_user_id: str, status: str | None = None, url: str | None = None
) -> list[dict[str, Any]]:
    with platform_transaction(admin_user_id, url) as cursor:
        cursor.execute("select * from app.platform_list_tenants(null, %s)", (status,))
        return _rows(cursor)


def get_tenant(admin_user_id: str, tenant_id: str, url: str | None = None) -> dict[str, Any] | None:
    with platform_transaction(admin_user_id, url) as cursor:
        cursor.execute("select * from app.platform_list_tenants(%s, null)", (tenant_id,))
        rows = _rows(cursor)
        return rows[0] if rows else None


def list_plans(admin_user_id: str, url: str | None = None) -> list[dict[str, Any]]:
    with platform_transaction(admin_user_id, url) as cursor:
        cursor.execute("select * from app.platform_list_plans()")
        return _rows(cursor)


def overview(admin_user_id: str, url: str | None = None) -> dict[str, Any]:
    with platform_transaction(admin_user_id, url) as cursor:
        cursor.execute("select app.platform_overview()")
        return cursor.fetchone()[0]


def recent_audit(
    admin_user_id: str,
    tenant_id: str | None = None,
    limit: int = 50,
    url: str | None = None,
) -> list[dict[str, Any]]:
    with platform_transaction(admin_user_id, url) as cursor:
        cursor.execute("select * from app.platform_recent_audit(%s, %s)", (tenant_id, limit))
        return _rows(cursor)
