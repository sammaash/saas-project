"""Catalogue management for one tenant (products and variants, prices, activation)."""

from __future__ import annotations

from typing import Any

from saas.tenant_admin import tenant_admin

from ..config import Settings
from . import audit, validation as v


def list_catalogue(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    with tenant_admin(tenant_id, settings.app_database_url) as ta:
        return ta.list_catalogue()


def _product_values(form: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": v.text(form.get("name"), "Product name"),
        "description": v.text(form.get("description"), "Description", max_length=2000, required=False),
        "category": v.text(form.get("category"), "Category", max_length=100, required=False),
        "sort_order": v.integer(form.get("sort_order"), "Sort order"),
        "attributes": v.json_object(form.get("attributes"), "Attributes"),
    }


def _variant_values(form: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": v.text(form.get("code"), "Variant code", max_length=100),
        "sku": v.text(form.get("sku") or form.get("code"), "SKU", max_length=100),
        "name": v.text(form.get("name"), "Variant name"),
        "price": v.price(form.get("price")),
        "attributes": v.json_object(form.get("attributes"), "Attributes"),
        "sort_order": v.integer(form.get("sort_order"), "Sort order"),
    }


def _optional_nonnegative_integer(value: Any, label: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    result = v.integer(value, label)
    if result < 0:
        raise v.ServiceError(f"{label} must not be negative.")
    return result


def set_variant_availability(
    settings: Settings,
    admin_id: str,
    tenant_id: str,
    variant_id: str,
    form: dict[str, Any],
) -> None:
    variant_id = v.uuid_value(variant_id, "Variant")
    availability = str(form.get("availability") or "").strip().upper()
    if availability not in {"IN_STOCK", "OUT_OF_STOCK", "MADE_TO_ORDER"}:
        raise v.ServiceError("Availability must be IN_STOCK, OUT_OF_STOCK or MADE_TO_ORDER.")
    stock_quantity = _optional_nonnegative_integer(form.get("stock_quantity"), "Stock quantity")
    reorder_level = _optional_nonnegative_integer(form.get("reorder_level"), "Reorder level")
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        if not ta.update_variant(
            variant_id,
            availability=availability,
            stock_quantity=stock_quantity,
            reorder_level=reorder_level,
        ):
            raise v.ServiceError("Variant not found.", 404)
        audit.record(
            settings,
            admin_id,
            tenant_id,
            "catalogue.variant_availability_updated",
            "product_variant",
            variant_id,
            {
                "availability": availability,
                "stock_quantity": stock_quantity,
                "reorder_level": reorder_level,
            },
        )


def create_product(settings: Settings, admin_id: str, tenant_id: str, form: dict[str, Any]) -> str:
    values = _product_values(form)
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        product_id = ta.create_product(**values)
        audit.record(settings, admin_id, tenant_id, "catalogue.product_created", "product", product_id,
                     {"name": values["name"]})
    return product_id


def update_product(
    settings: Settings, admin_id: str, tenant_id: str, product_id: str, form: dict[str, Any]
) -> None:
    product_id = v.uuid_value(product_id, "Product")
    values = _product_values(form)
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        if not ta.update_product(product_id, **values):
            raise v.ServiceError("Product not found.", 404)
        audit.record(settings, admin_id, tenant_id, "catalogue.product_updated", "product", product_id,
                     {"name": values["name"]})


def set_product_active(
    settings: Settings, admin_id: str, tenant_id: str, product_id: str, active: bool
) -> None:
    product_id = v.uuid_value(product_id, "Product")
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        if not ta.update_product(product_id, is_active=active):
            raise v.ServiceError("Product not found.", 404)
        audit.record(settings, admin_id, tenant_id, "catalogue.product_activated" if active
                     else "catalogue.product_deactivated", "product", product_id)


def create_variant(
    settings: Settings, admin_id: str, tenant_id: str, product_id: str, form: dict[str, Any]
) -> str:
    product_id = v.uuid_value(product_id, "Product")
    values = _variant_values(form)
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        if ta.get_product(product_id) is None:
            raise v.ServiceError("Product not found.", 404)
        variant_id = ta.create_variant(product_id, **values)
        audit.record(settings, admin_id, tenant_id, "catalogue.variant_created", "product_variant", variant_id,
                     {"product_id": product_id, "code": values["code"], "price": str(values["price"])})
    return variant_id


def update_variant(
    settings: Settings, admin_id: str, tenant_id: str, variant_id: str, form: dict[str, Any]
) -> None:
    variant_id = v.uuid_value(variant_id, "Variant")
    values = _variant_values(form)
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        previous = ta.get_variant(variant_id)
        if previous is None:
            raise v.ServiceError("Variant not found.", 404)
        if not form.get("sku"):
            values["sku"] = previous["sku"]
        if not ta.update_variant(variant_id, **values):
            raise v.ServiceError("Variant not found.", 404)
        audit.record(settings, admin_id, tenant_id, "catalogue.variant_updated", "product_variant", variant_id,
                     {"code": values["code"], "old_price": str(previous["price"]),
                      "price": str(values["price"])})


def set_variant_active(
    settings: Settings, admin_id: str, tenant_id: str, variant_id: str, active: bool
) -> None:
    variant_id = v.uuid_value(variant_id, "Variant")
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        if not ta.update_variant(variant_id, is_active=active):
            raise v.ServiceError("Variant not found.", 404)
        audit.record(settings, admin_id, tenant_id, "catalogue.variant_activated" if active
                     else "catalogue.variant_deactivated", "product_variant", variant_id)
