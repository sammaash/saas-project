"""Shared dispatch-boundary fake for mock-style webhook tests.

Tests that stub service functions (instead of a database) replace
``outbound_dispatch.dispatch_reply`` with ``fake_dispatch``: it performs the
provider call and mirrors the boundary's bookkeeping through the patchable
service functions, so each test's original assertions about sends and message
bookkeeping remain meaningful. The real boundary is covered by
``tests/test_outbound_dispatch.py``.
"""

from __future__ import annotations

from platform_admin.services import customers as customer_service
from platform_admin.services import outbound_dispatch


def fake_dispatch(
    settings, *, tenant_id, conversation_id, message_row_id, sender_type, send
):
    try:
        wamid = send()
    except Exception as error:  # noqa: BLE001 - mirror the boundary's mapping
        code = (
            getattr(error, "error_code", None)
            or getattr(error, "provider_code", None)
            or type(error).__name__
        )
        message = (
            getattr(error, "error_message", None)
            or getattr(error, "provider_message", None)
            or str(error)
        )
        ambiguous = bool(getattr(error, "ambiguous", False))
        if message_row_id:
            try:
                customer_service.mark_message_failed(
                    settings,
                    tenant_id,
                    str(message_row_id),
                    error_code=str(code)[:40],
                    error_message=str(message)[:500],
                )
            except Exception:  # noqa: BLE001 - bookkeeping only; tests assert via mocks
                pass
        return outbound_dispatch.DispatchOutcome(
            "unknown" if ambiguous else "failed",
            error_code=str(code)[:40],
            error_message=str(message)[:500],
            error_type=type(error).__name__,
        )
    if message_row_id:
        try:
            customer_service.mark_message_sent(settings, tenant_id, str(message_row_id), wamid)
        except Exception:  # noqa: BLE001 - bookkeeping only; tests assert via mocks
            pass
    return outbound_dispatch.DispatchOutcome("sent", wamid=wamid)