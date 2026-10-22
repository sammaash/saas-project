"""Tenant-scoped WhatsApp menu for customer self-service.

The menu is a thin layer over the services that already exist:

* catalogue data comes from ``TenantAdmin.list_catalogue()``;
* order status is looked up by order number *and* the sender's WhatsApp identity;
* FAQs come from ``tenant_settings.configuration.knowledge_base.faqs``;
* staff assistance reuses the durable handover store.

**State is carried in the action id, not persisted.** Every tap is
``m1.<step>.<arg>.<expiry>``. Nothing is written per conversation, so the menu works on a
database that has not applied migration 0035 and one conversation's menu cannot leak into
another. The tenant always comes from the resolved channel, and every argument is
re-validated against the current tenant's data before use.

The menu is disabled unless ``tenant_settings.configuration.menu.enabled`` is ``true``.
``configuration`` is the documented catch-all JSONB for new settings (0006), so enabling it
needs no schema change.
"""

from __future__ import annotations

import logging
import hashlib
import copy
import re
import time
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from saas.tenant_admin import tenant_admin

from ..config import Settings
from . import handover as handover_service

logger = logging.getLogger("platform_admin.menu")

MENU_TTL_SECONDS = 1800
CLOCK_SKEW_SECONDS = 60
ACTION_PREFIX = "m1"
MAX_LIST_ROWS = 10

STEPS = frozenset(
    {
        "main",
        "option",
        "browse",
        "cat",
        "prod",
        "order",
        "my_order",
        "delivery",
        "faqs",
        "faq",
        "staff",
        "more",
    }
)

OTHER_CATEGORY = "other-products"
PAGE_SIZE = 9

ORDER_COMMAND_RE = re.compile(r"^order\s+([A-Za-z0-9][A-Za-z0-9-]{0,39})$", re.IGNORECASE)
ARG_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Exact option names accepted as a text fallback. Bare numbers are deliberately absent:
#: "1" is ordinary conversation, not a menu command.
OPTION_ALIASES: dict[str, str] = {
    "browse": "browse",
    "browse products": "browse",
    "products": "browse",
    "place an order": "order",
    "place order": "order",
    "order": "order",
    "order something": "order",
    "my order": "my_order",
    "my orders": "my_order",
    "check my order": "my_order",
    "delivery": "delivery",
    "collection": "delivery",
    "delivery & collection": "delivery",
    "delivery and collection": "delivery",
    "faq": "faqs",
    "faqs": "faqs",
    "talk to staff": "staff",
    "staff": "staff",
    "human": "staff",
    "speak to staff": "staff",
}

STATUS_LABELS = {
    "draft": "Draft (not confirmed yet)",
    "pending_payment": "Awaiting payment",
    "confirmed": "Confirmed",
    "in_progress": "In progress",
    "ready": "Ready",
    "completed": "Completed",
    "cancelled": "Cancelled",
}

AVAILABILITY_LABELS = {
    "IN_STOCK": "in stock",
    "MADE_TO_ORDER": "made to order",
    "OUT_OF_STOCK": "out of stock",
}

#: Stable action types understood by the configurable menu. These map to the existing
#: menu implementations; configuration never contains executable code.
MENU_ACTIONS: tuple[str, ...] = (
    "browse_products",
    "place_order",
    "my_order",
    "delivery_collection",
    "faqs",
    "handover",
)
ACTION_STEPS: dict[str, str] = {
    "browse_products": "browse",
    "place_order": "order",
    "my_order": "my_order",
    "delivery_collection": "delivery",
    "faqs": "faqs",
    "handover": "staff",
}
#: Older/provisioning documents may use the internal step name as the action.
ACTION_ALIASES: dict[str, str] = {
    "browse": "browse_products",
    "order": "place_order",
    "delivery": "delivery_collection",
    "staff": "handover",
}
OPTION_DESCRIPTIONS: dict[str, str] = {
    "browse_products": "See what we have",
    "place_order": "How to order",
    "my_order": "Check your order",
    "delivery_collection": "Fees and hours",
    "faqs": "Common questions",
    "handover": "Reach a person",
}
DEFAULT_WELCOME_MESSAGE = "Main menu:"
DEFAULT_LANGUAGE = "en"
MAX_MENU_OPTIONS = 10
MAX_MENU_LABEL = 24
MAX_MENU_WELCOME = 300
MENU_OPTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MENU_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z]{2,8})?$")

#: The six historical options, in their historical order. This is both the compatibility
#: default and the initial state exposed by the dashboard builder.
DEFAULT_MENU_OPTIONS: tuple[dict[str, Any], ...] = (
    {"id": "browse_products", "label": "Browse Products",
     "action": "browse_products", "enabled": True, "position": 1},
    {"id": "place_order", "label": "Place an Order",
     "action": "place_order", "enabled": True, "position": 2},
    {"id": "my_order", "label": "My Order",
     "action": "my_order", "enabled": True, "position": 3},
    {"id": "delivery_collection", "label": "Delivery & Collection",
     "action": "delivery_collection", "enabled": True, "position": 4},
    {"id": "faqs", "label": "FAQs",
     "action": "faqs", "enabled": True, "position": 5},
    {"id": "handover", "label": "Talk to Staff",
     "action": "handover", "enabled": True, "position": 6},
)


@dataclass(frozen=True)
class MenuOption:
    id: str
    label: str
    action: str
    enabled: bool
    position: int


@dataclass(frozen=True)
class MenuConfig:
    enabled: bool
    welcome_message: str
    language: str
    options: tuple[MenuOption, ...]
    valid: bool = True


@dataclass(frozen=True)
class Selection:
    step: str
    arg: str | None


@dataclass(frozen=True)
class ParsedAction:
    selection: Selection | None
    expired: bool = False


@dataclass(frozen=True)
class MenuReply:
    """A customer-facing reply plus the native shape to try first.

    ``text`` is always sendable and is the fallback when interactive sending is
    unavailable. ``body`` is the shorter native body (the list/button UI already shows
    the rows), so the fallback text can be complete without duplicating it in the body.
    """

    text: str
    kind: str = "text"
    header: str | None = None
    body: str | None = None
    footer: str | None = None
    buttons: tuple[dict[str, str], ...] = ()
    button_text: str = "Choose"
    sections: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class MenuSendResult:
    """The outcome of one menu delivery attempt.

    ``uncertain`` means the provider may or may not have accepted the message; it must
    never be reported as sent and must never be retried automatically.
    """

    outcome: str
    wamid: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class MenuContext:
    settings: Settings
    tenant_id: str
    wa_id: str | None
    document: dict[str, Any]
    channel: dict[str, Any] | None = None
    phone_number_id: str | None = None
    customer: dict[str, Any] | None = None
    conversation: dict[str, Any] | None = None
    message_id: str | None = None


def is_enabled(document: dict[str, Any] | None) -> bool:
    """True only for the explicit ``configuration.menu.enabled: true`` opt-in."""
    if not isinstance(document, dict):
        return False
    configuration = document.get("configuration")
    if not isinstance(configuration, dict):
        return False
    menu = configuration.get("menu")
    return isinstance(menu, dict) and menu.get("enabled") is True


def with_menu_enabled(
    document: dict[str, Any] | None, enabled: bool = True
) -> dict[str, Any]:
    """Return a copy of the settings document with only ``menu.enabled`` changed.

    ``update_settings(configuration=...)`` replaces the whole JSONB document, so callers
    must never hand-build a partial one: doing so can erase ``knowledge_base`` or any
    other tenant configuration. This helper preserves every other key.
    """
    merged = copy.deepcopy(document if isinstance(document, dict) else {})
    configuration = merged.get("configuration")
    configuration = dict(configuration) if isinstance(configuration, dict) else {}
    menu = configuration.get("menu")
    menu = dict(menu) if isinstance(menu, dict) else {}
    menu["enabled"] = bool(enabled)
    configuration["menu"] = menu
    merged["configuration"] = configuration
    return merged


def set_enabled(settings: Settings, tenant_id: str, enabled: bool = True) -> dict[str, Any]:
    """Atomically merge ``menu.enabled`` into the tenant's settings document.

    Reading and writing happen in one tenant transaction, so a transient read failure
    cannot turn into a partial write that erases ``knowledge_base`` or other keys.
    """
    with tenant_admin(tenant_id, settings.app_database_url) as admin:
        document = admin.get_settings() or {}
        if not document:
            raise ValueError(
                "Tenant settings are not initialized; refusing to write a menu-only "
                "configuration that could erase other settings."
            )
        merged = with_menu_enabled(document, enabled)
        admin.update_settings(configuration=merged["configuration"])
    return merged


def normalize_menu_option_id(value: str) -> str:
    """Normalize a label-derived id to the persisted lowercase id alphabet.

    Explicit ids from stored documents are never passed through this helper: existing
    valid ids must remain byte-for-byte stable so outstanding taps keep working.
    """
    ascii_value = (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    normalized = re.sub(r"[^a-z0-9]+", "_", ascii_value)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized[:32].rstrip("_")


def menu_option_id_from_label(
    label: str, *, position: int, existing: set[str] | None = None
) -> str:
    """Build a unique, lowercase stable id when an operator leaves the id blank."""
    base = normalize_menu_option_id(label) or f"option_{max(1, int(position))}"
    base = base[:32].rstrip("_") or f"option_{max(1, int(position))}"
    used = existing or set()
    candidate = base
    suffix = 2
    while candidate in used:
        suffix_text = f"_{suffix}"
        stem = base[: 32 - len(suffix_text)].rstrip("_")
        candidate = f"{stem or 'option'}{suffix_text}"
        suffix += 1
    return candidate


def default_menu_options() -> tuple[MenuOption, ...]:
    """The historical six options used when a tenant has no options list."""
    return tuple(
        MenuOption(
            id=entry["id"],
            label=entry["label"],
            action=entry["action"],
            enabled=entry["enabled"],
            position=entry["position"],
        )
        for entry in DEFAULT_MENU_OPTIONS
    )


def _parse_option(raw: Any, *, default_position: int) -> MenuOption | None:
    """Parse one persisted option defensively; invalid options are never executed."""
    if not isinstance(raw, dict):
        return None
    option_id = str(raw.get("id") or raw.get("key") or "").strip()
    label = str(raw.get("label") or raw.get("title") or "").strip()
    action = str(raw.get("action") or raw.get("action_type") or "").strip()
    action = ACTION_ALIASES.get(action, action)
    raw_position = raw.get("position", raw.get("order", default_position))
    try:
        position = int(raw_position)
    except (TypeError, ValueError):
        return None
    raw_enabled = raw.get("enabled", True)
    if isinstance(raw_enabled, bool):
        enabled = raw_enabled
    else:
        enabled = str(raw_enabled).strip().lower() not in (
            "",
            "0",
            "false",
            "no",
            "off",
        )
    if (
        not MENU_OPTION_ID_RE.fullmatch(option_id)
        or not label
        or len(label) > MAX_MENU_LABEL
        or action not in MENU_ACTIONS
        or position < 1
        or position > MAX_MENU_OPTIONS
    ):
        return None
    return MenuOption(
        id=option_id,
        label=label,
        action=action,
        enabled=enabled,
        position=position,
    )


def menu_config(document: dict[str, Any] | None) -> MenuConfig:
    """Return a validated, backward-compatible view of ``configuration.menu``.

    Missing ``options`` means the historical six-option menu. An explicitly empty list
    means no options. Malformed options are retained only as an invalid configuration so
    runtime callers can return a safe error instead of executing an unknown action.
    """
    document = document if isinstance(document, dict) else {}
    configuration = document.get("configuration")
    configuration = configuration if isinstance(configuration, dict) else {}
    raw_menu = configuration.get("menu")
    raw_menu = raw_menu if isinstance(raw_menu, dict) else {}
    enabled = raw_menu.get("enabled") is True

    welcome = raw_menu.get("welcome_message", raw_menu.get("welcome"))
    welcome = (
        welcome.strip()
        if isinstance(welcome, str) and welcome.strip()
        else DEFAULT_WELCOME_MESSAGE
    )[:MAX_MENU_WELCOME]
    language = raw_menu.get("language")
    language = (
        language.strip()
        if isinstance(language, str) and language.strip()
        else DEFAULT_LANGUAGE
    )[:16]

    if not isinstance(raw_menu, dict) or "options" not in raw_menu:
        return MenuConfig(enabled, welcome, language, default_menu_options())

    raw_options = raw_menu.get("options")
    if not isinstance(raw_options, list):
        return MenuConfig(enabled, welcome, language, (), valid=False)

    options: list[MenuOption] = []
    seen_ids: set[str] = set()
    seen_positions: set[int] = set()
    valid = len(raw_options) <= MAX_MENU_OPTIONS
    for index, raw in enumerate(raw_options):
        option = _parse_option(raw, default_position=index + 1)
        if option is None or option.id in seen_ids or option.position in seen_positions:
            valid = False
            continue
        seen_ids.add(option.id)
        seen_positions.add(option.position)
        options.append(option)
    options.sort(key=lambda option: option.position)
    return MenuConfig(enabled, welcome, language, tuple(options), valid=valid)


def action_enabled(config: MenuConfig, step: str) -> bool:
    """True when at least one enabled configured option maps to an internal step."""
    return any(
        option.enabled and ACTION_STEPS.get(option.action) == step
        for option in config.options
    )


def make_option_action(option_id: str, *, now: float | None = None) -> str:
    """A stable action id for a configured option, independent of its label."""
    return make_action("option", option_id, now=now)


def load_document(settings: Settings, tenant_id: str) -> dict[str, Any]:
    """The tenant's settings document, or ``{}`` when unavailable.

    A failure is treated as "menu disabled": the caller falls back to the existing AI
    path rather than failing the webhook.
    """
    try:
        with tenant_admin(tenant_id, settings.app_database_url) as admin:
            return admin.get_settings() or {}
    except Exception:  # noqa: BLE001 - the menu must not break the webhook
        logger.exception("Could not load menu settings for tenant %s", tenant_id)
        return {}


def make_action(step: str, arg: str | None = None, *, now: float | None = None) -> str:
    expiry = int((time.time() if now is None else now) + MENU_TTL_SECONDS)
    return f"{ACTION_PREFIX}.{step}.{arg or '-'}.{expiry}"


def index_token(index: int, value: str) -> str:
    """A list position plus a short digest of what occupied it.

    A bare index would silently point at a different category after the catalogue is
    edited. The digest makes that tap detectably stale instead.
    """
    return f"{index}-{hashlib.sha1(value.encode('utf-8')).hexdigest()[:8]}"


def split_index_token(value: str | None) -> tuple[int, str | None]:
    match = re.fullmatch(r"(\d{1,3})(?:-([0-9a-f]{8}))?", value or "")
    if not match:
        return -1, None
    return int(match.group(1)), match.group(2)


def make_more(kind: str, context: str, offset: int, *, now: float | None = None) -> str:
    return make_action("more", f"{kind}_{context}_{offset}", now=now)


def split_more(value: str | None) -> tuple[str, str, int] | None:
    try:
        kind, rest = (value or "").split("_", 1)
        context, offset_text = rest.rsplit("_", 1)
        return kind, context, int(offset_text)
    except (ValueError, TypeError):
        return None


def _page(items: list[Any], page: int) -> tuple[list[Any], int | None]:
    """One page of at most ten rows, reserving the last row for "more" when needed."""
    offset = max(0, page) * PAGE_SIZE
    remaining = items[offset:]
    if len(remaining) <= MAX_LIST_ROWS:
        return remaining, None
    visible = remaining[: MAX_LIST_ROWS - 1]
    return visible, page + 1


def parse_action(value: Any, *, now: float | None = None) -> ParsedAction:
    """Validate an untrusted interactive id.

    ``ParsedAction(None, expired=False)`` means "not one of ours" and must be ignored.
    ``ParsedAction(None, expired=True)`` means it was ours but its window closed.
    """
    if not isinstance(value, str):
        return ParsedAction(None)
    parts = value.split(".")
    if len(parts) != 4 or parts[0] != ACTION_PREFIX:
        return ParsedAction(None)
    step, arg, expiry_text = parts[1], parts[2], parts[3]
    if step not in STEPS:
        return ParsedAction(None)
    if arg != "-" and not ARG_RE.fullmatch(arg):
        return ParsedAction(None)
    try:
        expiry = int(expiry_text)
    except ValueError:
        return ParsedAction(None)
    current = int(time.time() if now is None else now)
    if expiry < current - CLOCK_SKEW_SECONDS:
        return ParsedAction(None, expired=True)
    if expiry > current + MENU_TTL_SECONDS + CLOCK_SKEW_SECONDS:
        return ParsedAction(None)
    return ParsedAction(Selection(step, None if arg == "-" else arg))


def _text(text: str, *, header: str | None = None, footer: str | None = None) -> MenuReply:
    return MenuReply(text=text, header=header, footer=footer)


def _buttons(
    text: str,
    buttons: list[dict[str, str]],
    *,
    header: str | None = None,
    body: str | None = None,
    footer: str | None = None,
) -> MenuReply:
    return MenuReply(
        text=text,
        kind="buttons",
        header=header,
        body=body or text,
        footer=footer,
        buttons=tuple(buttons),
    )


def _list(
    text: str,
    sections: list[dict[str, Any]],
    *,
    header: str | None = None,
    body: str | None = None,
    footer: str | None = None,
    button_text: str = "Choose",
) -> MenuReply:
    return MenuReply(
        text=text,
        kind="list",
        header=header,
        body=body or text,
        footer=footer,
        sections=tuple(sections),
        button_text=button_text,
    )


def main_menu(
    document: dict[str, Any] | None = None, *, now: float | None = None
) -> MenuReply:
    """Build the configured main menu, or the historical default when none is saved."""
    config = menu_config(document)
    if not config.valid:
        return _text(
            "The saved menu configuration is invalid. Please ask the team to update it."
        )
    options = [option for option in config.options if option.enabled]
    if not options:
        return _text(
            f"{config.welcome_message}\n\nNo menu options are currently available."
        )
    rows = [
        {
            "id": make_option_action(option.id, now=now),
            "title": option.label,
            "description": OPTION_DESCRIPTIONS.get(option.action, "Choose this option"),
        }
        for option in options
    ]
    lines = [config.welcome_message]
    for position, option in enumerate(options, start=1):
        lines.append(f"{position}. {option.label}")
    lines.extend(
        [
            "",
            f"Reply with an option name (for example: {options[0].label}), or reply "
            "MENU for this list.",
        ]
    )
    return _list(
        "\n".join(lines),
        [{"title": "Menu", "rows": rows}],
        header="Main menu",
        body="Choose an option below.",
        footer="Reply MENU for this menu",
        button_text="Choose",
    )


def _bottom_buttons(*, include_browse: bool = False, include_faqs: bool = False) -> list[dict[str, str]]:
    """At most three escape hatches, in a stable order."""
    buttons: list[dict[str, str]] = []
    if include_browse:
        buttons.append({"id": make_action("browse"), "title": "Browse more"})
    if include_faqs:
        buttons.append({"id": make_action("faqs"), "title": "More FAQs"})
    buttons.append({"id": make_action("staff"), "title": "Talk to staff"})
    buttons.append({"id": make_action("main"), "title": "Main menu"})
    return buttons[:3]


def _configured_option(config: MenuConfig, text: str) -> MenuOption | None:
    normalized = text.strip().casefold()
    return next(
        (
            option
            for option in config.options
            if option.enabled and option.label.casefold() == normalized
        ),
        None,
    )


def handle_text(
    ctx: MenuContext, text: str, *, now: float | None = None
) -> MenuReply | None:
    """Route an exact command, configured label, or legacy alias."""
    value = (text or "").strip()
    lowered = value.casefold()
    config = menu_config(ctx.document)
    if lowered == "menu":
        return main_menu(ctx.document, now=now)
    if not config.valid:
        return _text("The saved menu configuration is invalid. Please ask us for help.")
    configured = _configured_option(config, value)
    if configured is not None:
        return _render_step(ctx, Selection(ACTION_STEPS[configured.action], None), now=now)
    match = ORDER_COMMAND_RE.fullmatch(value)
    if match:
        if not action_enabled(config, "my_order"):
            return _text("My Order is disabled in the current menu configuration.")
        return _order_status(ctx, match.group(1), now=now)
    step = OPTION_ALIASES.get(lowered)
    if step is None:
        return None
    if not action_enabled(config, step):
        return _text("That menu option is disabled in the current menu configuration.")
    return _render_step(ctx, Selection(step, None), now=now)


def could_be_configured_label(text: str) -> bool:
    """A deliberately cheap hint for the webhook before it reads tenant settings.

    Configured labels are constrained to WhatsApp row-title length. Requiring an
    upper-case first character keeps ordinary lowercase chat (and the old
    "no settings read for chat" contract) on the AI path; the authoritative check
    is still ``is_menu_command(text, document)`` after the settings are loaded.
    """
    value = (text or "").strip()
    return (
        0 < len(value) <= MAX_MENU_LABEL
        and "\n" not in value
        and value[:1].isupper()
    )


def is_menu_command(text: str, document: dict[str, Any] | None = None) -> bool:
    """Cheap pre-filter so ordinary chat never triggers a settings read."""
    value = (text or "").strip()
    lowered = value.casefold()
    if document is not None:
        config = menu_config(document)
        if config.valid and _configured_option(config, value) is not None:
            return True
    return (
        lowered == "menu"
        or bool(ORDER_COMMAND_RE.fullmatch(value))
        or lowered in OPTION_ALIASES
    )


def handle_action(
    ctx: MenuContext, action_id: Any, *, now: float | None = None
) -> MenuReply | None:
    """Route an interactive tap. Unknown ids return None; expired ids explain themselves."""
    parsed = parse_action(action_id, now=now)
    if parsed.expired:
        return _text("That menu option has expired. Reply MENU to start again.")
    if parsed.selection is None:
        return None
    return _render_step(ctx, parsed.selection, now=now)


def _render_option(
    ctx: MenuContext, option_id: str | None, *, now: float | None
) -> MenuReply:
    """Resolve a stable configured option id, never a display label."""
    config = menu_config(ctx.document)
    option = next(
        (entry for entry in config.options if entry.id == option_id),
        None,
    )
    if not config.valid or option is None or not option.enabled:
        return _text(
            "That menu option is no longer available. Reply MENU to start again."
        )
    return _render_step(ctx, Selection(ACTION_STEPS[option.action], None), now=now)


def _render_step(ctx: MenuContext, selection: Selection, *, now: float | None) -> MenuReply | None:
    step = selection.step
    if step == "main":
        return main_menu(ctx.document, now=now)
    if step == "option":
        return _render_option(ctx, selection.arg, now=now)
    if step == "browse":
        return _browse(ctx, now=now)
    if step == "cat":
        return _category(ctx, selection.arg, now=now)
    if step == "prod":
        return _product(ctx, selection.arg, now=now)
    if step == "order":
        return _place_order(now=now)
    if step == "my_order":
        return _my_order(now=now)
    if step == "delivery":
        return _delivery(ctx)
    if step == "faqs":
        return _faqs(ctx, now=now)
    if step == "faq":
        return _faq_answer(ctx, selection.arg, now=now)
    if step == "staff":
        return _staff(ctx)
    if step == "more":
        return _more(ctx, selection.arg, now=now)
    return None


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def _catalogue(ctx: MenuContext) -> list[dict[str, Any]] | None:
    """The tenant's catalogue, or None when it cannot be read."""
    try:
        with tenant_admin(ctx.tenant_id, ctx.settings.app_database_url) as admin:
            return admin.list_catalogue() or []
    except Exception:  # noqa: BLE001 - the customer gets an honest fallback
        logger.exception("Menu catalogue read failed for tenant %s", ctx.tenant_id)
        return None


def _active_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [product for product in products if product.get("is_active") is not False]


def _currency(ctx: MenuContext) -> str:
    value = ctx.document.get("currency")
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _price_summary(ctx: MenuContext, product: dict[str, Any]) -> str:
    prices = []
    for variant in product.get("variants") or []:
        if variant.get("is_active") is False or variant.get("price") is None:
            continue
        try:
            prices.append(Decimal(str(variant["price"])))
        except (InvalidOperation, TypeError, ValueError):
            continue
    if not prices:
        return "Ask us for the price"
    currency = _currency(ctx)
    return f"From {currency + ' ' if currency else ''}{min(prices):,.2f}"


def _product_rows(
    ctx: MenuContext, products: list[dict[str, Any]], *, now: float | None
) -> list[dict[str, Any]]:
    return [
        {
            "id": make_action("prod", str(product["id"]), now=now),
            "title": str(product.get("name") or "Product"),
            "description": _price_summary(ctx, product),
        }
        for product in products
    ]


def _rows_text(rows: list[dict[str, Any]]) -> str:
    """The readable version of a list, used when interactive sending fails."""
    lines = []
    for row in rows:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        description = str(row.get("description") or "").strip()
        lines.append(f"- {title}" + (f": {description}" if description else ""))
    return "\n".join(lines)


def _more_row(
    kind: str, context: str, offset: int, *, title: str, now: float | None = None
) -> dict[str, Any]:
    return {
        "id": make_more(kind, context, offset, now=now),
        "title": title,
        "description": "Show more",
    }


def _browse(ctx: MenuContext, *, page: int = 0, now: float | None = None) -> MenuReply:
    products = _catalogue(ctx)
    if products is None:
        return _buttons(
            "I can't load the catalogue right now. Please try again shortly.",
            _bottom_buttons(),
            header="Browse Products",
        )
    active = _active_products(products)
    if not active:
        return _buttons(
            "The catalogue is empty right now, so there is nothing to browse yet.",
            _bottom_buttons(),
            header="Browse Products",
        )

    real_categories: list[str] = []
    for product in active:
        category = str(product.get("category") or "").strip()
        if category and category not in real_categories:
            real_categories.append(category)
    real_categories.sort(key=str.lower)
    uncategorised = [
        product for product in active
        if not str(product.get("category") or "").strip()
    ]

    # Products without a category are reachable through "Other products" whenever a
    # categorised product exists. They used to disappear from the menu entirely.
    category_values = list(real_categories)
    if uncategorised and real_categories:
        category_values.append(OTHER_CATEGORY)

    if real_categories:
        visible, next_offset = _page(category_values, page)
        if not visible:
            return _buttons(
                "There are no more categories. Reply MENU for the main menu.",
                _bottom_buttons(include_browse=True),
                header="Browse Products",
            )
        rows: list[dict[str, Any]] = []
        for value in visible:
            if value == OTHER_CATEGORY:
                count = len(uncategorised)
                rows.append(
                    {
                        "id": make_action("cat", OTHER_CATEGORY, now=now),
                        "title": "Other products",
                        "description": f"{count} product{'s' if count != 1 else ''}",
                    }
                )
            else:
                index = real_categories.index(value)
                count = sum(
                    1 for product in active
                    if str(product.get("category") or "").strip() == value
                )
                rows.append(
                    {
                        "id": make_action("cat", index_token(index, value), now=now),
                        "title": value,
                        "description": f"{count} product{'s' if count != 1 else ''}",
                    }
                )
        if next_offset is not None:
            rows.append(
                _more_row("browse", "-", next_offset, title="More categories", now=now)
            )
        body = "Choose a category to see what is available."
        if page:
            body = "More categories."
        return _list(
            "Browse Products\n" + body + "\n\n" + _rows_text(rows),
            [{"title": "Categories", "rows": rows}],
            header="Browse Products",
            body=body,
            footer="Reply MENU for the main menu",
            button_text="Categories",
        )

    visible, next_offset = _page(active, page)
    if not visible:
        return _buttons(
            "There are no more products. Reply MENU for the main menu.",
            _bottom_buttons(include_browse=True),
            header="Browse Products",
        )
    rows = _product_rows(ctx, visible, now=now)
    if next_offset is not None:
        rows.append(_more_row("browse", "-", next_offset, title="More products", now=now))
    body = "Choose a product to see its details and prices."
    if page:
        body = "More products."
    return _list(
        "Browse Products\n" + body + "\n\n" + _rows_text(rows),
        [{"title": "Products", "rows": rows}],
        header="Browse Products",
        body=body,
        footer="Reply MENU for the main menu",
        button_text="Products",
    )


def _category(
    ctx: MenuContext, arg: str | None, *, page: int = 0, now: float | None = None
) -> MenuReply:
    products = _catalogue(ctx)
    if products is None:
        return _buttons(
            "I can't load the catalogue right now. Please try again shortly.",
            _bottom_buttons(),
        )
    active = _active_products(products)
    real_categories: list[str] = []
    for product in active:
        category = str(product.get("category") or "").strip()
        if category and category not in real_categories:
            real_categories.append(category)
    real_categories.sort(key=str.lower)

    if arg == OTHER_CATEGORY:
        category = "Other products"
        token = OTHER_CATEGORY
        in_category = [
            product for product in active
            if not str(product.get("category") or "").strip()
        ]
    else:
        index, expected = split_index_token(arg)
        if index < 0 or index >= len(real_categories):
            return _buttons(
                "That category is no longer available.",
                _bottom_buttons(include_browse=True),
                header="Browse Products",
            )
        category = real_categories[index]
        if expected and index_token(index, category) != f"{index}-{expected}":
            return _buttons(
                "That category has changed. Please browse the catalogue again.",
                _bottom_buttons(include_browse=True),
                header="Browse Products",
            )
        token = arg or index_token(index, category)
        in_category = [
            product for product in active
            if str(product.get("category") or "").strip() == category
        ]
    if not in_category:
        return _buttons(
            f"No products are listed under {category} right now.",
            _bottom_buttons(include_browse=True),
            header=category,
        )
    visible, next_offset = _page(in_category, page)
    if not visible:
        return _buttons(
            f"There are no more products in {category}.",
            _bottom_buttons(include_browse=True),
            header=category,
        )
    rows = _product_rows(ctx, visible, now=now)
    if next_offset is not None:
        rows.append(_more_row("prod", token, next_offset, title="More products", now=now))
    body = f"Choose a product in {category}."
    if page:
        body = f"More products in {category}."
    return _list(
        f"{category}\n{body}\n\n{_rows_text(rows)}",
        [{"title": category, "rows": rows}],
        header=category[:60],
        body=body,
        footer="Reply MENU for the main menu",
        button_text="Products",
    )


def _more(
    ctx: MenuContext, arg: str | None, *, now: float | None = None
) -> MenuReply:
    """Continue a paginated list. Context is re-validated by the target function."""
    parsed = split_more(arg)
    if parsed is None:
        return _text("That option is no longer available. Reply MENU to start again.")
    kind, context, offset = parsed
    if offset < 0 or offset > 10000:
        return _text("That option is no longer available. Reply MENU to start again.")
    if kind == "browse":
        return _browse(ctx, page=offset, now=now)
    if kind == "prod":
        return _category(ctx, None if context == "-" else context, page=offset, now=now)
    if kind == "faq":
        return _faqs(ctx, page=offset, now=now)
    return _text("That option is no longer available. Reply MENU to start again.")


def _product(ctx: MenuContext, product_id: str | None, *, now: float | None = None) -> MenuReply:
    if not product_id or not UUID_RE.fullmatch(product_id):
        return _buttons(
            "That product is no longer available.",
            _bottom_buttons(include_browse=True),
            header="Browse Products",
        )
    products = _catalogue(ctx)
    if products is None:
        return _buttons(
            "I can't load the catalogue right now. Please try again shortly.",
            _bottom_buttons(include_browse=True),
        )
    product = next(
        (item for item in _active_products(products) if str(item.get("id")) == product_id),
        None,
    )
    if product is None:
        return _buttons(
            "That product is no longer listed.",
            _bottom_buttons(include_browse=True),
            header="Browse Products",
        )
    name = str(product.get("name") or "Product")
    lines = [name]
    description = str(product.get("description") or "").strip()
    if description:
        lines.append(description[:500])
    variants = [
        variant for variant in (product.get("variants") or [])
        if variant.get("is_active") is not False
    ]
    if variants:
        lines.append("Options:")
        for variant in variants[:MAX_LIST_ROWS]:
            label = str(variant.get("name") or "Option")
            availability = AVAILABILITY_LABELS.get(
                str(variant.get("availability") or ""), ""
            )
            suffix = f" ({availability})" if availability else ""
            price = variant.get("price")
            if price is None:
                lines.append(f"- {label}{suffix}")
                continue
            try:
                amount = Decimal(str(price))
                currency = _currency(ctx)
                money = f"{currency + ' ' if currency else ''}{amount:,.2f}"
            except (InvalidOperation, TypeError, ValueError):
                money = "price on request"
            lines.append(f"- {label}: {money}{suffix}")
    else:
        lines.append("No options are listed for this product right now.")
    lines.append("")
    lines.append("Reply MENU for the main menu.")
    return _buttons(
        "\n".join(lines),
        _bottom_buttons(include_browse=True),
        header=name[:60],
    )


# ---------------------------------------------------------------------------
# Order, delivery, FAQs, staff
# ---------------------------------------------------------------------------

def _money(value: Any) -> str | None:
    try:
        return f"{Decimal(str(value)):,.2f}"
    except (InvalidOperation, TypeError, ValueError):
        return None


def _place_order(*, now: float | None = None) -> MenuReply:
    text = (
        "To place an order, tell us what you'd like and the quantity. We'll check "
        "availability and confirm the total with you before anything is paid."
    )
    return _buttons(
        text,
        [
            {"id": make_action("browse", now=now), "title": "Browse products"},
            {"id": make_action("staff", now=now), "title": "Talk to staff"},
            {"id": make_action("main", now=now), "title": "Main menu"},
        ],
        header="Place an Order",
    )


def _my_order(*, now: float | None = None) -> MenuReply:
    text = (
        "Reply with your order number, for example: order CB-1042.\n"
        "For your privacy, we only show orders placed with this WhatsApp number."
    )
    return _buttons(
        text,
        [
            {"id": make_action("staff", now=now), "title": "Talk to staff"},
            {"id": make_action("main", now=now), "title": "Main menu"},
        ],
        header="My Order",
    )


def _format_hours(value: Any) -> str:
    """Render configured hours only; never invent a schedule."""
    if isinstance(value, str):
        return value.strip()[:400]
    if isinstance(value, dict):
        parts = [f"{key}: {item}" for key, item in value.items() if str(item).strip()]
        return "; ".join(parts)[:400]
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return "; ".join(parts)[:400]
    return ""


def _delivery(ctx: MenuContext) -> MenuReply:
    rules = ctx.document.get("business_rules")
    rules = rules if isinstance(rules, dict) else {}
    currency = _currency(ctx)
    lines: list[str] = []
    fee = _money(rules.get("delivery_fee")) if rules.get("delivery_fee") is not None else None
    if fee is not None:
        lines.append(f"Delivery fee: {currency + ' ' if currency else ''}{fee}")
    hours = _format_hours(ctx.document.get("business_hours"))
    if hours:
        lines.append(f"Business hours: {hours}")
    if lines:
        text = (
            "Delivery & Collection\n"
            + "\n".join(lines)
            + "\n\nFor anything not shown here, please talk to our team."
        )
    else:
        text = (
            "Delivery and collection details aren't set up here yet. Our team can "
            "confirm them for you."
        )
    return _buttons(
        text,
        [
            {"id": make_action("staff"), "title": "Talk to staff"},
            {"id": make_action("main"), "title": "Main menu"},
        ],
        header="Delivery & Collection",
    )


def _faq_entries(document: dict[str, Any]) -> list[dict[str, str]]:
    configuration = document.get("configuration")
    configuration = configuration if isinstance(configuration, dict) else {}
    knowledge = configuration.get("knowledge_base")
    knowledge = knowledge if isinstance(knowledge, dict) else {}
    faqs = knowledge.get("faqs")
    entries: list[dict[str, str]] = []
    if isinstance(faqs, list):
        for item in faqs:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            answer = str(item.get("answer") or "").strip()
            if question:
                entries.append({"question": question, "answer": answer})
    return entries


def _faqs(ctx: MenuContext, *, page: int = 0, now: float | None = None) -> MenuReply:
    entries = _faq_entries(ctx.document)
    if not entries:
        return _buttons(
            "No FAQs are set up here yet. Ask us a question and our team will help.",
            _bottom_buttons(),
            header="FAQs",
        )
    visible, next_offset = _page(list(enumerate(entries)), page)
    if not visible:
        return _buttons(
            "There are no more questions. Reply MENU for the main menu.",
            _bottom_buttons(include_faqs=True),
            header="FAQs",
        )
    rows = [
        {
            "id": make_action("faq", index_token(index, entry["question"]), now=now),
            "title": entry["question"],
            "description": "Tap to read the answer",
        }
        for index, entry in visible
    ]
    if next_offset is not None:
        rows.append(_more_row("faq", "-", next_offset, title="More questions", now=now))
    body = "Choose a question to read its answer."
    if page:
        body = "More questions."
    return _list(
        "FAQs\n" + body + "\n\n" + _rows_text(rows),
        [{"title": "Frequently asked", "rows": rows}],
        header="FAQs",
        body=body,
        footer="Reply MENU for the main menu",
        button_text="Questions",
    )


def _faq_answer(ctx: MenuContext, arg: str | None, *, now: float | None = None) -> MenuReply:
    entries = _faq_entries(ctx.document)
    index, expected = split_index_token(arg)
    if index < 0 or index >= len(entries):
        return _buttons(
            "That question is no longer available.",
            _bottom_buttons(include_faqs=True),
            header="FAQs",
        )
    entry = entries[index]
    if expected and index_token(index, entry["question"]) != f"{index}-{expected}":
        return _buttons(
            "That question has changed. Please open the FAQs again.",
            _bottom_buttons(include_faqs=True),
            header="FAQs",
        )
    if not entry["answer"]:
        return _buttons(
            "That question doesn't have an answer recorded yet. Please talk to our team.",
            _bottom_buttons(include_faqs=True),
            header="FAQs",
        )
    text = f"{entry['question']}\n\n{entry['answer'][:3000]}"
    return _buttons(
        text,
        _bottom_buttons(include_faqs=True),
        header=entry["question"][:60],
    )


def _order_status(
    ctx: MenuContext, order_number: str, *, now: float | None = None
) -> MenuReply:
    """Order status, scoped to the sender's WhatsApp identity.

    A missing identity fails closed, and another customer's order number produces the
    same answer as a number that does not exist.
    """
    sender = (ctx.wa_id or "").strip()
    number = (order_number or "").strip()
    if not sender:
        return _buttons(
            "I can't verify this WhatsApp number, so I can't show order details here. "
            "Please talk to our team.",
            _bottom_buttons(),
            header="My Order",
        )
    try:
        with tenant_admin(ctx.tenant_id, ctx.settings.app_database_url) as admin:
            order = admin.get_order_by_number_for_whatsapp(number, sender)
            status = (
                admin.get_order_payment_status(str(order["id"])) if order else None
            )
    except Exception:  # noqa: BLE001 - never leak a database error to a customer
        logger.exception("Menu order lookup failed for tenant %s", ctx.tenant_id)
        return _buttons(
            "I couldn't look up that order just now. Please try again shortly.",
            _bottom_buttons(),
            header="My Order",
        )
    if not order or not status:
        return _buttons(
            "We couldn't find an order with that number for this WhatsApp number.",
            _bottom_buttons(),
            header="My Order",
        )
    currency = _currency(ctx)
    prefix = f"{currency} " if currency else ""
    total = _money(status.get("total")) or "not available"
    paid = _money(status.get("amount_paid")) or "not available"
    outstanding = _money(status.get("outstanding_balance")) or "not available"
    payment = str(status.get("payment_status") or "").replace("_", " ").strip().title()
    label = STATUS_LABELS.get(
        str(status.get("status") or ""),
        str(status.get("status") or "").replace("_", " ").title(),
    )
    lines = [
        f"Order {status.get('order_number') or number}",
        f"Status: {label}",
        f"Total: {prefix}{total}",
        f"Paid: {prefix}{paid}",
        f"Outstanding: {prefix}{outstanding}",
    ]
    if payment:
        lines.append(f"Payment: {payment}")
    return _buttons(
        "\n".join(lines),
        [
            {"id": make_action("staff", now=now), "title": "Talk to staff"},
            {"id": make_action("main", now=now), "title": "Main menu"},
        ],
        header="My Order",
    )


def _staff(ctx: MenuContext) -> MenuReply:
    """Record and notify a staff-assistance request through the existing handover path."""
    if not (
        ctx.channel
        and ctx.phone_number_id
        and ctx.wa_id
        and ctx.message_id
        and ctx.conversation
    ):
        return _text(
            "I can't arrange staff assistance right now. Please try again shortly."
        )
    try:
        _notified, text = handover_service.request_staff_assistance(
            ctx.settings,
            tenant_id=ctx.tenant_id,
            channel=ctx.channel,
            phone_number_id=ctx.phone_number_id,
            customer_key=ctx.wa_id,
            customer=ctx.customer,
            conversation=ctx.conversation,
            message_id=ctx.message_id,
            settings_document=ctx.document,
            business_name=str(ctx.document.get("business_name") or "our team"),
        )
    except Exception:  # noqa: BLE001 - the customer still gets an honest answer
        logger.exception("Menu staff handover failed for tenant %s", ctx.tenant_id)
        return _text(
            "I couldn't arrange staff assistance just now. Please try again shortly."
        )
    return _buttons(
        text,
        [{"id": make_action("main"), "title": "Main menu"}],
        header="Talk to Staff",
    )


