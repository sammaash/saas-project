"""Durable human-handover records for one tenant.

When a customer asks for something the catalogue cannot confirm, the answer is not a
price and not "contact staff yourself": it is a stored request plus a real notification.
This module is the storage half of that workflow.

It deliberately knows nothing about delivery. Every function runs inside
``tenant_transaction``, so row level security confines each statement to one tenant, and
the notification path (which needs tenant settings and the tenant's WhatsApp channel) lives
in ``platform_admin.services.handover``.

Two independent idempotency rules are enforced by unique indexes in migration 0034 rather
than by callers remembering to check:

* ``(tenant_id, dedupe_key)`` -- Meta redelivers a webhook until it receives a 200, so the
  same inbound message can be processed more than once;
* ``(tenant_id, wa_id, product_key) where status <> 'resolved'`` -- a customer asking again
  about the same product must not produce a second request.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from psycopg.types.json import Jsonb

from .tenant_context import tenant_transaction

logger = logging.getLogger("saas.handover")

#: Lifecycle of a request. ``failed`` means the notification could not be delivered and is
#: retryable; it never means "the customer was told staff have it".
STATUSES = ("pending", "notified", "in_progress", "resolved", "failed")

#: Why the handover was needed. Keeping this explicit is what distinguishes an unlisted
#: product from an existing product whose requested variant is unavailable.
REASONS = ("unlisted_product", "unavailable_variant", "custom_order", "customer_request")

#: Statuses a member of staff may move a request to from the dashboard.
STAFF_STATUSES = ("pending", "in_progress", "resolved")

MAX_TEXT = 200

_COLUMNS = (
    "id, tenant_id, customer_id, conversation_id, wa_id, customer_name, "
    "requested_product, product_key, details, context, reason, status, "
    "notification_target, notification_error, attempts, notified_at, resolved_at, "
    "created_at, updated_at"
)


def product_key(value: Any) -> str:
    """A stable key for a requested product: case, punctuation and spacing insensitive."""
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return cleaned[:MAX_TEXT]


def _required_text(value: Any, field: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    if not cleaned:
        raise ValueError(f"{field} is required.")
    return cleaned[:MAX_TEXT]


def _optional_text(value: Any, field: str) -> str | None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    return cleaned[:MAX_TEXT]


def _row(cursor: Any, row: tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(zip((column.name for column in cursor.description), row, strict=True))


def _rows(cursor: Any, rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [entry for entry in (_row(cursor, row) for row in rows) if entry is not None]


def create_request(
    tenant_id: str,
    *,
    wa_id: str,
    requested_product: str,
    dedupe_key: str,
    reason: str = "unlisted_product",
    details: dict[str, Any] | None = None,
    context: list[dict[str, str]] | None = None,
    customer_id: str | None = None,
    conversation_id: str | None = None,
    customer_name: str | None = None,
    database_url: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Record a handover request exactly once.

    Returns ``(row, created)``. ``created`` is False when the same provider event was
    retried or when this customer already has an unresolved request for the same product,
    so the caller can notify without ever creating a duplicate -- and knows whether this is
    a fresh request or a repeat.
    """
    if reason not in REASONS:
        raise ValueError(f"Unsupported handover reason: {reason!r}")
    wa_id = _required_text(wa_id, "wa_id")
    product = _required_text(requested_product, "requested_product")
    dedupe = _required_text(dedupe_key, "dedupe_key")
    key = product_key(product)

    with tenant_transaction(tenant_id, database_url) as cursor:
        cursor.execute(
            f"""
            insert into public.handover_requests
                (tenant_id, customer_id, conversation_id, wa_id, customer_name,
                 requested_product, product_key, details, context, reason, dedupe_key)
            values (app.current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict do nothing
            returning {_COLUMNS}
            """,
            (
                customer_id,
                conversation_id,
                wa_id,
                _optional_text(customer_name, "customer_name"),
                product,
                key,
                Jsonb(details or {}),
                Jsonb(context or []),
                reason,
                dedupe,
            ),
        )
        inserted = _row(cursor, cursor.fetchone())
        if inserted is not None:
            return inserted, True

        # A conflict means this request already exists: either the same provider event was
        # redelivered, or the customer already has an unresolved request for this product.
        cursor.execute(
            f"""
            select {_COLUMNS} from public.handover_requests
            where tenant_id = app.current_tenant_id()
              and (dedupe_key = %s
                   or (wa_id = %s and product_key = %s and status <> 'resolved'))
            order by (dedupe_key = %s) desc, created_at desc
            limit 1
            """,
            (dedupe, wa_id, key, dedupe),
        )
        existing = _row(cursor, cursor.fetchone())
        if existing is None:
            raise RuntimeError(
                "Handover insert conflicted but no existing request could be found."
            )
        return existing, False


def get_request(
    tenant_id: str, handover_id: str, database_url: str | None = None
) -> dict[str, Any] | None:
    """Fetch one request, or None when it does not belong to this tenant."""
    with tenant_transaction(tenant_id, database_url) as cursor:
        cursor.execute(
            f"select {_COLUMNS} from public.handover_requests "
            "where tenant_id = app.current_tenant_id() and id = %s",
            (handover_id,),
        )
        return _row(cursor, cursor.fetchone())


def list_requests(
    tenant_id: str,
    *,
    status: str | None = None,
    limit: int = 200,
    database_url: str | None = None,
) -> list[dict[str, Any]]:
    """The tenant's requests, newest first, optionally filtered by status."""
    limit = max(1, min(int(limit), 500))
    with tenant_transaction(tenant_id, database_url) as cursor:
        if status:
            cursor.execute(
                f"select {_COLUMNS} from public.handover_requests "
                "where tenant_id = app.current_tenant_id() and status = %s "
                "order by created_at desc limit %s",
                (status, limit),
            )
        else:
            cursor.execute(
                f"select {_COLUMNS} from public.handover_requests "
                "where tenant_id = app.current_tenant_id() "
                "order by created_at desc limit %s",
                (limit,),
            )
        return _rows(cursor, cursor.fetchall())


def open_requests(
    tenant_id: str, *, limit: int = 200, database_url: str | None = None
) -> list[dict[str, Any]]:
    """Requests staff still have to act on (anything not resolved)."""
    limit = max(1, min(int(limit), 500))
    with tenant_transaction(tenant_id, database_url) as cursor:
        cursor.execute(
            f"select {_COLUMNS} from public.handover_requests "
            "where tenant_id = app.current_tenant_id() and status <> 'resolved' "
            "order by created_at desc limit %s",
            (limit,),
        )
        return _rows(cursor, cursor.fetchall())


def mark_notified(
    tenant_id: str, handover_id: str, target: str | None, database_url: str | None = None
) -> None:
    """Record a delivered staff notification. Never called unless delivery succeeded."""
    with tenant_transaction(tenant_id, database_url) as cursor:
        cursor.execute(
            """
            update public.handover_requests
            set status = 'notified', notification_target = %s, notification_error = null,
                attempts = attempts + 1, notified_at = now()
            where tenant_id = app.current_tenant_id() and id = %s
            """,
            (target, handover_id),
        )


def mark_failed(
    tenant_id: str, handover_id: str, error: str, database_url: str | None = None
) -> None:
    """Record a failed notification so it is visible in the dashboard and retryable."""
    with tenant_transaction(tenant_id, database_url) as cursor:
        cursor.execute(
            """
            update public.handover_requests
            set status = 'failed', notification_error = %s, attempts = attempts + 1
            where tenant_id = app.current_tenant_id() and id = %s
            """,
            (str(error or "notification failed")[:500], handover_id),
        )


def set_status(
    tenant_id: str, handover_id: str, status: str, database_url: str | None = None
) -> bool:
    """Move a request through the staff-driven part of its lifecycle."""
    if status not in STAFF_STATUSES:
        raise ValueError(f"Unsupported handover status: {status!r}")
    with tenant_transaction(tenant_id, database_url) as cursor:
        cursor.execute(
            """
            update public.handover_requests
            set status = %s,
                resolved_at = case when %s = 'resolved' then now() else null end
            where tenant_id = app.current_tenant_id() and id = %s
            returning id
            """,
            (status, status, handover_id),
        )
        return cursor.fetchone() is not None


def latest_open_request(
    tenant_id: str, wa_id: str, database_url: str | None = None
) -> dict[str, Any] | None:
    """The most recent unresolved request for one customer, if there is one.

    The safety-net path uses this so a customer who keeps asking is not turned into a stream
    of near-identical requests for staff.
    """
    with tenant_transaction(tenant_id, database_url) as cursor:
        cursor.execute(
            f"select {_COLUMNS} from public.handover_requests "
            "where tenant_id = app.current_tenant_id() and wa_id = %s "
            "and status <> 'resolved' order by created_at desc limit 1",
            (wa_id,),
        )
        return _row(cursor, cursor.fetchone())


def count_open(tenant_id: str, database_url: str | None = None) -> int:
    """How many requests still need staff attention (used for dashboard hints)."""
    with tenant_transaction(tenant_id, database_url) as cursor:
        cursor.execute(
            "select count(*) from public.handover_requests "
            "where tenant_id = app.current_tenant_id() and status <> 'resolved'"
        )
        return int(cursor.fetchone()[0])

