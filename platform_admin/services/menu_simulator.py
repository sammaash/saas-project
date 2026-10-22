"""Local-only adapter between the dashboard and the real WhatsApp menu service.

The simulator never calls the WhatsApp webhook and never sends a message. It builds a
``MenuContext`` with a simulation customer identity, calls the same
``platform_admin.services.menu`` functions as the live channel, and returns the actual
``MenuReply`` objects for server-side rendering.

The simulator is deliberately limited to loopback application databases. The normal
``/playground`` AI path is not changed by this module.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from platform_admin.config import Settings

from . import channels as channel_service
from . import customers as customer_service
from . import menu
from .validation import ServiceError

SIMULATOR_WA_PREFIX = "simulator-"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def require_local_database(settings: Settings) -> None:
    """Refuse the simulator when the application database is not loopback."""
    try:
        host = (urlsplit(settings.app_database_url or "").hostname or "").lower()
    except ValueError as error:
        raise ServiceError("The menu simulator cannot inspect the database URL.", 403) from error
    if host not in LOCAL_HOSTS:
        raise ServiceError(
            "The WhatsApp Menu Simulator is available only with a loopback local database.",
            403,
        )


def _simulator_settings(settings: Settings) -> Settings:
    """Outbound delivery is always disabled inside the simulator."""
    return replace(settings, whatsapp_send_enabled=False)


def _active_channel(settings: Settings, tenant_id: str) -> dict[str, Any] | None:
    try:
        for channel in channel_service.list_channels(settings, tenant_id):
            if (
                str(channel.get("status") or "") == "active"
                and str(channel.get("phone_number_id") or "").strip()
            ):
                return channel
    except Exception:  # noqa: BLE001 - a missing channel makes staff flow fail honestly
        return None
    return None


def _simulation_identity(
    settings: Settings, tenant_id: str, admin_id: str, channel: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return local-only customer/conversation rows for staff handover."""
    if channel is None:
        return None, None
    channel_id = str(channel.get("id") or "")
    if not channel_id:
        return None, None
    wa_id = f"{SIMULATOR_WA_PREFIX}{admin_id}"
    try:
        customer = customer_service.get_or_create_customer(
            settings, tenant_id, channel_id, wa_id, name="Menu Simulator"
        )
        conversation = customer_service.get_or_create_conversation(
            settings, tenant_id, str(customer["id"]), channel_id
        )
        return customer, conversation
    except Exception:  # noqa: BLE001 - staff flow must fail closed, not leak
        return None, None


def make_context(
    settings: Settings,
    tenant_id: str,
    admin_id: str,
    document: dict[str, Any],
    *,
    message_id: str,
    include_identity: bool = False,
) -> menu.MenuContext:
    """Build a real menu context using only local simulator identity."""
    channel = _active_channel(settings, tenant_id)
    customer = None
    conversation = None
    if include_identity:
        customer, conversation = _simulation_identity(
            settings, tenant_id, admin_id, channel
        )
    return menu.MenuContext(
        settings=_simulator_settings(settings),
        tenant_id=tenant_id,
        wa_id=f"{SIMULATOR_WA_PREFIX}{admin_id}",
        document=document,
        channel=channel,
        phone_number_id=(
            str(channel.get("phone_number_id")) if channel is not None else None
        ),
        customer=customer,
        conversation=conversation,
        message_id=message_id,
    )


def status(settings: Settings, tenant_id: str) -> tuple[bool, dict[str, Any]]:
    """Read the actual saved menu document and return ``(enabled, document)``."""
    require_local_database(settings)
    document = menu.load_document(settings, tenant_id)
    return menu.is_enabled(document), document


def reply_view(reply: menu.MenuReply | None) -> dict[str, Any] | None:
    """Serialize a real menu reply for the simulator session/template."""
    if reply is None:
        return None
    return {
        "kind": reply.kind,
        "text": reply.text,
        "header": reply.header,
        "body": reply.body,
        "footer": reply.footer,
        "button_text": reply.button_text,
        "buttons": [dict(button) for button in reply.buttons],
        "sections": [
            {
                "title": section.get("title"),
                "rows": [dict(row) for row in section.get("rows", [])],
            }
            for section in reply.sections
        ],
    }


def disabled_message(tenant_id: str) -> str:
    """The simulator's honest explanation of the real menu-disabled behavior."""
    return (
        "The WhatsApp customer menu is disabled for this tenant. The live webhook "
        "would fall through to the ordinary AI path; the simulator does not call the "
        "AI or send a WhatsApp message."
    )
