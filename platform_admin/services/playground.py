"""Admin adapter for the shared tenant-aware bot response engine."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence
from urllib.parse import urlparse

from saas.bot import (
    BotConfigurationError,
    BotContextError,
    BotProviderError,
    BotResponseEngine,
    BotResponseProvider,
    ConversationTurn,
    TenantBotContext,
    TenantBotContextLoader,
)

from ..config import Settings
from . import validation as v

MAX_CART_QUANTITY = 99


def load_tenant_context(settings: Settings, tenant_id: str) -> dict[str, Any]:
    tenant_id = v.uuid_value(tenant_id, "Tenant")
    try:
        context = TenantBotContextLoader(settings.app_database_url).load(tenant_id)
    except BotContextError as error:
        raise v.ServiceError(str(error), 404) from error
    return {
        "tenant": context.tenant,
        "settings": context.settings,
        "catalogue": _catalogue_for_display(context.catalogue),
        "bot_context": context,
    }


def _catalogue_for_display(catalogue: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    products = []
    for source in catalogue:
        product = dict(source)
        product["id"] = str(product.get("id"))
        product["variants"] = [
            {**variant, "id": str(variant.get("id"))}
            for variant in product.get("variants") or []
            if isinstance(variant, dict)
        ]
        attributes = product.get("attributes")
        image_url = None
        if isinstance(attributes, dict):
            image_url = attributes.get("image_url") or attributes.get("image")
            images = attributes.get("images")
            if not image_url and isinstance(images, list) and images:
                image_url = images[0]
        if isinstance(image_url, str):
            parsed = urlparse(image_url)
            product["image_url"] = image_url if parsed.scheme == "https" and parsed.netloc else None
        else:
            product["image_url"] = None
        products.append(product)
    return products


def validate_product(
    catalogue: Sequence[dict[str, Any]], product_id: str
) -> dict[str, Any]:
    product_id = v.uuid_value(product_id, "Product")
    product = next((item for item in catalogue if str(item.get("id")) == product_id), None)
    if product is None:
        raise v.ServiceError("That active product is not available for this tenant.", 404)
    return product


def validate_selection(
    catalogue: Sequence[dict[str, Any]],
    product_id: str,
    variant_id: str,
    *,
    require_available: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    product = validate_product(catalogue, product_id)
    variant_id = v.uuid_value(variant_id, "Variant")
    variant = next(
        (
            item
            for item in product.get("variants") or []
            if str(item.get("id")) == variant_id
        ),
        None,
    )
    if variant is None:
        raise v.ServiceError("That active variant is not available for this product.", 404)
    if require_available and variant.get("availability") == "OUT_OF_STOCK":
        raise v.ServiceError(
            f"{product.get('name')} ({variant.get('name')}) is out of stock.", 409
        )
    return product, variant


def add_cart_item(
    catalogue: Sequence[dict[str, Any]],
    cart: Sequence[dict[str, Any]],
    product_id: str,
    variant_id: str,
    quantity: Any,
) -> list[dict[str, Any]]:
    product, variant = validate_selection(catalogue, product_id, variant_id)
    amount = v.integer(quantity, "Quantity", minimum=1, default=1)
    if amount > MAX_CART_QUANTITY:
        raise v.ServiceError(f"Quantity must be between 1 and {MAX_CART_QUANTITY}.")
    updated = [dict(item) for item in cart if isinstance(item, dict)]
    existing = next(
        (
            item
            for item in updated
            if str(item.get("product_id")) == str(product["id"])
            and str(item.get("variant_id")) == str(variant["id"])
        ),
        None,
    )
    if existing is None:
        updated.append(
            {
                "product_id": str(product["id"]),
                "variant_id": str(variant["id"]),
                "quantity": amount,
            }
        )
    else:
        new_quantity = int(existing.get("quantity", 0)) + amount
        if new_quantity > MAX_CART_QUANTITY:
            raise v.ServiceError(f"Cart quantity cannot exceed {MAX_CART_QUANTITY}.")
        existing["quantity"] = new_quantity
    configured_stock = variant.get("stock_quantity")
    final_quantity = amount if existing is None else int(existing["quantity"])
    if (
        variant.get("availability") == "IN_STOCK"
        and configured_stock is not None
        and final_quantity > int(configured_stock)
    ):
        raise v.ServiceError(
            f"Only {configured_stock} units are configured in stock.", 409
        )
    return updated


def change_cart_item(
    catalogue: Sequence[dict[str, Any]],
    cart: Sequence[dict[str, Any]],
    product_id: str,
    variant_id: str,
    quantity: Any,
) -> list[dict[str, Any]]:
    amount = v.integer(quantity, "Quantity", minimum=0)
    product, variant = validate_selection(
        catalogue,
        product_id,
        variant_id,
        require_available=amount > 0,
    )
    if amount > MAX_CART_QUANTITY:
        raise v.ServiceError(f"Quantity must be between 0 and {MAX_CART_QUANTITY}.")
    updated = [
        dict(item)
        for item in cart
        if isinstance(item, dict)
        and not (
            str(item.get("product_id")) == str(product["id"])
            and str(item.get("variant_id")) == str(variant["id"])
        )
    ]
    if amount:
        configured_stock = variant.get("stock_quantity")
        if (
            variant.get("availability") == "IN_STOCK"
            and configured_stock is not None
            and amount > int(configured_stock)
        ):
            raise v.ServiceError(
                f"Only {configured_stock} units are configured in stock.", 409
            )
        updated.append(
            {
                "product_id": str(product["id"]),
                "variant_id": str(variant["id"]),
                "quantity": amount,
            }
        )
    return updated


def cart_summary(
    catalogue: Sequence[dict[str, Any]],
    cart: Sequence[dict[str, Any]],
    currency: Any,
) -> dict[str, Any]:
    products = {str(product.get("id")): product for product in catalogue}
    items = []
    normalized = []
    total = Decimal("0.00")
    for raw in cart:
        if not isinstance(raw, dict):
            continue
        product = products.get(str(raw.get("product_id")))
        if product is None:
            continue
        variant = next(
            (
                item
                for item in product.get("variants") or []
                if str(item.get("id")) == str(raw.get("variant_id"))
            ),
            None,
        )
        try:
            quantity = int(raw.get("quantity", 0))
        except (TypeError, ValueError):
            continue
        if variant is None or not 1 <= quantity <= MAX_CART_QUANTITY:
            continue
        unit_price = v.price(variant.get("price"))
        subtotal = unit_price * quantity
        total += subtotal
        normalized.append(
            {
                "product_id": str(product["id"]),
                "variant_id": str(variant["id"]),
                "quantity": quantity,
            }
        )
        items.append(
            {
                "product_id": str(product["id"]),
                "product_name": str(product.get("name") or ""),
                "variant_id": str(variant["id"]),
                "variant_name": str(variant.get("name") or ""),
                "availability": str(variant.get("availability") or "IN_STOCK"),
                "availability_note": (
                    "Made to order; lead time may apply."
                    if variant.get("availability") == "MADE_TO_ORDER"
                    else (
                        "Out of stock; remove this item before checkout."
                        if variant.get("availability") == "OUT_OF_STOCK"
                        else None
                    )
                ),
                "quantity": quantity,
                "unit_price": f"{unit_price:.2f}",
                "subtotal": f"{subtotal:.2f}",
            }
        )
    return {
        "currency": v.currency(currency or "KES"),
        "items": items,
        "normalized": normalized,
        "item_count": sum(item["quantity"] for item in items),
        "total": f"{total:.2f}",
    }


def checkout_capabilities(tenant_settings: dict[str, Any]) -> dict[str, Any]:
    configuration = tenant_settings.get("configuration")
    configuration = configuration if isinstance(configuration, dict) else {}

    def methods_for(name: str) -> tuple[bool, list[str]]:
        methods = configuration.get(f"{name}_methods")
        if isinstance(methods, list):
            labels = [
                str(item).strip()
                for item in methods
                if isinstance(item, str) and str(item).strip()
            ]
            return bool(labels), labels
        section = configuration.get(name)
        if isinstance(section, dict) and section.get("enabled") is True:
            section_methods = section.get("methods")
            section_methods = section_methods if isinstance(section_methods, list) else []
            labels = [
                str(item).strip()
                for item in section_methods
                if isinstance(item, str) and str(item).strip()
            ]
            return True, labels
        return False, []

    delivery_enabled, delivery_methods = methods_for("delivery")
    payment_enabled, payment_methods = methods_for("payment")
    return {
        "delivery_configured": delivery_enabled,
        "delivery_methods": delivery_methods,
        "payment_configured": payment_enabled,
        "payment_methods": payment_methods,
    }


def demo_confirmation(
    tenant_settings: dict[str, Any],
    cart: dict[str, Any],
    customer_name: Any,
    delivery_details: Any,
) -> dict[str, Any]:
    if not cart["items"]:
        raise v.ServiceError("Add at least one item before demo checkout.")
    unavailable = [
        item for item in cart["items"] if item.get("availability") == "OUT_OF_STOCK"
    ]
    if unavailable:
        raise v.ServiceError("Remove out-of-stock items before demo checkout.", 409)
    capabilities = checkout_capabilities(tenant_settings)
    name = v.text(customer_name, "Sample customer name", max_length=120) or ""
    details = None
    if capabilities["delivery_configured"]:
        details = v.text(delivery_details, "Sample delivery details", max_length=500)
    return {
        "demo": True,
        "customer_name": name,
        "delivery_details": details,
        "cart": cart,
        "capabilities": capabilities,
    }


def respond_to_customer(
    settings: Settings,
    provider: BotResponseProvider | None,
    tenant_id: str,
    customer_message: str,
    history: Sequence[dict[str, str]],
    tenant_context: TenantBotContext | None = None,
) -> dict[str, Any]:
    tenant_id = v.uuid_value(tenant_id, "Tenant")
    message = v.text(customer_message, "Customer message", max_length=2_000) or ""
    if provider is None:
        raise v.ServiceError(
            "The AI provider is not configured. Set OPENAI_API_KEY on the server.", 503
        )
    turns = [
        ConversationTurn(str(item.get("role", "")), str(item.get("text", "")))
        for item in history
        if isinstance(item, dict)
    ]
    engine = BotResponseEngine(TenantBotContextLoader(settings.app_database_url), provider)
    try:
        response = engine.respond(
            tenant_id,
            message,
            turns,
            channel="admin-playground",
            safe_test_mode=True,
            context=tenant_context,
        )
    except BotContextError as error:
        raise v.ServiceError(str(error), 404) from error
    except (BotConfigurationError, BotProviderError) as error:
        raise v.ServiceError(str(error), 502) from error
    except ValueError as error:
        raise v.ServiceError("Conversation history is invalid. Start a new conversation.") from error
    return {
        "mode": "safe-test",
        "tenant": response.context.tenant,
        "response": response.text,
        "customer_message": message,
    }
