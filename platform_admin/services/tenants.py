"""Tenant lifecycle: create (draft), profile, rules/AI configuration, readiness, activation."""

from __future__ import annotations

import json
from typing import Any

from saas import platform
from saas.bot import redact_sensitive_values
from saas.tenant_admin import tenant_admin

from ..config import Settings
from . import audit, validation as v
from .config_merge import deep_merge

RULE_DOCUMENTS = (
    ("business_rules", "Business rules"),
    ("bot_persona", "Bot persona"),
    ("business_hours", "Business hours"),
    ("configuration", "Configuration"),
)

# -- WhatsApp AI agent onboarding form (sections A-G) ---------------------------------
WHATSAPP_BUSINESS_TYPES = ("Auto Parts", "Bakery", "Retail", "Food & Beverage", "Electronics", "Fashion")
BOT_TONES = ("Professional Short", "Friendly Sheng", "Formal")
TONE_DERIVED = {
    "Professional Short": {"max_chars": 250, "max_lines": 2, "emoji": "no"},
    "Friendly Sheng": {"max_chars": 400, "max_lines": 4, "emoji": "minimal"},
    "Formal": {"max_chars": 350, "max_lines": 3, "emoji": "no"},
}
BUSINESS_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
PAYMENT_METHODS = ("M-Pesa", "Cash", "Bank")
ON_PHOTO_CHOICES = ("ask_car_model", "try_identify_part", "send_to_human")
ON_PART_NOT_FOUND_CHOICES = ("show_alternatives_list", "ask_chassis_number", "escalate")
ON_PRICE_ASKED_CHOICES = ("show_price_with_order_buttons", "send_quote_pdf")
PRICING_VISIBILITY_CHOICES = ("show_exact", "show_from_price", "hide_ask_human")
SPAM_ACTIONS = ("send_to_human",)
WINDOW_24H_ACTIONS = ("send_template_to_reopen", "wait_for_user")
AUTO_CLOSE_CHOICES = ("24h", "48h", "72h", "never")
ESCALATION_TARGETS = ("Owner", "Staff")
ORDER_FLOW_LABELS = {
    "confirm_part": "Confirm",
    "ask_quantity": "Qty",
    "mpesa_payment": "M-Pesa",
    "ask_delivery": "Delivery",
}
RAG_PRIORITY = ("faqs", "products_sheet", "extra_info", "website", "documents")
CATALOGUE_COLUMNS = (
    "SKU", "Product Name", "Variant", "Category", "Description", "Price", "Currency",
    "Availability", "Stock Quantity", "Reorder Level", "Image URL",
)


def optional_choice(value: Any, field: str, choices: tuple[str, ...]) -> str | None:
    cleaned = (str(value).strip() if value is not None else "")
    if not cleaned:
        return None
    if cleaned not in choices:
        raise v.ServiceError(f"{field} must be one of: {', '.join(choices)}.")
    return cleaned


def phone_value(value: Any, field: str, *, required: bool = False) -> str | None:
    cleaned = (str(value).strip() if value is not None else "")
    if not cleaned:
        if required:
            raise v.ServiceError(f"{field} is required.")
        return None
    if len(cleaned) > 20 or not all(char.isdigit() or char in "+ -()" for char in cleaned):
        raise v.ServiceError(f"{field} must be a phone number.")
    return cleaned


def time_value(value: Any, field: str, default: str) -> str:
    cleaned = str(value).strip() if value is not None else ""
    if not cleaned:
        return default
    parts = cleaned.split(":")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        raise v.ServiceError(f"{field} must be a time such as 08:00.")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise v.ServiceError(f"{field} must be a time such as 08:00.")
    return f"{hour:02d}:{minute:02d}"


def parse_faqs(text: Any) -> list[dict[str, str]]:
    """Parse the bulk FAQ textarea: 'Q:' starts a pair, 'A:' fills the answer."""
    faqs: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in str(text or "").splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("q:"):
            current = {"question": stripped[2:].strip(), "answer": ""}
            faqs.append(current)
        elif lowered.startswith("a:") and current is not None:
            answer = stripped[2:].strip()
            current["answer"] = f"{current['answer']} {answer}".strip() if current["answer"] else answer
    return [entry for entry in faqs if entry["question"]]


def derive_order_flow(payment_methods: list[str], delivery: str | None) -> list[str]:
    """The order flow is never asked for: it is derived from the payment and delivery rules."""
    flow = ["confirm_part", "ask_quantity"]
    if "M-Pesa" in payment_methods:
        flow.append("mpesa_payment")
    if delivery and delivery != "Pickup only":
        flow.append("ask_delivery")
    return flow


def order_flow_badge(flow: list[str]) -> str:
    return " > ".join(ORDER_FLOW_LABELS.get(step, step) for step in flow)


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
    activity = v.text(form.get("activity"), "Activity", max_length=200, required=False)
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        current = ta.get_settings() or {}
        # Timezone and currency stay editable for operators who post them, but the
        # dashboard form only asks for the business name and its activity.
        timezone = v.timezone(form.get("timezone") or current.get("timezone") or "Africa/Nairobi")
        currency = v.currency(form.get("currency") or current.get("currency") or "KES")
        configuration = dict(current.get("configuration") or {})
        if activity is None:
            configuration.pop("activity", None)
        else:
            configuration["activity"] = activity
        ta.rename(name)
        ta.update_settings(timezone=timezone, currency=currency, configuration=configuration)
        audit.record(
            settings, admin_id, tenant_id, "tenant.profile_updated", "tenant", tenant_id,
            {"name": name, "activity": activity, "timezone": timezone, "currency": currency},
        )


def mask_documents(stored: dict[str, Any]) -> dict[str, str]:
    """The four stored documents as pretty JSON, for the raw JSON editors.

    A value whose key looks like a credential is replaced by :data:`audit.REDACTED`, and
    :func:`update_rules` puts the stored value back when that document is saved unchanged.
    Masking therefore cannot destroy a credential, and a credential never reaches a browser.
    """
    return {
        column: json.dumps(audit.redact(stored.get(column) or {}), indent=2, sort_keys=True)
        for column, _label in RULE_DOCUMENTS
    }


def _restore_masked(submitted: Any, stored: Any) -> Any:
    """Put stored values back where the raw editor showed the redaction mask."""
    if not isinstance(submitted, dict):
        return submitted
    restored = dict(submitted)
    for key, value in submitted.items():
        previous = stored.get(key) if isinstance(stored, dict) else None
        if value == audit.REDACTED and previous is not None:
            restored[key] = previous
        elif isinstance(value, dict):
            restored[key] = _restore_masked(value, previous)
    return restored


def update_rules(settings: Settings, admin_id: str, tenant_id: str, form: dict[str, Any]) -> None:
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        current = ta.get_settings() or {}
        documents = {
            column: _restore_masked(
                v.json_object(form.get(column), label), current.get(column) or {}
            )
            for column, label in RULE_DOCUMENTS
        }
        ta.update_settings(**documents)
        # Section names only: values may be large or sensitive and do not belong in audit.
        audit.record(
            settings, admin_id, tenant_id, "tenant.rules_updated", "tenant_settings", tenant_id,
            {"sections": sorted(documents)},
        )


def _structured_documents(form: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the four settings documents from the A-G onboarding form fields."""
    display_name = v.text(form.get("wa_display_name"), "WhatsApp display name", max_length=100)
    business_type = optional_choice(form.get("business_type"), "Business type", WHATSAPP_BUSINESS_TYPES)
    if business_type is None:
        raise v.ServiceError("Business type is required.")
    number = phone_value(form.get("whatsapp_number"), "WhatsApp number")

    tone = optional_choice(form.get("tone"), "Tone", BOT_TONES)
    if tone is None:
        raise v.ServiceError("Tone is required.")
    bot_name = v.text(form.get("bot_name"), "Bot name", max_length=20, required=False) or display_name

    greeting = v.text(form.get("greeting"), "Greeting", max_length=2000)
    human_hours: dict[str, dict[str, Any]] = {}
    for day in BUSINESS_DAYS:
        is_open = form.get(f"day_open_{day}") == "1"
        entry: dict[str, Any] = {"is_open": is_open}
        if is_open:
            entry["open"] = time_value(form.get(f"day_open_time_{day}"), f"{day.title()} open time", "08:00")
            entry["close"] = time_value(form.get(f"day_close_time_{day}"), f"{day.title()} close time", "18:00")
        human_hours[day] = entry
    offline_message = v.text(
        form.get("human_offline_message"), "Offline message", max_length=2000, required=False
    ) or "Team yetu iko offline sai. Watarudi kesho 8am. Nime-save ujumbe wako."
    return _structured_documents_tail(
        form,
        bot_persona={**TONE_DERIVED[tone], "tone": tone, "bot_name": bot_name},
        business_hours={
            "greeting": greeting,
            "timezone": "Africa/Nairobi",
            "human_hours": human_hours,
            "human_offline_message": offline_message,
            "bot_hours": "24/7",
        },
        identity={
            "whatsapp_display_name": display_name,
            "business_type": business_type,
            "whatsapp_number": number,
        },
    )


def _structured_documents_tail(
    form: dict[str, Any], *, bot_persona: dict, business_hours: dict, identity: dict
) -> dict[str, dict[str, Any]]:
    description = v.text(form.get("business_description"), "Business description", max_length=4000)
    pricing_rule = optional_choice(form.get("pricing_rule"), "Pricing rule", ("Fixed", "Negotiable", "Quote-based"))
    discount_allowed = optional_choice(
        form.get("discount_allowed"), "Discount", ("No", "Up to 5%", "Up to 10%")
    )
    out_of_stock = optional_choice(
        form.get("out_of_stock"), "Out of stock", ("Suggest alternative", "Take order", "Escalate to human")
    )
    payment_methods = [
        method for method in (str(item).strip() for item in form.get("payment_methods") or [])
        if method in PAYMENT_METHODS
    ]
    delivery = optional_choice(
        form.get("delivery"), "Delivery", ("Pickup only", "Delivery within Nairobi", "Countrywide")
    )
    return_policy = v.text(form.get("return_policy"), "Return policy", max_length=500, required=False)
    sellable_products = [
        str(item).strip() for item in form.get("sellable_products") or [] if str(item).strip()
    ]
    escalation_tries = v.integer(form.get("escalation_tries"), "Escalation tries", minimum=1, default=2)
    escalation_transfer = optional_choice(
        form.get("escalation_transfer"), "Escalation target", ESCALATION_TARGETS
    ) or "Owner"
    escalation_whatsapp = phone_value(form.get("escalation_whatsapp"), "Escalation WhatsApp number")
    order_flow = derive_order_flow(payment_methods, delivery)
    return _structured_documents_config(
        form,
        bot_persona=bot_persona,
        business_hours=business_hours,
        business_rules={
            "description_paragraph": description,
            "pricing_rule": pricing_rule,
            "discount_allowed": discount_allowed,
            "out_of_stock": out_of_stock,
            "payment_methods": payment_methods,
            "delivery": delivery,
            "return_policy": return_policy,
            "sellable_products": sellable_products,
            "order_flow_derived": order_flow,
            "escalation": {
                "after_tries": escalation_tries,
                "transfer_to": escalation_transfer,
                "whatsapp": escalation_whatsapp,
            },
        },
        identity=identity,
    )


def _structured_documents_config(
    form: dict[str, Any], *, bot_persona: dict, business_hours: dict, business_rules: dict, identity: dict
) -> dict[str, dict[str, Any]]:
    on_photo = optional_choice(form.get("on_photo"), "On photo", ON_PHOTO_CHOICES)
    on_part_not_found = optional_choice(
        form.get("on_part_not_found"), "On part not found", ON_PART_NOT_FOUND_CHOICES
    )
    on_price_asked = optional_choice(form.get("on_price_asked"), "On price asked", ON_PRICE_ASKED_CHOICES)
    pricing_visibility = optional_choice(
        form.get("pricing_visibility"), "Pricing visibility", PRICING_VISIBILITY_CHOICES
    )
    spam_repeat = v.integer(form.get("spam_repeat"), "Spam repeat count", minimum=1, default=3)
    spam_action = optional_choice(form.get("spam_action"), "Spam action", SPAM_ACTIONS) or "send_to_human"
    business_rules.update({
        "on_photo": on_photo,
        "on_part_not_found": on_part_not_found,
        "on_price_asked": on_price_asked,
        "pricing_visibility": pricing_visibility,
        "spam_rule": {"repeat_count": spam_repeat, "action": spam_action},
    })

    provider = str(form.get("provider") or "").strip() or "meta_cloud_api"
    if provider != "meta_cloud_api":
        raise v.ServiceError("Only the Meta Cloud API provider is supported.")
    window_24h_action = optional_choice(
        form.get("window_24h_action"), "24h window action", WINDOW_24H_ACTIONS
    ) or "send_template_to_reopen"
    followup_template = v.text(
        form.get("followup_template_name"), "Follow-up template name", max_length=100, required=False
    ) or "order_followup_1"
    handover_number = phone_value(form.get("human_handover_number"), "Human handover number")
    auto_close = optional_choice(form.get("auto_close_ticket_after"), "Auto close", AUTO_CLOSE_CHOICES) or "48h"

    website_link = v.text(form.get("website_link"), "Website link", max_length=300, required=False)
    if website_link and not website_link.lower().startswith(("http://", "https://")):
        raise v.ServiceError("Website link must start with http:// or https://.")
    include_subpages = form.get("include_subpages") == "1"
    max_pages = v.integer(form.get("max_pages"), "Max pages", minimum=1, default=20)
    if max_pages > 100:
        raise v.ServiceError("Max pages must be at most 100.")
    faqs = parse_faqs(form.get("faqs_bulk"))
    documents = [str(item).strip() for item in form.get("documents") or [] if str(item).strip()]
    extra_info = v.text(form.get("extra_info"), "Extra information", max_length=4000, required=False)

    configuration = {
        "identity": identity,
        "config": {
            "provider": provider,
            "window_24h_action": window_24h_action,
            "followup_template_name": followup_template,
            "human_handover_number": handover_number,
            "auto_close_ticket_after": auto_close,
        },
        "knowledge_base": {
            "website_link": website_link,
            "scrape_options": {"include_subpages": include_subpages, "max_pages": max_pages},
            "faqs": faqs,
            "documents": documents,
            "extra_info": extra_info,
            "rag_priority": list(RAG_PRIORITY),
        },
    }
    return {
        "bot_persona": bot_persona,
        "business_hours": business_hours,
        "business_rules": business_rules,
        "configuration": configuration,
    }


def update_rules_from_form(
    settings: Settings, admin_id: str, tenant_id: str, form: dict[str, Any]
) -> None:
    """Save the structured WhatsApp AI agent onboarding form (sections A-G at once).

    Each document is deep-merged over the tenant's existing one, so values saved earlier --
    through the raw JSON editors or through a single-section save -- survive. This is the
    "new client" path: every field is submitted, so it keeps the onboarding validation.
    """
    documents = _structured_documents(form)
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        current = ta.get_settings() or {}
        merged = {
            column: deep_merge(current.get(column) or {}, documents[column])
            for column in documents
        }
        ta.update_settings(**merged)
        audit.record(
            settings, admin_id, tenant_id, "tenant.rules_updated", "tenant_settings", tenant_id,
            {"sections": sorted(merged), "form": "whatsapp_onboarding"},
        )


def rules_form_values(settings: Settings, tenant_id: str) -> dict[str, Any]:
    """Prefill values for the structured onboarding form, derived from the stored documents."""
    profile = get_profile(settings, tenant_id)
    stored = profile["settings"]
    configuration = stored.get("configuration") or {}
    persona = stored.get("bot_persona") or {}
    hours = stored.get("business_hours") or {}
    rules = stored.get("business_rules") or {}
    human_hours = hours.get("human_hours") or {}
    days = {
        day: {
            "is_open": bool((human_hours.get(day) or {}).get("is_open")),
            "open": (human_hours.get(day) or {}).get("open", "08:00"),
            "close": (human_hours.get(day) or {}).get("close", "18:00"),
        }
        for day in BUSINESS_DAYS
    }
    faqs = (configuration.get("knowledge_base") or {}).get("faqs") or []
    faqs_bulk = "\n".join(
        f"Q: {entry.get('question', '')}\nA: {entry.get('answer', '')}"
        for entry in faqs
        if isinstance(entry, dict)
    )
    order_flow = rules.get("order_flow_derived") or derive_order_flow(
        rules.get("payment_methods") or [], rules.get("delivery")
    )
    return {
        "stored": stored,
        "identity": configuration.get("identity") or {},
        "api": configuration.get("config") or {},
        "knowledge": configuration.get("knowledge_base") or {},
        "persona": persona,
        "hours": hours,
        "rules": rules,
        "days": days,
        "faqs_bulk": faqs_bulk,
        "order_flow": order_flow,
        "order_flow_badge": order_flow_badge(order_flow),
    }


def bot_config_preview(stored: dict[str, Any]) -> dict[str, Any]:
    """Compose the single JSON configuration the bot consumes, for read-only preview.

    Secrets are redacted with the helper the bot uses before configuration reaches a prompt:
    this block is rendered to a browser, so a credential an operator pasted into a document
    must not appear here.
    """
    configuration = stored.get("configuration") or {}
    rules = stored.get("business_rules") or {}
    return redact_sensitive_values(
        {
            "identity": configuration.get("identity") or {},
            "persona": stored.get("bot_persona") or {},
            "hours_and_greeting": stored.get("business_hours") or {},
            "business_rules": rules,
            "config": configuration.get("config") or {"provider": "meta_cloud_api"},
            "knowledge_base": {
                **(configuration.get("knowledge_base") or {}),
                "rag_priority": list(RAG_PRIORITY),
            },
            "catalogue_schema": {
                "file": "catalogue-import-template.xlsx",
                "sheet": "Products",
                "columns": list(CATALOGUE_COLUMNS),
            },
        }
    )


def tenant_audit(settings: Settings, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with tenant_admin(tenant_id, settings.app_database_url) as ta:
        return ta.list_audit(limit)
