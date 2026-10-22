"""Tenant-aware order operations for the platform admin dashboard.

Thin wrapper over saas.tenant_admin.TenantAdmin, following the same shape as
services/catalogue.py: validate operator input here, run tenant-scoped data
access through tenant_admin(), and record platform audit events. RLS confines
every statement to the one tenant; the LLM is never authoritative for money.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from saas.tenant_admin import tenant_admin

from ..config import Settings
from . import audit, validation as v


def _money(value: Any, field: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except Exception:
        raise v.ServiceError(f"{field} must be a number.") from None
    if not amount.is_finite() or amount < 0:
        raise v.ServiceError(f"{field} must not be negative.") from None
    return amount.quantize(Decimal("0.01"))


def create_order(
    settings: Settings,
    admin_id: str,
    tenant_id: str,
    form: dict[str, Any],
) -> str:
    customer_name = v.text(form.get("customer_name"), "Customer name", required=False)
    customer_phone = v.text(form.get("customer_phone"), "Customer phone", max_length=30, required=False)
    fulfilment_raw = (form.get("fulfilment") or "").strip() or None
    if fulfilment_raw not in (None, "pickup", "delivery"):
        raise v.ServiceError("Fulfilment must be 'pickup' or 'delivery'.")
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        order_id = ta.create_order(
            idempotency_key=(form.get("idempotency_key") or "").strip() or None,
            customer_name=customer_name,
            customer_phone=customer_phone,
            whatsapp_wa_id=(form.get("whatsapp_wa_id") or "").strip() or None,
            currency=v.currency(form.get("currency") or "KES"),
            fulfilment=fulfilment_raw,
            required_date=(form.get("required_date") or "").strip() or None,
            delivery_location=v.text(form.get("delivery_location"), "Delivery location",
                                     max_length=500, required=False),
            created_by_user_id=v.optional_uuid(form.get("created_by_user_id"), "Staff user"),
        )
        audit.record(settings, admin_id, tenant_id, "orders.order_created",
                     "order", order_id, {"order_id": order_id})
    return order_id


def get_order(settings: Settings, tenant_id: str, order_id: str) -> dict[str, Any]:
    order_id = v.uuid_value(order_id, "Order")
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        order = ta.get_order(order_id)
    if order is None:
        raise v.ServiceError("Order not found.", 404)
    return order


def quote_order(settings: Settings, tenant_id: str, order_id: str) -> dict[str, Any]:
    order_id = v.uuid_value(order_id, "Order")
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        try:
            return ta.quote_order(order_id)
        except ValueError as error:
            raise v.ServiceError(str(error), 404) from None


def add_order_item(
    settings: Settings,
    admin_id: str,
    tenant_id: str,
    order_id: str,
    form: dict[str, Any],
) -> int:
    order_id = v.uuid_value(order_id, "Order")
    item_type = (form.get("item_type") or "product").strip()
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        try:
            item_id = ta.add_order_item(
                order_id,
                item_type=item_type,
                product_id=v.optional_uuid(form.get("product_id"), "Product"),
                variant_id=v.optional_uuid(form.get("variant_id"), "Variant"),
                sku=v.text(form.get("sku"), "SKU", max_length=100, required=False),
                name=v.text(form.get("name"), "Item name", max_length=200),
                quantity=v.integer(form.get("quantity") or 1, "Quantity", minimum=1),
                unit_price=_money(form.get("unit_price") or 0, "Unit price"),
                attributes=v.json_object(form.get("attributes"), "Attributes"),
            )
        except ValueError as error:
            raise v.ServiceError(str(error)) from None
        audit.record(settings, admin_id, tenant_id, "orders.item_added",
                     "order", order_id, {"item_id": item_id})
    return item_id


def confirm_order(settings: Settings, admin_id: str, tenant_id: str, order_id: str) -> dict[str, Any]:
    order_id = v.uuid_value(order_id, "Order")
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        try:
            quote = ta.confirm_order(order_id)
        except ValueError as error:
            raise v.ServiceError(str(error)) from None
        audit.record(settings, admin_id, tenant_id, "orders.order_confirmed",
                     "order", order_id, {"order_number": quote["order_number"]})
    return quote


def cancel_order(
    settings: Settings, admin_id: str, tenant_id: str, order_id: str, form: dict[str, Any]
) -> None:
    order_id = v.uuid_value(order_id, "Order")
    reason = v.text(form.get("reason"), "Cancellation reason", max_length=500)
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        try:
            if not ta.cancel_order(order_id, reason):
                raise v.ServiceError("Order not found.", 404)
        except ValueError as error:
            raise v.ServiceError(str(error)) from None
        audit.record(settings, admin_id, tenant_id, "orders.order_cancelled",
                     "order", order_id, {"reason": reason})


def record_order_payment(
    settings: Settings,
    admin_id: str,
    tenant_id: str,
    order_id: str,
    form: dict[str, Any],
) -> int:
    order_id = v.uuid_value(order_id, "Order")
    kind = (form.get("kind") or "payment").strip()
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        try:
            payment_id = ta.record_order_payment(
                order_id,
                kind=kind,
                amount=_money(form.get("amount"), "Amount"),
                method=v.text(form.get("method"), "Method", max_length=60, required=False),
                reference=v.text(form.get("reference"), "Reference", max_length=200, required=False),
                idempotency_key=(form.get("idempotency_key") or "").strip() or None,
                recorded_by=v.optional_uuid(form.get("recorded_by"), "Recording staff user"),
                note=v.text(form.get("note"), "Note", max_length=1000, required=False),
            )
        except ValueError as error:
            raise v.ServiceError(str(error)) from None
        audit.record(settings, admin_id, tenant_id, "orders.payment_recorded",
                     "order_payment", str(payment_id), {"order_id": order_id, "kind": kind})
    return payment_id


def verify_order_payment(
    settings: Settings,
    admin_id: str,
    tenant_id: str,
    payment_id: int,
    form: dict[str, Any],
) -> None:
    """Staff-only verification of a recorded payment line."""
    status = (form.get("status") or "verified").strip()
    verified_by = v.uuid_value(form.get("verified_by"), "Verifying staff user")
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        try:
            if not ta.verify_order_payment(int(payment_id), verified_by=verified_by, status=status):
                raise v.ServiceError("Payment not found or already decided.", 404)
        except ValueError as error:
            raise v.ServiceError(str(error)) from None
        audit.record(settings, admin_id, tenant_id, "orders.payment_verified",
                     "order_payment", str(payment_id),
                     {"status": status, "verified_by": verified_by})


def order_payment_status(settings: Settings, tenant_id: str, order_id: str) -> dict[str, Any]:
    order_id = v.uuid_value(order_id, "Order")
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        try:
            return ta.get_order_payment_status(order_id)
        except ValueError as error:
            raise v.ServiceError(str(error), 404) from None




