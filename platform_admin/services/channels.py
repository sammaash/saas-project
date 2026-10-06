"""Manual WhatsApp channel management for one tenant.

The platform never calls Meta. An administrator records non-secret identifiers (WABA id,
phone number id, display number) by hand. All writes go through the ``app.platform_*``
database functions, which enforce authorization, tenant scope, identifier rules, reserved
numbers and activation rules; this module only validates input and maps errors.
"""

from __future__ import annotations

import re
from typing import Any

from saas import platform
from saas.tenant_admin import tenant_admin

from ..config import Settings
from . import validation as v

PHONE_NUMBER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
WABA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
E164_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")


def list_channels(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    with tenant_admin(tenant_id, settings.app_database_url) as ta:
        return ta.list_channels()


def _identifiers(form: dict[str, Any]) -> tuple[str, str | None, str | None]:
    phone_number_id = v.text(form.get("phone_number_id"), "Phone number ID", max_length=64) or ""
    if not PHONE_NUMBER_ID_RE.match(phone_number_id):
        raise v.ServiceError("Phone number ID may contain letters, digits, hyphens and underscores.")
    waba_id = v.text(form.get("waba_id"), "WABA ID", max_length=64, required=False)
    if waba_id and not WABA_ID_RE.match(waba_id):
        raise v.ServiceError("WABA ID may contain letters, digits, hyphens and underscores.")
    display = v.text(form.get("display_phone_number"), "WhatsApp business number", max_length=20, required=False)
    if display and not E164_RE.match(display):
        raise v.ServiceError("WhatsApp business number must be in E.164 format, e.g. +254700000000.")
    return phone_number_id, waba_id, display


def connect_channel(
    settings: Settings, admin_id: str, tenant: dict[str, Any], form: dict[str, Any]
) -> str:
    """Record a pending channel for ``tenant``; the tenant's slug must be typed to confirm."""
    if (form.get("confirm_slug") or "").strip() != tenant["slug"]:
        raise v.ServiceError("Type the tenant's slug exactly to confirm this tenant.")
    phone_number_id, waba_id, display = _identifiers(form)
    with v.translated():
        return platform.create_channel(
            admin_id, tenant["id"], phone_number_id, waba_id, display,
            url=settings.platform_database_url,
        )


def update_channel(
    settings: Settings, admin_id: str, tenant_id: str, channel_id: str, form: dict[str, Any]
) -> None:
    channel_id = v.uuid_value(channel_id, "Channel")
    phone_number_id, waba_id, display = _identifiers(form)
    _require_channel_of_tenant(settings, tenant_id, channel_id)
    with v.translated():
        platform.update_channel(
            admin_id, channel_id, phone_number_id, waba_id, display,
            url=settings.platform_database_url,
        )


def set_channel_status(
    settings: Settings, admin_id: str, tenant_id: str, channel_id: str, status: str
) -> None:
    channel_id = v.uuid_value(channel_id, "Channel")
    if status not in ("pending", "active", "disabled"):
        raise v.ServiceError("Unsupported channel status.")
    if status == "active" and not settings.allow_active_channels:
        raise v.ServiceError("Activating channels is disabled in this environment.", 403)
    _require_channel_of_tenant(settings, tenant_id, channel_id)
    with v.translated():
        platform.set_channel_status(
            admin_id, channel_id, status, url=settings.platform_database_url
        )


def _require_channel_of_tenant(settings: Settings, tenant_id: str, channel_id: str) -> None:
    """The route names a tenant; refuse a channel that belongs to another one."""
    if not any(str(c["id"]) == channel_id for c in list_channels(settings, tenant_id)):
        raise v.ServiceError("Channel not found.", 404)
