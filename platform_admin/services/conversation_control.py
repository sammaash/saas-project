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

Inbox states (no extra column; derived from ``ai_mode`` + ``needs_attention``):
    green  AI_ACTIVE        ai_mode = ai_active, no alert
    orange HUMAN_REQUESTED  needs_attention (the customer asked for a person;
                            ``request_human`` also moves ai_active -> paused)
    red    HUMAN_HANDLING   ai_mode in (paused, human), no alert
Staff taking over (or replying) and Resume AI clear the alert.
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


def indicator(ai_mode: str | None, needs_attention: bool | None) -> str:
    """Inbox colour: ``green`` AI replying, ``orange`` human requested, ``red`` human.

    Orange wins: a customer who asked for a person needs staff even while AI is silent.
    """
    if needs_attention:
        return "orange"
    if (ai_mode or "ai_active") == "ai_active":
        return "green"
    return "red"


INDICATOR_LABELS = {
    "green": "AI active",
    "orange": "Customer asked for a person",
    "red": "Human handling - AI paused",
}


def get_state(
    settings: Settings, tenant_id: str, conversation_id: str
) -> tuple[str, bool] | None:
    """The authoritative ``(ai_mode, needs_attention)`` pair, or None outside the tenant."""
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            "select ai_mode, needs_attention from public.conversations "
            "where id = %s and tenant_id = app.current_tenant_id()",
            (conversation_id,),
        )
        row = cursor.fetchone()
        return (str(row[0]), bool(row[1])) if row else None


def clear_attention_locked(
    cursor: Any, conversation_id: str, actor_user_id: str | None, source: str
) -> None:
    """Clear the alert inside the caller's locked transaction and audit it.

    The caller has already established, under the row lock, that the flag is set.
    """
    cursor.execute(
        "update public.conversations set needs_attention = false "
        "where id = %s and tenant_id = app.current_tenant_id()",
        (conversation_id,),
    )
    record_event(
        cursor,
        conversation_id=conversation_id,
        actor_user_id=actor_user_id,
        event_type="needs_attention_cleared",
        previous_mode=None,
        new_mode=None,
        metadata={"source": source},
    )


def request_human(
    settings: Settings,
    *,
    tenant_id: str,
    conversation_id: str,
    source: str,
) -> dict[str, Any]:
    """The customer asked for a person: stop automation and raise the orange alert.

    One transaction under the conversation row lock (the dispatch lock), so an
    automated send that has not started yet sees the new mode and is suppressed.
    ``ai_active`` becomes ``paused``; a conversation staff already own keeps its mode
    and only gains the alert. Idempotent: a repeat changes nothing and writes no
    event. System actor, audited with ``source``.
    """
    try:
        with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
            cursor.execute(
                "select set_config('lock_timeout', %s, true)",
                (outbound_dispatch.LOCK_TIMEOUT_SQL,),
            )
            cursor.execute(
                "select ai_mode, needs_attention from public.conversations "
                "where id = %s and tenant_id = app.current_tenant_id() for update",
                (conversation_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ServiceError("Conversation not found.", 404)
            previous, attention = str(row[0]), bool(row[1])
            new_mode = "paused" if previous == "ai_active" else previous
            if new_mode == previous and attention:
                return {"changed": False, "previous_mode": previous, "mode": previous}
            cursor.execute(
                "update public.conversations set ai_mode = %s, needs_attention = true, "
                "updated_at = now() where id = %s and tenant_id = app.current_tenant_id()",
                (new_mode, conversation_id),
            )
            if new_mode != previous:
                record_event(
                    cursor,
                    conversation_id=conversation_id,
                    actor_user_id=None,
                    event_type="ai_paused",
                    previous_mode=previous,
                    new_mode=new_mode,
                    metadata={"source": source, "reason": "human_requested"},
                )
            if not attention:
                record_event(
                    cursor,
                    conversation_id=conversation_id,
                    actor_user_id=None,
                    event_type="needs_attention_set",
                    previous_mode=None,
                    new_mode=None,
                    metadata={"source": source},
                )
            return {"changed": True, "previous_mode": previous, "mode": new_mode}
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


def record_generation_skipped(
    settings: Settings,
    *,
    tenant_id: str,
    conversation_id: str,
    mode: str,
    message_id: str,
) -> None:
    """Audit an inbound message that got no automated turn because AI is not active."""
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        record_event(
            cursor,
            conversation_id=conversation_id,
            actor_user_id=None,
            event_type=outbound_dispatch.SUPPRESSED_CODE,
            previous_mode=mode,
            new_mode=mode,
            metadata={"stage": "before_generation", "message_id": str(message_id)[:200]},
        )


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
                "select ai_mode, needs_attention from public.conversations "
                "where id = %s and tenant_id = app.current_tenant_id() for update",
                (conversation_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ServiceError("Conversation not found.", 404)
            previous, attention = row[0], bool(row[1])
            # Staff taking over or resuming answers an orange alert (red/green after).
            acknowledges = (
                attention and actor_user_id is not None and new_mode in ("human", "ai_active")
            )
            if previous == new_mode:
                if acknowledges:
                    clear_attention_locked(
                        cursor, conversation_id, actor_user_id, f"set_mode:{new_mode}"
                    )
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
            if acknowledges:
                clear_attention_locked(
                    cursor, conversation_id, actor_user_id, f"set_mode:{new_mode}"
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
