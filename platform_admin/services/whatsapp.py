"""Verified WhatsApp webhook routing and the gated outbound text adapter."""

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
MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
CUSTOMER_KEY_RE = re.compile(r"^\+?[1-9][0-9]{6,30}$")


def verify_signature(raw_body: bytes, header: str | None, app_secret: str | None) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[7:], expected)


def parse_event(payload: Any) -> dict[str, str] | None:
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return None
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
                text = message.get("text") if isinstance(message, dict) else None
                message_id = message.get("id") if isinstance(message, dict) else None
                sender = message.get("from") if isinstance(message, dict) else None
                body = text.get("body") if isinstance(text, dict) else None
                if (
                    isinstance(message_id, str) and MESSAGE_ID_RE.fullmatch(message_id)
                    and isinstance(sender, str) and CUSTOMER_KEY_RE.fullmatch(sender)
                    and isinstance(body, str) and body.strip()
                ):
                    return {
                        "phone_number_id": phone_number_id,
                        "message_id": message_id,
                        "customer_key": sender,
                        "text": body[:4000],
                    }
    return None


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
) -> None:
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            update public.whatsapp_webhook_events
            set status = %s, error_code = %s, processed_at = now()
            where message_id = %s and tenant_id = app.current_tenant_id()
            """,
            (status, error_code, message_id),
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


def send_text(
    settings: Settings,
    *,
    phone_number_id: str,
    tenant_id: str,
    credentials_ref: str | None,
    recipient: str,
    text: str,
) -> str:
    if not settings.whatsapp_send_enabled:
        raise ServiceError("WhatsApp sending is disabled in this environment.", 503)
    token = access_token(settings, tenant_id, credentials_ref)
    payload = json.dumps(
        {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"body": text[:4096]},
        }
    ).encode()
    outbound = urllib.request.Request(
        f"https://graph.facebook.com/{settings.whatsapp_graph_api_version}/"
        f"{phone_number_id}/messages",
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(outbound, timeout=10) as response:  # noqa: S310
            body = json.loads(response.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as error:
        raise ServiceError("WhatsApp message delivery failed.", 502) from error
    messages = body.get("messages") if isinstance(body, dict) else None
    message_id = messages[0].get("id") if isinstance(messages, list) and messages else None
    if not isinstance(message_id, str):
        raise ServiceError("WhatsApp returned an invalid message response.", 502)
    return message_id
