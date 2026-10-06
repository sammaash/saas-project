"""Members and subscription for one tenant."""

from __future__ import annotations

from typing import Any

from saas.tenant_admin import tenant_admin

from ..config import Settings
from . import audit, validation as v

MEMBER_ROLES = ("owner", "staff")
MEMBER_STATUSES = ("invited", "active")
SUBSCRIPTION_STATUSES = ("trial", "active", "past_due", "suspended", "cancelled", "expired")


def list_members(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    with tenant_admin(tenant_id, settings.app_database_url) as ta:
        return ta.list_members()


def add_member(settings: Settings, admin_id: str, tenant_id: str, form: dict[str, Any]) -> None:
    # The Auth user id must be a verified Supabase UUID supplied by the operator.
    user_id = v.uuid_value(form.get("user_id"), "User id")
    role = form.get("role")
    status = form.get("status") or "active"
    if role not in MEMBER_ROLES:
        raise v.ServiceError("Role must be owner or staff.")
    if status not in MEMBER_STATUSES:
        raise v.ServiceError("Status must be invited or active.")
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        member_id = ta.add_member(user_id, role, status)
        audit.record(settings, admin_id, tenant_id, "member.added", "tenant_user", member_id,
                     {"user_id": user_id, "role": role, "status": status})


def get_subscription(settings: Settings, tenant_id: str) -> dict[str, Any]:
    with tenant_admin(tenant_id, settings.app_database_url) as ta:
        return {"subscription": ta.get_live_subscription(), "plans": ta.list_plans()}


def set_subscription(
    settings: Settings, admin_id: str, tenant_id: str, form: dict[str, Any]
) -> None:
    plan_id = v.uuid_value(form.get("plan_id"), "Plan")
    status = form.get("status")
    if status not in SUBSCRIPTION_STATUSES:
        raise v.ServiceError("Unsupported subscription status.")
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        ta.set_subscription(plan_id, status)
        audit.record(settings, admin_id, tenant_id, "subscription.updated", "subscription", None,
                     {"plan_id": plan_id, "status": status})
