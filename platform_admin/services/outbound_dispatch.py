"""The single dispatch boundary for customer-facing sends.

Every provider send to a customer runs inside one transaction that

1. takes the conversation row lock (``select ... for update``),
2. rechecks the conversation's ``ai_mode`` immediately before dispatch,
3. performs the provider call while the lock is held (bounded by the adapter's
   own 10-second network timeout),
4. finalizes the outbound row in the same transaction,

and only then commits. Control changes (pause / takeover) take the same row lock,
so dispatch and control are totally ordered per conversation: if a takeover
commits first, the dispatch sees the new mode and the send never begins; if the
dispatch holds the lock first, its send completes before the takeover can commit.
A provider-accepted message is never recalled.

If the provider accepts a message and the database finalization or commit then
fails, the transaction rolls back and the row keeps no provider id and no error
fields: the outcome is *unknown* and is never retried automatically
(docs/OUTBOUND_DELIVERY.md).

When migration 0039 is not applied (no ``ai_mode`` column), dispatch FAILS CLOSED
with a 503: there is no legacy fallback in this release. The startup schema guard
(``REQUIRED_SCHEMA`` / ``verify_startup_database``) refuses to boot an un-migrated
database first; this raise is the runtime backstop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from psycopg.types.json import Jsonb

from saas.tenant_context import tenant_transaction

from ..config import Settings
from .validation import ServiceError

logger = logging.getLogger("platform_admin")

#: Production lock timeout for the dispatch and control critical sections.
LOCK_TIMEOUT_SQL = "5s"

#: Unified disposition token (message error_code + system audit event type).
SUPPRESSED_CODE = "ai_reply_suppressed"
UNSENT_CODE = "ai_reply_unsent"


class SendFailed(Exception):
    """A provider send that failed with known diagnostics.

    ``ambiguous`` marks an outcome that may have been accepted (timeout, reset,
    malformed response): the row is finalised as *unknown* and never resent.
    """

    def __init__(
        self,
        error_code: str | None,
        error_message: str | None = None,
        *,
        ambiguous: bool = False,
    ):
        super().__init__(error_message or error_code or "send failed")
        self.error_code = error_code
        self.error_message = error_message
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class DispatchOutcome:
    """What actually happened at the dispatch boundary."""

    kind: str  # "sent" | "suppressed" | "failed" | "unknown"
    wamid: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    #: Exception class name of the failure (for event bookkeeping parity).
    error_type: str | None = None


def _missing_0039(error: Exception) -> bool:
    """True when the database simply lacks the 0039 columns/tables."""
    sqlstate = getattr(error, "sqlstate", None)
    return sqlstate in ("42703", "42P01")  # undefined_column / undefined_table


def _lock_timeout(error: Exception) -> bool:
    return getattr(error, "sqlstate", None) == "55P03"


def _failure_fields(error: Exception) -> tuple[str, str, bool]:
    code = (
        getattr(error, "error_code", None)
        or getattr(error, "provider_code", None)
        or type(error).__name__
    )
    message = (
        getattr(error, "error_message", None)
        or getattr(error, "provider_message", None)
        or getattr(error, "message", None)
        or str(error)
    )
    return str(code)[:40], str(message)[:500], bool(getattr(error, "ambiguous", False))


def dispatch_reply(
    settings: Settings,
    *,
    tenant_id: str,
    conversation_id: str | None,
    message_row_id: str | None,
    sender_type: str,
    send: Callable[[], str],
) -> DispatchOutcome:
    """Run the dispatch critical section for one recorded outbound intent.

    ``send`` performs the provider call and returns the provider message id
    (wamid). It is invoked while the conversation row lock is held, only after the
    mode recheck passed. ``message_row_id`` may be None when no correlated row was
    recorded: the send is still gated and locked, only the row finalisation is
    skipped.
    """

    def _body(cursor: Any) -> DispatchOutcome:
        cursor.execute(
            "select set_config('lock_timeout', %s, true)", (LOCK_TIMEOUT_SQL,)
        )
        mode = None
        if conversation_id:
            cursor.execute(
                "select ai_mode from public.conversations "
                "where id = %s and tenant_id = app.current_tenant_id() for update",
                (conversation_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ServiceError("Conversation not found.", 404)
            mode = row[0]
        if sender_type != "staff" and mode not in (None, "ai_active"):
            _mark_suppressed(cursor, conversation_id, message_row_id, mode)
            return DispatchOutcome(
                "suppressed",
                error_code=SUPPRESSED_CODE,
                error_message=f"Suppressed: conversation mode is {mode}",
            )
        try:
            wamid = send()
        except Exception as error:  # noqa: BLE001 - finalise whatever the provider said
            code, message, ambiguous = _failure_fields(error)
            if message_row_id:
                _mark_error(cursor, message_row_id, code, message, ambiguous)
            return DispatchOutcome(
                "unknown" if ambiguous else "failed",
                error_code=code,
                error_message=message,
                error_type=type(error).__name__,
            )
        if message_row_id:
            cursor.execute(
                """
                update public.messages
                set provider_message_id = %s,
                    delivery_status = 'sent',
                    error_code = null,
                    error_message = null
                where id = %s and tenant_id = app.current_tenant_id()
                """,
                (wamid, message_row_id),
            )
        return DispatchOutcome("sent", wamid=wamid)

    try:
        with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
            return _body(cursor)
    except ServiceError:
        raise
    except Exception as error:  # noqa: BLE001
        if _missing_0039(error):
            # Fail closed: without migration 0039 there is no ai_mode to enforce, so
            # NO automated customer-facing send may happen at all. The startup schema
            # guard (REQUIRED_SCHEMA) refuses to boot such a database first; this is
            # the runtime backstop. There is no legacy fallback in this release.
            raise ServiceError(
                "Conversation control schema is missing (migration 0039).", 503
            ) from error
        if _lock_timeout(error):
            raise ServiceError("Conversation is busy - try again.", 409) from error
        raise


def _mark_suppressed(
    cursor: Any,
    conversation_id: str | None,
    message_row_id: str | None,
    mode: str | None,
) -> None:
    if message_row_id:
        cursor.execute(
            """
            update public.messages
            set error_code = %s, error_message = %s
            where id = %s and tenant_id = app.current_tenant_id()
              and provider_message_id is null
            """,
            (SUPPRESSED_CODE, f"Suppressed: conversation mode is {mode}"[:500], message_row_id),
        )
    if conversation_id:
        cursor.execute(
            """
            insert into public.conversation_control_events
              (tenant_id, conversation_id, actor_user_id, actor_type,
               event_type, previous_mode, new_mode, metadata)
            values (app.current_tenant_id(), %s, null, 'system', %s, %s, null, %s)
            """,
            (
                conversation_id,
                SUPPRESSED_CODE,
                mode,
                Jsonb({"message_row_id": message_row_id} if message_row_id else {}),
            ),
        )


def _mark_error(
    cursor: Any,
    message_row_id: str,
    error_code: str,
    error_message: str,
    ambiguous: bool,
) -> None:
    """Record a definitive failure, or an ambiguous outcome that is never resent."""
    cursor.execute(
        """
        update public.messages
        set delivery_status = %s,
            error_code = %s,
            error_message = %s
        where id = %s and tenant_id = app.current_tenant_id()
        """,
        (
            None if ambiguous else "failed",
            error_code or None,
            error_message or None,
            message_row_id,
        ),
    )