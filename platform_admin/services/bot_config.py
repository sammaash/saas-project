"""Partial updates of a tenant's saved WhatsApp bot configuration.

The onboarding form is a *configuration manager*, not a one-shot wizard: an administrator
opens an existing client, sees the settings that are actually saved, and changes one field
or one section. This module implements that contract.

How configuration is stored and loaded
--------------------------------------
``public.tenant_settings`` holds one row per tenant and is guarded by row level security
through ``app.current_tenant_id()``. Six columns carry the configuration:

* ``timezone`` and ``currency`` -- scalar columns,
* ``configuration``, ``bot_persona``, ``business_hours`` and ``business_rules`` -- jsonb.

``TenantAdmin.update_settings`` writes only the columns named in the call, and
``TenantBotContextLoader.load`` reads the row afresh for every inbound message, so a saved
change reaches the bot on its next reply with no cache to invalidate. Nothing here
introduces a second configuration format or a second source of truth.

The partial-update contract
---------------------------
A section builder returns a *patch*: only the keys the submitted form actually carried.

* A key that is **absent** from the request is never written, so it cannot be reset to a
  default, an empty string or ``None``.
* A key that is **present and empty** is an explicit clear: it is removed from the stored
  document, so "unset" is a real state rather than a fabricated default. This is what lets
  an administrator change one setting on a client that was never fully configured without
  having to complete the whole onboarding form first.
* A key that is **present and non-empty** is validated against the same whitelist and
  choice sets the onboarding form uses. An unknown choice, a malformed time, number, phone
  number or URL is rejected and nothing is written.

The stricter onboarding requirements (a display name, a business type, a tone, a greeting
and a business description are all mandatory) still apply to the "new client" path in
``tenants.update_rules_from_form``, which submits every field at once.

Patches are deep-merged into the stored document, so changing
``business_rules.escalation.whatsapp`` preserves ``after_tries`` and ``transfer_to``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from saas.bot import redact_sensitive_values
from saas.tenant_admin import tenant_admin

from ..config import Settings
from . import audit, validation as v
from .config_merge import (
    UNSET,
    ItemRemoval,
    ItemUpdate,
    deep_merge,
    field_paths,
    prune_columns,
)
from .tenants import (
    AUTO_CLOSE_CHOICES,
    BOT_TONES,
    BUSINESS_DAYS,
    ESCALATION_TARGETS,
    ON_PART_NOT_FOUND_CHOICES,
    ON_PHOTO_CHOICES,
    ON_PRICE_ASKED_CHOICES,
    PAYMENT_METHODS,
    PRICING_VISIBILITY_CHOICES,
    SPAM_ACTIONS,
    TONE_DERIVED,
    WINDOW_24H_ACTIONS,
    WHATSAPP_BUSINESS_TYPES,
    derive_order_flow,
    optional_choice,
    parse_faqs,
    phone_value,
    time_value,
)

#: Fields the browser posts as a repeated control rather than a single value.
MULTI_VALUE_FIELDS = ("payment_methods", "sellable_products", "documents")

#: ``request.form`` cannot tell "the operator cleared this" from "this was not submitted",
#: because an unchecked box and an untouched field are both simply absent. The section
#: templates therefore post a ``present_<name>`` marker next to every control that can
#: legitimately submit nothing, and :class:`SubmittedForm` treats either as submitted.
PRESENCE_PREFIX = "present_"

#: A submit button may name the branch of the section it saves, for field-level saves.
FOCUS_FIELD = "focus"

#: The version of the configuration the rendered form was built from (optimistic locking).
VERSION_FIELD = "config_version"

CONFLICT_MESSAGE = (
    "This configuration changed in another session. Reload the page and apply your "
    "change again."
)


def _choice_or_unset(value: Any, field: str, choices: tuple[str, ...]) -> Any:
    """A closed-choice field: an empty value means "unset", an unknown one is rejected."""
    cleaned = optional_choice(value, field, choices)
    return UNSET if cleaned is None else cleaned


def _text_or_unset(value: Any, field: str, *, max_length: int) -> Any:
    cleaned = v.text(value, field, max_length=max_length, required=False)
    return UNSET if cleaned is None else cleaned


def _integer_or_unset(value: Any, field: str, *, minimum: int, maximum: int | None = None) -> Any:
    if value is None or str(value).strip() == "":
        return UNSET
    number = v.integer(value, field, minimum=minimum, default=minimum)
    if maximum is not None and number > maximum:
        raise v.ServiceError(f"{field} must be at most {maximum}.")
    return number


class SubmittedForm:
    """A submitted section form that remembers which fields were actually sent."""

    def __init__(self, form: Mapping[str, Any] | None = None):
        self._values: dict[str, Any] = {str(key): value for key, value in dict(form or {}).items()}
        self._sent = set(self._values)

    def submitted(self, *names: str) -> bool:
        return any(
            name in self._sent or f"{PRESENCE_PREFIX}{name}" in self._sent for name in names
        )

    def value(self, name: str) -> Any:
        return self._values.get(name)

    def values(self, name: str) -> list[str]:
        """The multi-value form of ``name``: a repeated field, a list, or one scalar."""
        raw = self._values.get(name)
        if raw is None:
            return []
        items = raw if isinstance(raw, (list, tuple)) else [raw]
        return [str(item).strip() for item in items if str(item).strip()]


def _column(column: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Wrap a section patch in its settings column, or nothing when no field was sent."""
    return {column: patch} if patch else {}


def _nested(key: str, patch: dict[str, Any]) -> dict[str, Any]:
    return {key: patch} if patch else {}


def _phone_or_unset(value: Any, field: str) -> Any:
    cleaned = phone_value(value, field)
    return UNSET if cleaned is None else cleaned


def _website_or_unset(value: Any) -> Any:
    link = v.text(value, "Website link", max_length=300, required=False)
    if link is None:
        return UNSET
    if not link.lower().startswith(("http://", "https://")):
        raise v.ServiceError("Website link must start with http:// or https://.")
    return link


# ------------------------------------------------------------------ section builders
def _identity_patch(form: SubmittedForm) -> dict[str, Any]:
    """A. WhatsApp identity -> ``configuration.identity``."""
    patch: dict[str, Any] = {}
    if form.submitted("wa_display_name"):
        patch["whatsapp_display_name"] = _text_or_unset(
            form.value("wa_display_name"), "WhatsApp display name", max_length=100
        )
    if form.submitted("business_type"):
        patch["business_type"] = _choice_or_unset(
            form.value("business_type"), "Business type", WHATSAPP_BUSINESS_TYPES
        )
    if form.submitted("whatsapp_number"):
        patch["whatsapp_number"] = _phone_or_unset(
            form.value("whatsapp_number"), "WhatsApp number"
        )
    return _column("configuration", _nested("identity", patch))


def _persona_patch(form: SubmittedForm) -> dict[str, Any]:
    """B. Bot persona -> ``bot_persona``; the tone also drives the reply limits."""
    patch: dict[str, Any] = {}
    if form.submitted("tone"):
        tone = _choice_or_unset(form.value("tone"), "Tone", BOT_TONES)
        patch["tone"] = tone
        if tone is not UNSET:
            patch.update(TONE_DERIVED[tone])
    if form.submitted("bot_name"):
        patch["bot_name"] = _text_or_unset(form.value("bot_name"), "Bot name", max_length=20)
    return _column("bot_persona", patch)


def _hours_patch(form: SubmittedForm) -> dict[str, Any]:
    """C. Greeting and human hours -> ``business_hours``."""
    patch: dict[str, Any] = {}
    if form.submitted("greeting"):
        patch["greeting"] = _text_or_unset(
            form.value("greeting"), "Greeting", max_length=2000
        )
    days: dict[str, Any] = {}
    for day in BUSINESS_DAYS:
        if not form.submitted(
            f"day_open_{day}", f"day_open_time_{day}", f"day_close_time_{day}"
        ):
            continue
        if form.value(f"day_open_{day}") == "1":
            days[day] = {
                "is_open": True,
                "open": time_value(
                    form.value(f"day_open_time_{day}"), f"{day.title()} open time", "08:00"
                ),
                "close": time_value(
                    form.value(f"day_close_time_{day}"), f"{day.title()} close time", "18:00"
                ),
            }
        else:
            # A closed day keeps no hours: UNSET drops them rather than inventing 08:00-18:00.
            days[day] = {"is_open": False, "open": UNSET, "close": UNSET}
    if days:
        patch["human_hours"] = days
    if form.submitted("human_offline_message"):
        patch["human_offline_message"] = _text_or_unset(
            form.value("human_offline_message"), "Offline message", max_length=2000
        )
    return _column("business_hours", patch)

_RULE_TEXT_FIELDS = (
    ("description_paragraph", "Business description", 4000),
    ("return_policy", "Return policy", 500),
)
_RULE_CHOICE_FIELDS = (
    ("pricing_rule", "Pricing rule", ("Fixed", "Negotiable", "Quote-based")),
    ("discount_allowed", "Discount", ("No", "Up to 5%", "Up to 10%")),
    ("out_of_stock", "Out of stock", ("Suggest alternative", "Take order", "Escalate to human")),
    ("delivery", "Delivery", ("Pickup only", "Delivery within Nairobi", "Countrywide")),
)


def _rules_patch(form: SubmittedForm) -> dict[str, Any]:
    """D. Business rules -> ``business_rules``."""
    patch: dict[str, Any] = {}
    for name, label, max_length in _RULE_TEXT_FIELDS:
        if form.submitted(name):
            patch[name] = _text_or_unset(form.value(name), label, max_length=max_length)
    for name, label, choices in _RULE_CHOICE_FIELDS:
        if form.submitted(name):
            patch[name] = _choice_or_unset(form.value(name), label, choices)
    if form.submitted("payment_methods"):
        patch["payment_methods"] = [
            method for method in form.values("payment_methods") if method in PAYMENT_METHODS
        ]
    if form.submitted("sellable_products"):
        patch["sellable_products"] = form.values("sellable_products")
    escalation: dict[str, Any] = {}
    if form.submitted("escalation_tries"):
        escalation["after_tries"] = _integer_or_unset(
            form.value("escalation_tries"), "Escalation tries", minimum=1
        )
    if form.submitted("escalation_transfer"):
        escalation["transfer_to"] = _choice_or_unset(
            form.value("escalation_transfer"), "Escalation target", ESCALATION_TARGETS
        )
    if form.submitted("escalation_whatsapp"):
        escalation["whatsapp"] = _phone_or_unset(
            form.value("escalation_whatsapp"), "Escalation WhatsApp number"
        )
    if escalation:
        patch["escalation"] = escalation
    return _column("business_rules", patch)


def _rules_derive(column: str, document: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """The order flow is derived from the payment and delivery rules, never asked for."""
    if column != "business_rules" or not ({"payment_methods", "delivery"} & set(patch)):
        return document
    return {
        **document,
        "order_flow_derived": derive_order_flow(
            [m for m in document.get("payment_methods") or [] if m in PAYMENT_METHODS],
            document.get("delivery"),
        ),
    }


_LOGIC_CHOICE_FIELDS = (
    ("on_photo", "On photo", ON_PHOTO_CHOICES),
    ("on_part_not_found", "On part not found", ON_PART_NOT_FOUND_CHOICES),
    ("on_price_asked", "On price asked", ON_PRICE_ASKED_CHOICES),
    ("pricing_visibility", "Pricing visibility", PRICING_VISIBILITY_CHOICES),
)


def _logic_patch(form: SubmittedForm) -> dict[str, Any]:
    """E. WhatsApp logic -> ``business_rules``."""
    patch: dict[str, Any] = {}
    for name, label, choices in _LOGIC_CHOICE_FIELDS:
        if form.submitted(name):
            patch[name] = _choice_or_unset(form.value(name), label, choices)
    spam: dict[str, Any] = {}
    if form.submitted("spam_repeat"):
        spam["repeat_count"] = _integer_or_unset(
            form.value("spam_repeat"), "Spam repeat count", minimum=1
        )
    if form.submitted("spam_action"):
        spam["action"] = _choice_or_unset(form.value("spam_action"), "Spam action", SPAM_ACTIONS)
    if spam:
        patch["spam_rule"] = spam
    return _column("business_rules", patch)

_API_CHOICE_FIELDS = (
    ("window_24h_action", "24h window action", WINDOW_24H_ACTIONS),
    ("auto_close_ticket_after", "Auto close", AUTO_CLOSE_CHOICES),
)


def _api_patch(form: SubmittedForm) -> dict[str, Any]:
    """F. WhatsApp API configuration -> ``configuration.config``."""
    patch: dict[str, Any] = {}
    if form.submitted("provider"):
        provider = str(form.value("provider") or "").strip() or "meta_cloud_api"
        if provider != "meta_cloud_api":
            raise v.ServiceError("Only the Meta Cloud API provider is supported.")
        patch["provider"] = provider
    for name, label, choices in _API_CHOICE_FIELDS:
        if form.submitted(name):
            patch[name] = _choice_or_unset(form.value(name), label, choices)
    if form.submitted("followup_template_name"):
        patch["followup_template_name"] = _text_or_unset(
            form.value("followup_template_name"), "Follow-up template name", max_length=100
        )
    if form.submitted("human_handover_number"):
        patch["human_handover_number"] = _phone_or_unset(
            form.value("human_handover_number"), "Human handover number"
        )
    return _column("configuration", _nested("config", patch))


def _knowledge_patch(form: SubmittedForm) -> dict[str, Any]:
    """G. Knowledge base -> ``configuration.knowledge_base``."""
    patch: dict[str, Any] = {}
    if form.submitted("website_link"):
        patch["website_link"] = _website_or_unset(form.value("website_link"))
    scrape: dict[str, Any] = {}
    if form.submitted("include_subpages"):
        scrape["include_subpages"] = form.value("include_subpages") == "1"
    if form.submitted("max_pages"):
        scrape["max_pages"] = _integer_or_unset(
            form.value("max_pages"), "Max pages", minimum=1, maximum=100
        )
    if scrape:
        patch["scrape_options"] = scrape
    if form.submitted("faqs_bulk"):
        patch["faqs"] = parse_faqs(form.value("faqs_bulk"))
    if form.submitted("documents"):
        patch["documents"] = form.values("documents")
    if form.submitted("extra_info"):
        patch["extra_info"] = _text_or_unset(
            form.value("extra_info"), "Extra information", max_length=4000
        )
    return _column("configuration", _nested("knowledge_base", patch))


def _faq_patch(form: SubmittedForm) -> dict[str, Any]:
    """G.1 One FAQ, addressed by index, so no other FAQ can change."""
    index = v.integer(form.value("faq_index"), "FAQ", minimum=0, default=0)
    if form.submitted("faq_remove"):
        return _column("configuration", _nested("knowledge_base", {"faqs": ItemUpdate(index)}))
    question = v.text(form.value("faq_question"), "FAQ question", max_length=500)
    answer = v.text(form.value("faq_answer"), "FAQ answer", max_length=2000)
    return _column(
        "configuration",
        _nested(
            "knowledge_base",
            {"faqs": ItemUpdate(index, {"question": question, "answer": answer})},
        ),
    )


def _document_patch(form: SubmittedForm) -> dict[str, Any]:
    """G.2 Removing one uploaded document is an explicit, single-item action."""
    name = v.text(form.value("document_name"), "Document", max_length=300)
    return _column("configuration", _nested("knowledge_base", {"documents": ItemRemoval(name)}))
# ---------------------------------------------------------------------- the registry
@dataclass(frozen=True)
class SectionSpec:
    """One labelled section of the onboarding form and the fields it owns."""

    name: str
    label: str
    columns: tuple[str, ...]
    build: Callable[[SubmittedForm], dict[str, Any]]
    derive: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None
    #: Checkbox groups can legitimately submit nothing, so the template posts a
    #: ``present_<name>`` marker for each of them. Everything else is always submitted.
    markers: tuple[str, ...] = ()


SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec("identity", "WhatsApp identity", ("configuration",), _identity_patch),
    SectionSpec("persona", "Bot persona", ("bot_persona",), _persona_patch),
    SectionSpec("hours", "Business hours", ("business_hours",), _hours_patch),
    SectionSpec(
        "rules",
        "Business rules",
        ("business_rules",),
        _rules_patch,
        derive=_rules_derive,
        markers=("payment_methods", "sellable_products"),
    ),
    SectionSpec("logic", "WhatsApp logic", ("business_rules",), _logic_patch),
    SectionSpec("api", "API configuration", ("configuration",), _api_patch),
    SectionSpec(
        "knowledge",
        "Knowledge base",
        ("configuration",),
        _knowledge_patch,
        markers=("include_subpages", "documents"),
    ),
    SectionSpec("faq", "One FAQ", ("configuration",), _faq_patch),
    SectionSpec("document", "One document", ("configuration",), _document_patch),
)
SECTION_BY_NAME = {spec.name: spec for spec in SECTIONS}
SECTION_LABELS = {spec.name: spec.label for spec in SECTIONS}


def section_spec(name: str) -> SectionSpec:
    spec = SECTION_BY_NAME.get(str(name))
    if spec is None:
        raise v.ServiceError("Unknown configuration section.", 404)
    return spec


def version_of(stored: Mapping[str, Any]) -> str:
    """The version a rendered form was built from: ``tenant_settings.updated_at``."""
    value = (stored or {}).get("updated_at")
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def load_configuration(settings: Settings, tenant_id: str) -> dict[str, Any]:
    """Read the tenant's stored configuration exactly as the bot runtime loads it."""
    with tenant_admin(tenant_id, settings.app_database_url) as ta:
        stored = ta.get_settings()
    if stored is None:
        raise v.ServiceError("Tenant not found.", 404)
    return stored


def apply_section(
    settings: Settings,
    admin_id: str,
    tenant_id: str,
    section: str,
    form: Mapping[str, Any] | SubmittedForm,
    *,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Merge one section's submitted fields into the stored configuration.

    Only the columns named by the section's patch are written, and only the keys the form
    actually carried are merged, so every unrelated setting survives. Returns the stored
    configuration after the write, so the caller re-renders from the authoritative state
    rather than from what the browser believed.
    """
    spec = section_spec(section)
    submitted = form if isinstance(form, SubmittedForm) else SubmittedForm(form)
    patch = spec.build(submitted)
    focus = str(submitted.value(FOCUS_FIELD) or "").strip()
    if focus:
        patch = prune_columns(patch, tuple(part for part in focus.split(".") if part))
    if not patch:
        raise v.ServiceError("Nothing to save: this section submitted no changes.")
    with v.translated(), tenant_admin(tenant_id, settings.app_database_url) as ta:
        current = ta.get_settings() or {}
        if expected_version and version_of(current) != expected_version:
            raise v.ServiceError(CONFLICT_MESSAGE, 409)
        updates: dict[str, Any] = {}
        for column, column_patch in patch.items():
            document = deep_merge(current.get(column) or {}, column_patch)
            if spec.derive is not None:
                document = spec.derive(column, document, column_patch)
            updates[column] = document
        ta.update_settings(**updates)
        # Section and field names only: values may be large or sensitive.
        audit.record(
            settings, admin_id, tenant_id, "tenant.config_updated", "tenant_settings", tenant_id,
            {"section": spec.name, "fields": sorted(field_paths(patch))},
        )
    return load_configuration(settings, tenant_id)


def field_values(stored: Mapping[str, Any]) -> dict[str, Any]:
    """Project the stored documents onto the flat field names the section forms use.

    Nothing is defaulted: a field the tenant never set is ``None`` (or an empty list), so
    the form can show "not set" instead of a hardcoded value that would look saved.
    """
    configuration = stored.get("configuration") or {}
    identity = configuration.get("identity") or {}
    api = configuration.get("config") or {}
    knowledge = configuration.get("knowledge_base") or {}
    scrape = knowledge.get("scrape_options") or {}
    persona = stored.get("bot_persona") or {}
    hours = stored.get("business_hours") or {}
    human_hours = hours.get("human_hours") or {}
    rules = stored.get("business_rules") or {}
    escalation = rules.get("escalation") or {}
    spam = rules.get("spam_rule") or {}
    faqs = [entry for entry in knowledge.get("faqs") or [] if isinstance(entry, dict)]
    values: dict[str, Any] = {
        "wa_display_name": identity.get("whatsapp_display_name"),
        "business_type": identity.get("business_type"),
        "whatsapp_number": identity.get("whatsapp_number"),
        "tone": persona.get("tone"),
        "bot_name": persona.get("bot_name"),
        "greeting": hours.get("greeting"),
        "human_offline_message": hours.get("human_offline_message"),
        "business_timezone": hours.get("timezone"),
        "description_paragraph": rules.get("description_paragraph"),
        "pricing_rule": rules.get("pricing_rule"),
        "discount_allowed": rules.get("discount_allowed"),
        "out_of_stock": rules.get("out_of_stock"),
        "payment_methods": list(rules.get("payment_methods") or []),
        "delivery": rules.get("delivery"),
        "return_policy": rules.get("return_policy"),
        "sellable_products": list(rules.get("sellable_products") or []),
        "escalation_tries": escalation.get("after_tries"),
        "escalation_transfer": escalation.get("transfer_to"),
        "escalation_whatsapp": escalation.get("whatsapp"),
        "on_photo": rules.get("on_photo"),
        "on_part_not_found": rules.get("on_part_not_found"),
        "on_price_asked": rules.get("on_price_asked"),
        "pricing_visibility": rules.get("pricing_visibility"),
        "spam_repeat": spam.get("repeat_count"),
        "spam_action": spam.get("action"),
        "window_24h_action": api.get("window_24h_action"),
        "followup_template_name": api.get("followup_template_name"),
        "human_handover_number": api.get("human_handover_number"),
        "auto_close_ticket_after": api.get("auto_close_ticket_after"),
        "website_link": knowledge.get("website_link"),
        "include_subpages": bool(scrape.get("include_subpages")),
        "max_pages": scrape.get("max_pages"),
        "faqs": faqs,
        "documents": list(knowledge.get("documents") or []),
        "extra_info": knowledge.get("extra_info"),
        "order_flow_derived": list(rules.get("order_flow_derived") or []),
    }
    for day in BUSINESS_DAYS:
        entry = human_hours.get(day) or {}
        values[f"day_open_{day}"] = bool(entry.get("is_open"))
        values[f"day_open_time_{day}"] = entry.get("open")
        values[f"day_close_time_{day}"] = entry.get("close")
    values["faqs_bulk"] = "\n".join(
        f"Q: {entry.get('question', '')}\nA: {entry.get('answer', '')}" for entry in faqs
    )
    return values


def configuration_snapshot(settings: Settings, tenant_id: str) -> dict[str, Any]:
    """The saved configuration for one tenant, as the JSON read API returns it.

    Secrets are redacted with the same helper the bot uses before configuration reaches a
    prompt, so a credential an operator pasted into a document cannot reach a browser.
    """
    stored = load_configuration(settings, tenant_id)
    return {
        "tenant_id": str(tenant_id),
        "version": version_of(stored),
        "fields": field_values(stored),
        "sections": [
            {"name": spec.name, "label": spec.label, "markers": list(spec.markers)}
            for spec in SECTIONS
        ],
        "stored": redact_sensitive_values(
            {
                "timezone": stored.get("timezone"),
                "currency": stored.get("currency"),
                "configuration": stored.get("configuration") or {},
                "bot_persona": stored.get("bot_persona") or {},
                "business_hours": stored.get("business_hours") or {},
                "business_rules": stored.get("business_rules") or {},
            }
        ),
    }

