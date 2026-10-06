"""Tenant lifecycle: create (draft), profile, rules/AI configuration, readiness, activation."""

from __future__ import annotations

from typing import Any

from saas import platform
from saas.tenant_admin import tenant_admin

from ..config import Settings
from . import audit, validation as v

RULE_DOCUMENTS = (
    ("business_rules", "Business rules"),
    ("bot_persona", "Bot persona"),
    ("business_hours", "Business hours"),
    ("configuration", "Configuration"),
)


def create_tenant(settings: Settings, admin_id: str, form: dict[str, Any]) -> str:
    name = v.text(form.get("name"), "Business name")
    slug = v.slug(form.get("slug"))
    timezone = v.timezone(form.get("timezone") or "Africa/Nairobi")
    currency = v.currency(form.get("currency") or "KES")
    owner = v.optional_uuid(form.get("owner_user_id"), "Owner user id")
    plan_id = v.optional_uuid(form.get("plan_id"), "Plan")
    with v.translated():
        return platform.provision_tenant(
            admin_id, name, slug, timezone, currency, owner, plan_id,
            url=settings.platform_database_url,
        )


def list_tenants(settings: Settings, admin_id: str, status: str | None = None) -> list[dict[str, Any]]:
    with v.translated():
        return platform.list_tenants(admin_id, status, url=settings.platform_database_url)


def get_tenant(settings: Settings, admin_id: str, tenant_id: str) -> dict[str, Any]:
    tenant_id = v.uuid_value(tenant_id, "Tenant")
    with v.translated():
        tenant = platform.get_tenant(admin_id, tenant_id, url=settings.platform_database_url)
    if tenant is None:
        raise v.ServiceError("Tenant not found.", 404)
    tenant["id"] = str(tenant["id"])
    return tenant


def list_plans(settings: Settings, admin_id: str) -> list[dict[str, Any]]:
    with v.translated():
        return platform.list_plans(admin_id, url=settings.platform_database_url)


def overview(settings: Settings, admin_id: str) -> dict[str, Any]:
    with v.translated():
        data = platform.overview(admin_id, url=settings.platform_database_url)
        data["recent_audit"] = platform.recent_audit(
            admin_id, None, 15, url=settings.platform_database_url
        )
    return data


def readiness(settings: Settings, admin_id: str, tenant_id: str) -> dict[str, Any]:
    with v.translated():
        return platform.tenant_readiness(admin_id, tenant_id, url=settings.platform_database_url)


def change_status(
    settings: Settings, admin_id: str, tenant_id: str, status: str, waive_owner: bool = False
) -> str:
    with v.translated():
        return platform.set_tenant_status(
            admin_id, tenant_id, status, waive_owner, url=settings.platform_database_url
        )


def get_profile(settings: Settings, tenant_id: str) -> dict[str, Any]:
    with tenant_admin(tenant_id, settings.app_database_url) as ta:
        tenant = ta.get_tenant()
        config = ta.get_settings()
    if tenant is None or config is None:
        raise v.ServiceError("Tenant not found.", 404)
    return {"tenant": tenant, "settings": config}


def update_profile(settings: Settings, admin_id: str, tenant_id: str, form: dict[str, Any]) -> None:
    name = v.text(form.get("name"), "Business name")
    timezone = v.timezone(form.get("timezone"))
    currency = v.currency(form.get("currency"))
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        ta.rename(name)
        ta.update_settings(timezone=timezone, currency=currency)
        audit.record(
            settings, admin_id, tenant_id, "tenant.profile_updated", "tenant", tenant_id,
            {"name": name, "timezone": timezone, "currency": currency},
        )


def update_rules(settings: Settings, admin_id: str, tenant_id: str, form: dict[str, Any]) -> None:
    documents = {
        column: v.json_object(form.get(column), label) for column, label in RULE_DOCUMENTS
    }
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        ta.update_settings(**documents)
        # Section names only: values may be large or sensitive and do not belong in audit.
        audit.record(
            settings, admin_id, tenant_id, "tenant.rules_updated", "tenant_settings", tenant_id,
            {"sections": sorted(documents)},
        )


def tenant_audit(settings: Settings, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with tenant_admin(tenant_id, settings.app_database_url) as ta:
        return ta.list_audit(limit)
