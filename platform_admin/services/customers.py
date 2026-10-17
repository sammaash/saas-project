"""Tenant-isolated customer, conversation and message management.

Every function runs inside ``tenant_transaction`` so that RLS confines all reads
and writes to exactly one tenant. The bot and dashboard connect as ``app_backend``,
which holds the only privileges on these tables.

Identity is WhatsApp-native: ``wa_id`` (the ``from`` field on inbound webhooks)
is the stable customer identity within a tenant. A customer may have many
conversations; each conversation belongs to one channel and carries a lifecycle
status (open/closed/archived). Messages are the durable, chronological record of
inbound and outbound traffic, with provider-message-id deduplication as the
idempotency backstop.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg.types.json import Jsonb

from saas.db import connect
from saas.tenant_context import tenant_transaction

from ..config import Settings
from .validation import ServiceError

logger = logging.getLogger("platform_admin.customers")


class DuplicateReplyError(ServiceError):
    """A reply for this inbound message has already been recorded.

    Raised when ``messages_outbound_reply_unique`` rejects a second outbound row for the
    same inbound provider message. The caller must treat this as "look at the existing
    row", never as a generic persistence failure: it is the database enforcing
    at-most-one-reply-per-inbound-message.
    """

    def __init__(self, message: str = "This message has already been answered."):
        super().__init__(message, 409)


class SchemaOutOfDateError(ServiceError):
    """A column this code needs does not exist yet (migration 0035 not applied).

    The webhook treats this as "use the pre-0035 persistence path" rather than as an
    error, so deploying this code ahead of the migration degrades instead of silently
    losing outbound message history.
    """

    def __init__(self, message: str = "The database schema is older than this code."):
        super().__init__(message, 503)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

def get_or_create_customer(
    settings: Settings,
    tenant_id: str,
    channel_id: str,
    wa_id: str,
    name: str | None = None,
    phone_normalized: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """Return the customer for ``wa_id`` within ``tenant_id``, creating it if needed.

    ``channel_id`` is validated to belong to the tenant (FK on conversations
    enforces this later) but is not stored on the customer itself -- a customer's
    identity is tenant-scoped, not channel-scoped.
    """
    wa_id = (wa_id or "").strip()
    if not wa_id:
        raise ServiceError("A WhatsApp identity (wa_id) is required.", 400)

    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            insert into public.customers
                (tenant_id, wa_id, name, phone_normalized, email, metadata,
                 last_interaction_at)
            values (app.current_tenant_id(), %s, %s, %s, %s, '{}'::jsonb, now())
            on conflict (tenant_id, wa_id) do update
              set name        = coalesce(excluded.name, public.customers.name),
                  phone_normalized = coalesce(excluded.phone_normalized, public.customers.phone_normalized),
                  email       = coalesce(excluded.email, public.customers.email),
                  updated_at  = now(),
                  last_interaction_at = now()
            returning id, tenant_id, wa_id, name, phone_normalized, email,
                      metadata, created_at, updated_at, last_interaction_at
            """,
            (wa_id, name, phone_normalized, email),
        )
        row = cursor.fetchone()
        return _row_to_dict(cursor, row)



def get_customer(
    settings: Settings, tenant_id: str, customer_id: str
) -> dict[str, Any] | None:
    """Fetch one customer by id, or None if it does not belong to this tenant."""
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            select id, tenant_id, wa_id, name, phone_normalized, email,
                   metadata, created_at, updated_at, last_interaction_at
            from public.customers
            where id = %s
            """,
            (customer_id,),
        )
        row = cursor.fetchone()
        return _row_to_dict(cursor, row) if row else None


def list_customers(
    settings: Settings,
    tenant_id: str,
    *,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """List a tenant's customers, optionally filtered by a name/wa_id/email search.

    Returns ``(rows, total_count)``. Rows are ordered by most recent interaction.
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size

    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        where = "where tenant_id = app.current_tenant_id()"
        params: list[Any] = []
        if search:
            like = f"%{search.lower()}%"
            where += """ and (
                lower(wa_id) like %s or lower(coalesce(name, '')) like %s
                or lower(coalesce(email, '')) like %s
            )"""
            params.extend([like, like, like])

        cursor.execute(
            f"""
            select count(*) from public.customers {where}
            """,
            params,
        )
        total = cursor.fetchone()[0]

        cursor.execute(
            f"""
            select id, tenant_id, wa_id, name, phone_normalized, email,
                   metadata, created_at, updated_at, last_interaction_at
            from public.customers
            {where}
            order by
              case when last_interaction_at is null then 1 else 0 end,
              last_interaction_at desc nulls last,
              created_at desc
            limit %s offset %s
            """,
            [*params, page_size, offset],
        )
        rows = [_row_to_dict(cursor, r) for r in cursor.fetchall()]
        return rows, total



# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def get_or_create_conversation(
    settings: Settings,
    tenant_id: str,
    customer_id: str,
    channel_id: str,
) -> dict[str, Any]:
    """Return the most recent open conversation for ``customer_id``/``channel_id``,
    creating one if none exists. Re-uses an open conversation so a continuous
    WhatsApp thread stays in a single conversation row.
    """
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        # Reuse the most recent open conversation for this customer on this channel.
        cursor.execute(
            """
            select id, tenant_id, customer_id, channel_id,
                   provider_conversation_id, status, assigned_to,
                   created_at, updated_at, last_message_at, closed_at
            from public.conversations
            where tenant_id = app.current_tenant_id()
              and customer_id = %s
              and channel_id = %s
              and status = 'open'
            order by updated_at desc
            limit 1
            """,
            (customer_id, channel_id),
        )
        row = cursor.fetchone()
        if row:
            return _row_to_dict(cursor, row)

        cursor.execute(
            """
            insert into public.conversations
                (tenant_id, customer_id, channel_id, status, last_message_at)
            values (app.current_tenant_id(), %s, %s, 'open', now())
            returning id, tenant_id, customer_id, channel_id,
                      provider_conversation_id, status, assigned_to,
                      created_at, updated_at, last_message_at, closed_at
            """,
            (customer_id, channel_id),
        )
        return _row_to_dict(cursor, cursor.fetchone())


def get_conversation(
    settings: Settings, tenant_id: str, conversation_id: str
) -> dict[str, Any] | None:
    """Fetch one conversation by id, or None if it does not belong to this tenant."""
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            select id, tenant_id, customer_id, channel_id,
                   provider_conversation_id, status, assigned_to,
                   created_at, updated_at, last_message_at, closed_at
            from public.conversations
            where id = %s
            """,
            (conversation_id,),
        )
        row = cursor.fetchone()
        return _row_to_dict(cursor, row) if row else None


def list_conversations(
    settings: Settings,
    tenant_id: str,
    *,
    customer_id: str | None = None,
    channel_id: str | None = None,
    status: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """List a tenant's conversations with optional customer/channel/status/search filters."""
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size

    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        where = "where c.tenant_id = app.current_tenant_id()"
        params: list[Any] = []
        if customer_id:
            where += " and c.customer_id = %s"
            params.append(customer_id)
        if channel_id:
            where += " and c.channel_id = %s"
            params.append(channel_id)
        if status:
            where += " and c.status = %s"
            params.append(status)
        if search:
            like = f"%{search.lower()}%"
            where += """ and (
                lower(c.provider_conversation_id) like %s or
                lower(cast(c.id as text)) like %s
            )"""
            params.extend([like, like])

        cursor.execute(
            f"""
            select count(*) from public.conversations c {where}
            """,
            params,
        )
        total = cursor.fetchone()[0]

        cursor.execute(
            f"""
            select c.id, c.tenant_id, c.customer_id, c.channel_id,
                   c.provider_conversation_id, c.status, c.assigned_to,
                   c.created_at, c.updated_at, c.last_message_at, c.closed_at,
                   cu.wa_id as customer_wa_id,
                   cu.name as customer_name
            from public.conversations c
            join public.customers cu on cu.id = c.customer_id
            {where}
            order by c.last_message_at desc nulls last, c.updated_at desc
            limit %s offset %s
            """,
            [*params, page_size, offset],
        )
        rows = [_row_to_dict(cursor, r) for r in cursor.fetchall()]
        return rows, total


def update_conversation_activity(
    settings: Settings, tenant_id: str, conversation_id: str
) -> None:
    """Touch ``last_message_at`` and ``updated_at`` on a conversation."""
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            update public.conversations
            set last_message_at = now(), updated_at = now()
            where id = %s and tenant_id = app.current_tenant_id()
            """,
            (conversation_id,),
        )



# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def save_message(
    settings: Settings,
    tenant_id: str,
    channel_id: str,
    conversation_id: str,
    *,
    direction: str,
    sender_type: str,
    provider_message_id: str | None = None,
    message_type: str = "text",
    body_text: str | None = None,
    payload: dict[str, Any] | None = None,
    provider_timestamp: str | None = None,
    delivery_status: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    reply_to_provider_message_id: str | None = None,
) -> dict[str, Any]:
    """Persist one message.

    ``direction`` in ``inbound``/``outbound``; ``sender_type`` in
    ``customer``/``assistant``/``staff``/``system``. Returns the inserted row.
    Raises ``ServiceError`` on provider-message-id duplication (idempotency) and
    ``DuplicateReplyError`` when this inbound message already has an outbound reply.
    """
    if direction not in ("inbound", "outbound"):
        raise ServiceError("Invalid message direction.", 400)
    if sender_type not in ("customer", "assistant", "staff", "system"):
        raise ServiceError("Invalid sender type.", 400)
    if message_type not in (
        "text", "image", "document", "audio", "video", "interactive",
        "location", "contacts", "other",
    ):
        raise ServiceError("Invalid message type.", 400)
    if delivery_status and delivery_status not in ("sent", "delivered", "read", "failed"):
        raise ServiceError("Invalid delivery status.", 400)

    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        # The new column is referenced ONLY when a value is supplied. That keeps the
        # statement byte-for-byte equivalent to the pre-0035 one for every existing caller
        # (inbound messages and dashboard writes), so an un-migrated database keeps
        # persisting history instead of failing every insert.
        columns = [
            "tenant_id", "channel_id", "conversation_id", "direction", "sender_type",
            "provider_message_id", "message_type", "body_text", "payload",
            "provider_timestamp", "delivery_status", "error_code", "error_message",
        ]
        parameters: list[Any] = [
            channel_id, conversation_id, direction, sender_type, provider_message_id,
            message_type, body_text, Jsonb(payload) if payload is not None else None,
            provider_timestamp, delivery_status, error_code, error_message,
        ]
        returning = (
            "id, tenant_id, channel_id, conversation_id, direction, sender_type, "
            "provider_message_id, message_type, body_text, payload, provider_timestamp, "
            "received_at, delivery_status, error_code, error_message"
        )
        if reply_to_provider_message_id is not None:
            columns.append("reply_to_provider_message_id")
            parameters.append(reply_to_provider_message_id)
            returning += ", reply_to_provider_message_id"
        returning += ", created_at, updated_at"
        placeholders = ", ".join("%s" for _ in parameters)
        try:
            cursor.execute(
                f"insert into public.messages ({', '.join(columns)}) "
                f"values (app.current_tenant_id(), {placeholders}) "
                f"returning {returning}",
                tuple(parameters),
            )
        except ServiceError:
            raise
        except Exception as error:  # noqa: BLE001 - translate expected failures
            constraint = getattr(getattr(error, "diag", None), "constraint_name", "") or ""
            message = str(error)
            if "reply_to_provider_message_id" in message and "does not exist" in message:
                # 0035 has not been applied to this database yet.
                raise SchemaOutOfDateError() from error
            if type(error).__name__ == "UndefinedColumn":
                raise SchemaOutOfDateError() from error
            if "messages_outbound_reply_unique" in constraint or "messages_outbound_reply_unique" in message:
                raise DuplicateReplyError() from error
            if "messages_provider_message_unique" in constraint or "messages_provider_message_unique" in message:
                raise ServiceError(
                    "A message with this provider message id already exists.", 409
                ) from error
            raise ServiceError(f"Failed to save message: {message}", 500) from error
        row = cursor.fetchone()
        return _row_to_dict(cursor, row)


def find_outbound_reply(
    settings: Settings,
    tenant_id: str,
    channel_id: str,
    inbound_provider_message_id: str,
) -> dict[str, Any] | None:
    """The outbound row recorded for one inbound message, if any.

    ``provider_message_id`` being set means the reply reached Meta. It being null means an
    earlier attempt recorded the intent but no successful send was recorded: the ambiguous
    case, which is never automatically resent for a non-repeatable reply.
    """
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            select id, tenant_id, channel_id, conversation_id, direction, sender_type,
                   provider_message_id, message_type, body_text, payload,
                   provider_timestamp, received_at, delivery_status,
                   error_code, error_message, reply_to_provider_message_id,
                   created_at, updated_at
            from public.messages
            where tenant_id = app.current_tenant_id()
              and channel_id = %s
              and direction = 'outbound'
              and reply_to_provider_message_id = %s
            """,
            (channel_id, inbound_provider_message_id),
        )
        return _row_to_dict(cursor, cursor.fetchone())


def mark_message_sent(
    settings: Settings,
    tenant_id: str,
    message_id: str,
    provider_message_id: str,
) -> None:
    """Attach the provider's wamid and mark the outbound row as sent."""
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            update public.messages
            set provider_message_id = %s,
                delivery_status = 'sent',
                error_code = null,
                error_message = null
            where id = %s and tenant_id = app.current_tenant_id()
            """,
            (provider_message_id, message_id),
        )


def mark_message_failed(
    settings: Settings,
    tenant_id: str,
    message_id: str,
    *,
    error_code: str | None,
    error_message: str | None,
) -> None:
    """Record a failed send on the outbound row, keeping Meta's own diagnostic fields."""
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            update public.messages
            set delivery_status = 'failed',
                error_code = %s,
                error_message = %s
            where id = %s and tenant_id = app.current_tenant_id()
            """,
            (
                (error_code or "")[:40] or None,
                (error_message or "")[:500] or None,
                message_id,
            ),
        )


def update_delivery_status(
    settings: Settings,
    tenant_id: str,
    *,
    status: str,
    provider_message_id: str | None = None,
    row_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    """Apply one delivery receipt. Returns the number of rows changed (0 or 1).

    Status webhooks are neither ordered nor deduplicated, so the UPDATE allows only a
    forward transition: null -> anything, sent -> anything, failed -> delivered/read,
    delivered -> read. ``read`` is terminal, and ``failed`` arriving after a success is
    ignored because the message demonstrably arrived.

    Matching is by wamid where available, otherwise by the outbound row id sent as
    ``biz_opaque_callback_data``. Rows are never created here: an unknown id is a no-op.
    """
    if status not in ("sent", "delivered", "read", "failed"):
        return 0
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            update public.messages
            set delivery_status = %s,
                error_code = coalesce(%s, error_code),
                error_message = coalesce(%s, error_message)
            where tenant_id = app.current_tenant_id()
              and (
                    (%s::text is not null and provider_message_id = %s::text)
                 or (%s::uuid is not null and id = %s::uuid)
              )
              and (
                    delivery_status is null
                 or delivery_status = 'sent'
                 or (delivery_status = 'failed' and %s in ('delivered', 'read'))
                 or (delivery_status = 'delivered' and %s = 'read')
              )
            """,
            (
                status,
                (error_code or "")[:40] or None,
                (error_message or "")[:500] or None,
                provider_message_id, provider_message_id,
                row_id, row_id,
                status, status,
            ),
        )
        return cursor.rowcount


def list_messages(
    settings: Settings,
    tenant_id: str,
    conversation_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """List messages in one conversation, oldest first."""
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size

    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            select count(*) from public.messages
            where conversation_id = %s and tenant_id = app.current_tenant_id()
            """,
            (conversation_id,),
        )
        total = cursor.fetchone()[0]

        cursor.execute(
            """
            select id, tenant_id, channel_id, conversation_id, direction, sender_type,
                   provider_message_id, message_type, body_text, payload,
                   provider_timestamp, received_at, delivery_status,
                   error_code, error_message, created_at, updated_at
            from public.messages
            where conversation_id = %s and tenant_id = app.current_tenant_id()
            order by received_at asc, id asc
            limit %s offset %s
            """,
            (conversation_id, page_size, offset),
        )
        rows = [_row_to_dict(cursor, r) for r in cursor.fetchall()]
        return rows, total



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    columns = [column.name for column in cursor.description]
    result: dict[str, Any] = {}
    for column, value in zip(columns, row, strict=True):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        result[column] = value
    return result

