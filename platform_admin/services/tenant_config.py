"""Members and subscription for one tenant."""

from __future__ import annotations

from typing import Any

from saas.tenant_admin import tenant_admin

from ..config import Settings
from . import audit, validation as v

SUBSCRIPTION_STATUSES = ("trial", "active", "past_due", "suspended", "cancelled", "expired")


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
