"""Verified WhatsApp webhook routing and the gated outbound text adapter.

Inbound parsing is deliberately strict and typed: an event carries a ``kind`` so the
caller can decide what to do with it instead of assuming free text. Delivery-status
webhooks are parsed separately because they describe messages we sent, not messages a
customer sent, and they are not deduplicated by the webhook-claim table.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from saas.db import connect
from saas.tenant_context import tenant_transaction

from ..config import Settings
from .validation import ServiceError

PHONE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
# Meta's message ids look like "wamid.HBgMMjU0NzIzNTk1MzM0FQIAERgSRjk2NDBCNUIwQzFGQkI5RkQyAA==".
# The payload is base64, so it can contain "+", "/" and "=" padding. An earlier pattern
# excluded those characters, which made every real inbound message fail validation and be
# silently ignored (200 {"ignored": true}, no rows written) while the tests passed because
# their fixtures used a simplified id.
MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.:+/=-]{1,200}$")
CUSTOMER_KEY_RE = re.compile(r"^\+?[1-9][0-9]{6,30}$")

#: Meta documents 256 characters for an interactive reply id. Anything longer did not come
#: from our own renderer, so it is dropped rather than trusted.
ACTION_ID_MAX = 256
#: Provider error text is stored for diagnosis, never shown to a customer.
PROVIDER_ERROR_TEXT_MAX = 500
#: The four statuses the messages table accepts (0033). A status outside this set is
#: ignored instead of writing a value the CHECK constraint would reject.
DELIVERY_STATUSES = frozenset({"sent", "delivered", "read", "failed"})


class WhatsAppSendError(ServiceError):
    """A send that failed, carrying Meta's diagnostic fields.

    ``message`` stays exactly what it was before this class existed, so the response
    body Meta receives does not change. The extra attributes exist only so the webhook
    can record a structured error against the outbound row.
    """

    def __init__(
        self,
        message: str,
        status: int = 502,
        *,
        provider_code: str | None = None,
        provider_message: str | None = None,
        http_status: int | None = None,
        ambiguous: bool = False,
    ):
        super().__init__(message, status)
        self.provider_code = provider_code
        self.provider_message = provider_message
        #: HTTP status when Meta answered at all. None for transport failures.
        self.http_status = http_status
        #: True when the message may have been accepted even though this call failed.
        #: Callers must never retry automatically in that case.
        self.ambiguous = ambiguous


def verify_signature(raw_body: bytes, header: str | None, app_secret: str | None) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[7:], expected)


def _valid_action_id(value: Any) -> str | None:
    """A usable interactive reply id, or None.

    The id is opaque to us (it is echoed back verbatim), so the only checks are the
    ones that keep it safe to store and match: a real string, bounded length, no NUL.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > ACTION_ID_MAX or "\x00" in cleaned:
        return None
    return cleaned


def _first_provider_error(errors: Any) -> tuple[str | None, str | None]:
    """Meta's first error entry as (code, text). Never raises."""
    if not isinstance(errors, list) or not errors:
        return None, None
    first = errors[0]
    if not isinstance(first, dict):
        return None, None
    code = first.get("code")
    code_text = None if code is None else str(code).strip()[:40] or None
    message = first.get("message") or first.get("title") or first.get("error_data")
    if isinstance(message, dict):
        message = message.get("details")
    if not isinstance(message, str) or not message.strip():
        return code_text, None
    return code_text, message.strip()[:PROVIDER_ERROR_TEXT_MAX].replace("\x00", "")



def parse_event(payload: Any) -> dict[str, str] | None:
    """The first actionable inbound message, typed, or None.

    Only two kinds are produced:
      * ``text``        -- ``text`` carries the (truncated) body.
      * ``interactive`` -- ``action_type``/``action_id``/``action_title`` describe the tap.

    Text wins over a tap when both appear in one payload: a batch is delivered in order,
    but dropping a typed message because a button event arrived first would lose a real
    customer request. A tap is therefore remembered and returned only if the payload
    contains no text at all.

    Anything else (media, reactions, unsupported interactive types) still returns None,
    which the route acknowledges and ignores exactly as before. A customer-visible
    fallback for unsupported messages is a separate, unapproved change.
    """
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return None
    interactive_event: dict[str, str] | None = None
    for entry in payload.get("entry", []):
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []):
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            messages = value.get("messages")
            if not isinstance(metadata, dict) or not isinstance(messages, list):
                continue
            phone_number_id = metadata.get("phone_number_id")
            if not isinstance(phone_number_id, str) or not PHONE_ID_RE.fullmatch(phone_number_id):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                sender = message.get("from")
                if not (
                    isinstance(message_id, str) and MESSAGE_ID_RE.fullmatch(message_id)
                    and isinstance(sender, str) and CUSTOMER_KEY_RE.fullmatch(sender)
                ):
                    continue
                base = {
                    "phone_number_id": phone_number_id,
                    "message_id": message_id,
                    "customer_key": sender,
                }
                text = message.get("text")
                body = text.get("body") if isinstance(text, dict) else None
                if isinstance(body, str) and body.strip():
                    return {**base, "kind": "text", "text": body[:4000]}
                interactive = message.get("interactive")
                if isinstance(interactive, dict):
                    action_type = interactive.get("type")
                    if action_type in ("button_reply", "list_reply"):
                        reply = interactive.get(action_type)
                        action_id = _valid_action_id(
                            reply.get("id") if isinstance(reply, dict) else None
                        )
                        if action_id:
                            title = reply.get("title") if isinstance(reply, dict) else None
                            # Remember the first tap but keep scanning: a text message
                            # later in the same batch must not be lost.
                            if interactive_event is None:
                                interactive_event = {
                                    **base,
                                    "kind": "interactive",
                                    "action_type": str(action_type),
                                    "action_id": action_id,
                                    "action_title": (
                                        title[:200] if isinstance(title, str) else ""
                                    ),
                                }
    return interactive_event


def parse_status_events(payload: Any) -> list[dict[str, str]]:
    """Every delivery-status entry in a webhook, in order. Never raises.

    Status webhooks describe messages *we* sent. They are not deduplicated by
    ``whatsapp_webhook_events`` (there is no customer message id to claim), so applying
    one must be idempotent -- which is why the update is guarded by a transition rule
    rather than by the claim table.
    """
    events: list[dict[str, str]] = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return events
    for entry in payload.get("entry", []):
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []):
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            statuses = value.get("statuses")
            if not isinstance(metadata, dict) or not isinstance(statuses, list):
                continue
            phone_number_id = metadata.get("phone_number_id")
            if not isinstance(phone_number_id, str) or not PHONE_ID_RE.fullmatch(phone_number_id):
                continue
            for item in statuses:
                if not isinstance(item, dict):
                    continue
                message_id = item.get("id")
                status = item.get("status")
                if not (
                    isinstance(message_id, str) and MESSAGE_ID_RE.fullmatch(message_id)
                    and isinstance(status, str) and status in DELIVERY_STATUSES
                ):
                    continue
                error_code, error_message = _first_provider_error(item.get("errors"))
                callback = item.get("biz_opaque_callback_data")
                events.append(
                    {
                        "phone_number_id": phone_number_id,
                        "provider_message_id": message_id,
                        "status": status,
                        "error_code": error_code or "",
                        # Correlates a status with the outbound row even when the send
                        # response (and therefore the wamid) was never recorded.
                        "biz_opaque_callback_data": (
                            callback[:200]
                            if isinstance(callback, str) and len(callback) <= 200
                            else ""
                        ),
                        "error_message": error_message or "",
                    }
                )
    return events



def resolve_channel(settings: Settings, phone_number_id: str) -> dict[str, Any] | None:
    with connect(settings.app_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select * from app.resolve_whatsapp_channel(%s)", (phone_number_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(zip((column.name for column in cursor.description), row, strict=True))


def claim_event(
    settings: Settings, tenant_id: str, channel_id: str, message_id: str, customer_key: str
) -> bool:
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            insert into public.whatsapp_webhook_events
                (tenant_id, channel_id, message_id, customer_key)
            values (app.current_tenant_id(), %s, %s, %s)
            on conflict (message_id) do nothing
            """,
            (channel_id, message_id, customer_key),
        )
        return cursor.rowcount == 1


def update_event(
    settings: Settings,
    tenant_id: str,
    message_id: str,
    status: str,
    error_code: str | None = None,
    response_message_id: str | None = None,
) -> None:
    """Record the outcome of one claimed inbound message.

    ``response_message_id`` is the wamid of the reply we sent for it. It is written with
    ``coalesce`` so a later failure update cannot erase a wamid that was already recorded;
    that value is how a redelivery can tell "already replied" from "never replied".
    """
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            update public.whatsapp_webhook_events
            set status = %s,
                error_code = %s,
                response_message_id = coalesce(%s, response_message_id),
                processed_at = now()
            where message_id = %s and tenant_id = app.current_tenant_id()
            """,
            (status, error_code, response_message_id, message_id),
        )


def conversation_history(
    settings: Settings, tenant_id: str, customer_key: str
) -> list[dict[str, str]]:
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            select history from public.whatsapp_conversations
            where tenant_id = app.current_tenant_id() and customer_key = %s
            """,
            (customer_key,),
        )
        row = cursor.fetchone()
        return row[0] if row and isinstance(row[0], list) else []


def save_conversation(
    settings: Settings, tenant_id: str, customer_key: str, history: list[dict[str, str]]
) -> None:
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            insert into public.whatsapp_conversations
                (tenant_id, customer_key, history)
            values (app.current_tenant_id(), %s, %s)
            on conflict (tenant_id, customer_key) do update
            set history = excluded.history, updated_at = now()
            """,
            (customer_key, Jsonb(history[-8:])),
        )


def access_token(settings: Settings, tenant_id: str, credentials_ref: str | None) -> str:
    expected_ref = f"tenants/{tenant_id}/whatsapp"
    if credentials_ref != expected_ref:
        raise ServiceError("WhatsApp credentials are not configured for this tenant.", 503)
    token = os.environ.get(f"WHATSAPP_ACCESS_TOKEN_{tenant_id.replace('-', '_').upper()}", "")
    if not token:
        raise ServiceError("WhatsApp credentials are not configured for this tenant.", 503)
    return token


def _http_error_details(error: urllib.error.HTTPError) -> tuple[str | None, str | None]:
    """Meta's error code and text from an HTTPError body. Never raises, never logs secrets."""
    try:
        raw = error.read()
        parsed = json.loads(raw.decode() or "{}")
    except (ValueError, OSError, UnicodeDecodeError):
        return None, None
    payload = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(payload, dict):
        return None, None
    code = payload.get("code")
    code_text = None if code is None else str(code).strip()[:40] or None
    message = payload.get("message") or payload.get("title")
    if not isinstance(message, str) or not message.strip():
        return code_text, None
    return code_text, message.strip()[:PROVIDER_ERROR_TEXT_MAX].replace("\x00", "")


def _interactive_header(header: str | None, footer: str | None) -> dict[str, Any]:
    envelope: dict[str, Any] = {}
    if header:
        envelope["header"] = {"type": "text", "text": header[:60]}
    if footer:
        envelope["footer"] = {"text": footer[:60]}
    return envelope


def build_button_payload(
    *,
    recipient: str,
    body: str,
    buttons: list[dict[str, str]],
    header: str | None = None,
    footer: str | None = None,
    reply_to_row_id: str | None = None,
) -> dict[str, Any]:
    """A native WhatsApp reply-button message with at most three buttons.

    Meta's limits are enforced here: three buttons, a 20-character title, a 1024-character
    body and a 256-character id. Titles are truncated (display text); ids are never
    truncated, because a shortened id would route to the wrong action -- an oversized id
    is a programming error and raises instead.
    """
    if not 1 <= len(buttons) <= 3:
        raise ValueError("WhatsApp reply-button messages accept between 1 and 3 buttons.")
    actions: list[dict[str, Any]] = []
    for button in buttons:
        action_id = str(button.get("id") or "")
        title = str(button.get("title") or "")
        if not action_id or len(action_id) > ACTION_ID_MAX:
            raise ValueError("A reply-button id must be between 1 and 256 characters.")
        if not title.strip():
            raise ValueError("A reply-button title is required.")
        actions.append(
            {"type": "reply", "reply": {"id": action_id, "title": title[:20]}}
        )
    interactive: dict[str, Any] = {
        "type": "button",
        "body": {"text": body[:1024]},
        "action": {"buttons": actions},
    }
    interactive.update(_interactive_header(header, footer))
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "interactive",
        "interactive": interactive,
    }
    if reply_to_row_id:
        payload["biz_opaque_callback_data"] = reply_to_row_id
    return payload


def build_list_payload(
    *,
    recipient: str,
    body: str,
    button_text: str,
    sections: list[dict[str, Any]],
    header: str | None = None,
    footer: str | None = None,
    reply_to_row_id: str | None = None,
) -> dict[str, Any]:
    """A native WhatsApp list message: at most 10 rows across at most 10 sections.

    Row titles are 24 characters, descriptions 72, the action button 20. Row ids are
    routing keys and are never truncated; an oversized id raises.
    """
    if not sections:
        raise ValueError("A list message needs at least one section.")
    if len(sections) > 10:
        raise ValueError("WhatsApp list messages accept at most 10 sections.")
    cleaned: list[dict[str, Any]] = []
    total = 0
    for section in sections:
        rows: list[dict[str, Any]] = []
        for row in section.get("rows") or []:
            action_id = str(row.get("id") or "")
            title = str(row.get("title") or "")
            if not action_id or len(action_id) > ACTION_ID_MAX:
                raise ValueError("A list-row id must be between 1 and 256 characters.")
            if not title.strip():
                raise ValueError("A list-row title is required.")
            item: dict[str, Any] = {"id": action_id, "title": title[:24]}
            description = str(row.get("description") or "").strip()
            if description:
                item["description"] = description[:72]
            rows.append(item)
        total += len(rows)
        if rows:
            cleaned.append({"title": str(section.get("title") or "")[:24], "rows": rows})
    if not 1 <= total <= 10:
        raise ValueError("WhatsApp list messages accept between 1 and 10 rows in total.")
    interactive: dict[str, Any] = {
        "type": "list",
        "body": {"text": body[:1024]},
        "action": {"button": button_text[:20], "sections": cleaned},
    }
    interactive.update(_interactive_header(header, footer))
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "interactive",
        "interactive": interactive,
    }
    if reply_to_row_id:
        payload["biz_opaque_callback_data"] = reply_to_row_id
    return payload


def _post_message(
    settings: Settings,
    *,
    phone_number_id: str,
    tenant_id: str,
    credentials_ref: str | None,
    payload: dict[str, Any],
) -> str:
    """POST one message payload to Meta and return the wamid.

    All outbound shapes (text, reply buttons, list) share this path so the gate, the
    credential lookup and the provider-error translation cannot drift apart.
    """
    if not settings.whatsapp_send_enabled:
        raise ServiceError("WhatsApp sending is disabled in this environment.", 503)
    token = access_token(settings, tenant_id, credentials_ref)
    outbound = urllib.request.Request(
        f"https://graph.facebook.com/{settings.whatsapp_graph_api_version}/"
        f"{phone_number_id}/messages",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(outbound, timeout=10) as response:  # noqa: S310
            response_body = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        code, provider_message = _http_error_details(error)
        raise WhatsAppSendError(
            "WhatsApp message delivery failed.",
            502,
            provider_code=code,
            provider_message=provider_message,
            http_status=error.code,
            # A 4xx is a definite rejection. A 5xx or 408 may have been accepted before
            # the failure, so it is ambiguous and must not be retried automatically.
            ambiguous=error.code >= 500 or error.code == 408,
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise WhatsAppSendError(
            "WhatsApp message delivery failed.",
            502,
            provider_message=type(error).__name__,
            ambiguous=True,
        ) from error
    except OSError as error:
        # Connection reset / broken pipe after the request may have reached Meta.
        raise WhatsAppSendError(
            "WhatsApp message delivery failed.",
            502,
            provider_message=type(error).__name__,
            ambiguous=True,
        ) from error
    except ValueError as error:
        raise WhatsAppSendError(
            "WhatsApp message delivery failed.", 502, ambiguous=True
        ) from error
    messages = response_body.get("messages") if isinstance(response_body, dict) else None
    message_id = messages[0].get("id") if isinstance(messages, list) and messages else None
    if not isinstance(message_id, str):
        raise WhatsAppSendError(
            "WhatsApp returned an invalid message response.", 502, ambiguous=True
        )
    return message_id


def send_text(
    settings: Settings,
    *,
    phone_number_id: str,
    tenant_id: str,
    credentials_ref: str | None,
    recipient: str,
    text: str,
    reply_to_row_id: str | None = None,
) -> str:
    """Send one free-form text message and return its wamid.

    ``reply_to_row_id`` is omitted by every existing caller, so the request body for a
    plain text reply is byte-for-byte what it was before this parameter existed. When it
    is supplied, Meta echoes it back on the status webhook, which is how an outbound row
    can be reconciled even if this call's response was lost.

    It is deliberately unused today. Meta's status-webhook reference documents the field
    as present only when the message was sent with it, but the request-side schema could
    not be confirmed for this Graph version, and a real send has not been made. See
    docs/OUTBOUND_DELIVERY.md before wiring it up.
    """
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {"body": text[:4096]},
    }
    if reply_to_row_id:
        payload["biz_opaque_callback_data"] = reply_to_row_id
    return _post_message(
        settings,
        phone_number_id=phone_number_id,
        tenant_id=tenant_id,
        credentials_ref=credentials_ref,
        payload=payload,
    )


def send_buttons(
    settings: Settings,
    *,
    phone_number_id: str,
    tenant_id: str,
    credentials_ref: str | None,
    recipient: str,
    body: str,
    buttons: list[dict[str, str]],
    header: str | None = None,
    footer: str | None = None,
) -> str:
    """Send a native reply-button message (max 3 buttons) and return its wamid."""
    return _post_message(
        settings,
        phone_number_id=phone_number_id,
        tenant_id=tenant_id,
        credentials_ref=credentials_ref,
        payload=build_button_payload(
            recipient=recipient,
            body=body,
            buttons=buttons,
            header=header,
            footer=footer,
        ),
    )


def send_list(
    settings: Settings,
    *,
    phone_number_id: str,
    tenant_id: str,
    credentials_ref: str | None,
    recipient: str,
    body: str,
    button_text: str,
    sections: list[dict[str, Any]],
    header: str | None = None,
    footer: str | None = None,
) -> str:
    """Send a native list message (max 10 rows) and return its wamid."""
    return _post_message(
        settings,
        phone_number_id=phone_number_id,
        tenant_id=tenant_id,
        credentials_ref=credentials_ref,
        payload=build_list_payload(
            recipient=recipient,
            body=body,
            button_text=button_text,
            sections=sections,
            header=header,
            footer=footer,
        ),
    )
