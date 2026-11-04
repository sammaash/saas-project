"""Authoritative per-conversation AI control state and its audit trail.

The backend — never the interface — decides whether automation may reply. Every
transition runs in one transaction that takes the conversation row lock (the same
lock ``outbound_dispatch`` holds across a provider send), so a mode change and an
in-flight dispatch are totally ordered. The mode change and its audit event
commit atomically: no event row exists without the corresponding change.

Lock contention beyond ``LOCK_TIMEOUT_SQL`` (5s) aborts the transaction and is
reported as HTTP 409 — nothing changes and no audit row is written.

Transition matrix (see docs/INBOX.md):
    ai_active -> paused    (Pause AI)
    ai_active -> human     (Take over)
    paused    -> human     (Take over)
    paused    -> ai_active (Resume AI)
    human     -> ai_active (Resume AI)
Same-mode requests are idempotent no-ops (no event). ``human -> paused`` is
rejected: human ownership already implies automation stays silent.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg.types.json import Jsonb

from saas.tenant_context import tenant_transaction

from ..config import Settings
from . import outbound_dispatch
from .validation import ServiceError

logger = logging.getLogger("platform_admin")

MODES = ("ai_active", "paused", "human")

ALLOWED_TRANSITIONS = frozenset(
    {
        ("ai_active", "paused"),
        ("ai_active", "human"),
        ("paused", "human"),
        ("paused", "ai_active"),
        ("human", "ai_active"),
    }
)

_TRANSITION_EVENTS = {
    ("ai_active", "paused"): "ai_paused",
    ("paused", "ai_active"): "ai_resumed",
    ("human", "ai_active"): "ai_resumed",
}


def _event_for(previous: str, new: str) -> str:
    return _TRANSITION_EVENTS.get((previous, new), "human_takeover")


def _lock_timeout(error: Exception) -> bool:
    return getattr(error, "sqlstate", None) == "55P03"


def record_event(
    cursor: Any,
    *,
    conversation_id: str,
    actor_user_id: str | None,
    event_type: str,
    previous_mode: str | None,
    new_mode: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one immutable audit row. System events carry no actor uuid."""
    cursor.execute(
        """
        insert into public.conversation_control_events
          (tenant_id, conversation_id, actor_user_id, actor_type,
           event_type, previous_mode, new_mode, metadata)
        values (app.current_tenant_id(), %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            conversation_id,
            actor_user_id,
            "system" if actor_user_id is None else "user",
            event_type,
            previous_mode,
            new_mode,
            Jsonb(metadata or {}),
        ),
    )


def set_mode(
    settings: Settings,
    *,
    actor_user_id: str | None,
    tenant_id: str,
    conversation_id: str,
    new_mode: str,
) -> str:
    """Apply one AI-mode transition. Returns ``"changed"`` or ``"unchanged"``.

    ``actor_user_id`` is the server-verified tenant user; None means a system
    action. Raises ServiceError 409 for disallowed transitions and for lock
    contention, 404 for a conversation outside the tenant.
    """
    if new_mode not in MODES:
        raise ServiceError("Unknown conversation mode.", 400)
    try:
        with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
            cursor.execute(
                "select set_config('lock_timeout', %s, true)",
                (outbound_dispatch.LOCK_TIMEOUT_SQL,),
            )
            cursor.execute(
                "select ai_mode from public.conversations "
                "where id = %s and tenant_id = app.current_tenant_id() for update",
                (conversation_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ServiceError("Conversation not found.", 404)
            previous = row[0]
            if previous == new_mode:
                return "unchanged"
            if (previous, new_mode) not in ALLOWED_TRANSITIONS:
                raise ServiceError("That mode change is not allowed.", 409)
            cursor.execute(
                "update public.conversations set ai_mode = %s, updated_at = now() "
                "where id = %s and tenant_id = app.current_tenant_id()",
                (new_mode, conversation_id),
            )
            record_event(
                cursor,
                conversation_id=conversation_id,
                actor_user_id=actor_user_id,
                event_type=_event_for(previous, new_mode),
                previous_mode=previous,
                new_mode=new_mode,
            )
            return "changed"
    except ServiceError:
        raise
    except Exception as error:  # noqa: BLE001
        if getattr(error, "sqlstate", None) in ("42703", "42P01"):
            raise ServiceError(
                "Conversation control schema is missing (migration 0039).", 503
            ) from error
        if _lock_timeout(error):
            raise ServiceError("Conversation is busy - try again.", 409) from error
        raise


def set_needs_attention(
    settings: Settings,
    *,
    actor_user_id: str | None,
    tenant_id: str,
    conversation_id: str,
    active: bool,
) -> str:
    """Mark or clear the needs-attention flag. Returns ``"changed"``/``"unchanged"``."""
    try:
        with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
            cursor.execute(
                "select set_config('lock_timeout', %s, true)",
                (outbound_dispatch.LOCK_TIMEOUT_SQL,),
            )
            cursor.execute(
                "select needs_attention from public.conversations "
                "where id = %s and tenant_id = app.current_tenant_id() for update",
                (conversation_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ServiceError("Conversation not found.", 404)
            if bool(row[0]) == active:
                return "unchanged"
            cursor.execute(
                "update public.conversations set needs_attention = %s, updated_at = now() "
                "where id = %s and tenant_id = app.current_tenant_id()",
                (active, conversation_id),
            )
            record_event(
                cursor,
                conversation_id=conversation_id,
                actor_user_id=actor_user_id,
                event_type="needs_attention_set" if active else "needs_attention_cleared",
                previous_mode=None,
                new_mode=None,
            )
            return "changed"
    except ServiceError:
        raise
    except Exception as error:  # noqa: BLE001
        if getattr(error, "sqlstate", None) in ("42703", "42P01"):
            raise ServiceError(
                "Conversation control schema is missing (migration 0039).", 503
            ) from error
        if _lock_timeout(error):
            raise ServiceError("Conversation is busy - try again.", 409) from error
        raise
