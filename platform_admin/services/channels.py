"""WhatsApp / Meta channel management for one tenant.

Channels are created ``pending`` and only ever hold a ``credentials_ref`` pointer. Every
Meta operation names the tenant explicitly and is audited. Raw tokens never enter this
module: secret resolution happens inside the provider.
"""

from __future__ import annotations

import re
from typing import Any

from saas.tenant_admin import tenant_admin

from ..config import Settings
from ..meta.provider import (
    MetaError,
    MetaNotConfigured,
    MetaProvider,
    TenantMetaContext,
)
from ..meta.secrets import CredentialScopeError, SecretNotConfigured, tenant_credentials_ref
from . import audit, validation as v

PHONE_NUMBER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
E164_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")
META_OPERATIONS = {
    "list_businesses": (),
    "list_wabas": ("business_id",),
    "list_phone_numbers": ("waba_id",),
    "get_waba": ("waba_id",),
    "get_phone_number": ("phone_number_id",),
    "register_number": ("waba_id", "display_phone_number"),
    "check_webhook": ("waba_id",),
}


def list_channels(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    with tenant_admin(tenant_id, settings.app_database_url) as ta:
        return ta.list_channels()


def connect_channel(
    settings: Settings, admin_id: str, tenant: dict[str, Any], form: dict[str, Any]
) -> str:
    """Record a channel for ``tenant`` as pending. Requires typing the tenant's slug."""
    if (form.get("confirm_slug") or "").strip() != tenant["slug"]:
        raise v.ServiceError("Type the tenant's slug exactly to confirm this tenant.")
    phone_number_id = v.text(form.get("phone_number_id"), "Phone number ID", max_length=64)
    if not PHONE_NUMBER_ID_RE.match(phone_number_id):
        raise v.ServiceError("Phone number ID may contain letters, digits, hyphens and underscores.")
    if phone_number_id in settings.blocked_phone_number_ids:
        raise v.ServiceError("This phone number is blocked from being connected in this environment.", 403)
    waba_id = v.text(form.get("waba_id"), "WABA ID", max_length=64, required=False)
    if waba_id and not EXTERNAL_ID_RE.match(waba_id):
        raise v.ServiceError("WABA ID may contain letters, digits, hyphens and underscores.")
    display = v.text(form.get("display_phone_number"), "Display phone number", max_length=20, required=False)
    if display and not E164_RE.match(display):
        raise v.ServiceError("Display phone number must be in E.164 format, e.g. +254700000000.")

    tenant_id = tenant["id"]
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        is_default = not ta.list_channels()
        channel_id = ta.create_channel(
            phone_number_id, waba_id, display, tenant_credentials_ref(tenant_id), is_default
        )
        audit.record(ta, admin_id, "channel.connect", "tenant_channel", channel_id, {
            "phone_number_id": phone_number_id, "waba_id": waba_id,
            "credentials_ref": tenant_credentials_ref(tenant_id), "status": "pending",
        })
    return channel_id


def set_channel_status(
    settings: Settings, admin_id: str, tenant_id: str, channel_id: str, status: str
) -> None:
    channel_id = v.uuid_value(channel_id, "Channel")
    if status not in ("pending", "active", "disabled"):
        raise v.ServiceError("Unsupported channel status.")
    if status == "active" and not settings.allow_active_channels:
        raise v.ServiceError("Activating channels is disabled in this environment.", 403)
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        if not ta.set_channel_status(channel_id, status):
            raise v.ServiceError("Channel not found.", 404)
        audit.record(ta, admin_id, "channel.status_changed", "tenant_channel", channel_id,
                     {"status": status})


def run_meta_operation(
    settings: Settings,
    provider: MetaProvider,
    admin_id: str,
    tenant_id: str,
    operation: str,
    **arguments: str,
) -> Any:
    """Run one provider operation for ``tenant_id`` and audit it."""
    if operation not in META_OPERATIONS:
        raise v.ServiceError("Unsupported Meta operation.")
    arguments = {name: arguments.get(name, "") for name in META_OPERATIONS[operation]}
    for name, value in arguments.items():
        pattern = E164_RE if name == "display_phone_number" else EXTERNAL_ID_RE
        if not value or not pattern.match(value):
            raise v.ServiceError(f"Invalid value for {name}.")
    context = TenantMetaContext(tenant_id, tenant_credentials_ref(tenant_id))
    try:
        result = getattr(provider, operation)(context, **arguments)
        outcome = "ok"
    except MetaNotConfigured:
        raise v.ServiceError("Meta integration is not configured in this environment.", 503) from None
    except SecretNotConfigured:
        raise v.ServiceError("No credentials are configured for this tenant's Meta access.", 503) from None
    except CredentialScopeError:
        raise v.ServiceError("Credential reference does not belong to this tenant.", 403) from None
    except MetaError as error:
        outcome = "failed"
        result = None
        failure = str(error)
    with tenant_admin(tenant_id, settings.app_database_url) as ta:
        audit.record(ta, admin_id, f"channel.meta_{operation}", "tenant_channel", None,
                     {"operation": operation, "outcome": outcome, **arguments})
    if outcome == "failed":
        raise v.ServiceError(f"Meta operation failed: {failure}", 502)
    return result
