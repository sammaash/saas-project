"""Deterministic WhatsApp quotation workflow (portal redesign Stage 3).

The model no longer runs quotations. The webhook calls ``run`` for an AI-active
conversation of a tenant that has opted in (``configuration.quotations.enabled``) when
the customer explicitly asks for a quotation, or while a recent request is still being
collected. ``run`` decides the next step and returns a ``FlowStep``; the webhook owns
every send (through the dispatch boundary, so Stage 2's pause still applies).

Steps:

1. Read the request with the catalogue matcher (``saas.quotation_intent``). Only when
   the wording names something the matcher cannot read does one strict JSON extraction
   call run, and its product names are accepted only if they are exact catalogue names.
2. Persist the request (``quotation_briefs``) and let ``evaluate_brief`` resolve it
   against the live catalogue. Anything missing -> ask ONE question. Anything unlisted
   or unavailable -> staff handover (the AI is not paused for product handovers).
3. When complete and a customer name is known, ``create_quotation_from_brief`` issues
   the quotation (catalogue prices, snapshot lines, idempotency key = the inbound
   WhatsApp message id), and the PDF is rendered from the snapshot and stored once.

Nothing here states a price that ``create_quotation`` did not compute.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from saas import interpretation
from saas import quotation_intent as intent
from saas import quotations as saas_quotations
from saas.catalogue import CatalogueService

from ..config import Settings
from . import quotations as quotation_service

logger = logging.getLogger(__name__)

UNAVAILABLE_TEXT = (
    "Sorry, I can't prepare a quotation right now. Please try again in a little while, "
    "or reply \"talk to a person\" and a member of our team will help."
)
NAME_QUESTION = "Thanks. What name should I put on the quotation?"
#: Wording that adds to the request rather than replacing ("actually add a Vanilla too").
#: A bare "and" is deliberately absent: "and" joining two products already yields two
#: matched items (no replacement is attempted), while "make it Vanilla and 2 kg" adds a
#: detail to the one replacement product [review R1-F1].
_ADDITION_RE = re.compile(r"\b(?:add|also|too|plus|as\s+well|another|extra)\b", re.I)


@dataclass
class FlowStep:
    """What the webhook should do next.

    ``kind``:
      * ``ask`` -- send ``text`` (one question); nothing else happens.
      * ``quotation`` -- the quotation was issued; send the summary then the PDF.
      * ``staff`` -- record a product handover (``reason``/``request``) and send ``text``.
      * ``unavailable`` -- the catalogue or quotation store failed; send ``text``.
    """

    kind: str
    text: str
    quotation: dict[str, Any] | None = None
    pdf: bytes | None = None
    reason: str | None = None
    request: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _variants_text(product: Mapping[str, Any]) -> str:
    options = []
    for variant in product.get("variants") or []:
        weight = saas_quotations._weight_of(variant)  # noqa: SLF001 - shared helper
        options.append(f"{weight} kg" if weight else str(variant.get("name")))
    if len(options) <= 1:
        return "".join(options)
    return ", ".join(options[:-1]) + " or " + options[-1]


#: Room for the product list in the first question (Audit fix D10). WhatsApp allows 4096
#: characters per text; the list stops well short of that and says how many it left out.
_LISTING_BUDGET = 1500


def _product_listing(products: Sequence[Mapping[str, Any]]) -> str:
    """Every active product name (D10); the whole returned text is at most
    ``_LISTING_BUDGET`` characters, including its prefix and "and N more" suffix
    [review R1-F2]."""
    names = [str(p.get("name")) for p in products if p.get("name")]

    def render(count: int) -> str:
        rest = len(names) - count
        more = f" and {rest} more" if rest else ""
        return " We can quote for: " + ", ".join(names[:count]) + more + "."

    count = len(names)
    while count > 0 and len(render(count)) > _LISTING_BUDGET:
        count -= 1
    return render(count) if count else ""


def _question(evaluation: Mapping[str, Any], items: Sequence[Mapping[str, Any]],
              products: Sequence[Mapping[str, Any]]) -> str:
    """One deterministic question for the FIRST missing detail."""
    missing = list(evaluation.get("missing") or [])
    if not missing or missing == ["items"]:
        return ("Which product would you like a quotation for, and how many?"
                + _product_listing(products))
    first = missing[0]
    index = int(first.split("[", 1)[1].split("]", 1)[0]) - 1 if "[" in first else 0
    resolved = (evaluation.get("items") or [])[index] if index < len(
        evaluation.get("items") or []) else {}
    product = (resolved or {}).get("product")
    name = str((product or {}).get("name") or (items[index] if index < len(items) else {})
               .get("product_name") or "this item")
    if first.endswith(".product"):
        return str(evaluation.get("message") or f"Which product did you mean by {name}?")
    if first.endswith(".weight_kg"):
        return f"What size would you like for the {name}: {_variants_text(product or {})}?"
    if first.endswith(".variant"):
        return f"Which option of {name} would you like: {_variants_text(product or {})}?"
    if first.endswith(".quantity"):
        return f"How many {name} would you like?"
    return str(evaluation.get("message") or "Could you tell me a little more?")


def _apply_loose(items: list[dict[str, Any]], evaluation: Mapping[str, Any] | None,
                 *, weight: int | None, quantity: int | None, each: bool) -> bool:
    """Apply a size/quantity written without a product name. True when applied."""
    if not items or (weight is None and quantity is None):
        return False
    missing = list((evaluation or {}).get("missing") or [])

    def _targets(field_name: str) -> list[dict[str, Any]]:
        if each:
            return items
        for entry in missing:
            if entry.endswith("." + field_name) and "[" in entry:
                index = int(entry.split("[", 1)[1].split("]", 1)[0]) - 1
                if 0 <= index < len(items):
                    return [items[index]]
        # A correction ("make it 3kg") is unambiguous only with a single item.
        return items if len(items) == 1 else []

    applied = False
    if weight is not None:
        for item in _targets("weight_kg"):
            item["weight_kg"] = weight
            applied = True
    if quantity is not None:
        for item in _targets("quantity"):
            item["quantity"] = quantity
            applied = True
    return applied


def _pending(evaluation: Mapping[str, Any] | None) -> tuple[int, str] | None:
    """``(item index, field)`` of the question the customer was last asked, if any.

    ``_question`` always asks about the first missing detail, so that is the pending one.
    """
    missing = list((evaluation or {}).get("missing") or [])
    if not missing or "[" not in missing[0]:
        return None
    first = missing[0]
    try:
        index = int(first.split("[", 1)[1].split("]", 1)[0]) - 1
    except ValueError:
        return None
    return index, first.rsplit(".", 1)[-1]


def _answer_pending_option(items: list[dict[str, Any]], evaluation: Mapping[str, Any] | None,
                           number: int | None) -> bool:
    """Audit fix D3: a bare number answers a pending size/option question.

    After "Which option of Black Forest would you like: 1 kg, 2 kg or 3 kg?", the reply
    "1" means that size (or the first option), not "change the quantity to 1". A number
    that equals one of the offered sizes selects it; otherwise a number within the list
    selects that position. Anything else is left to the quantity reading as before.
    """
    pending = _pending(evaluation)
    if number is None or pending is None:
        return False
    index, field_name = pending
    if field_name not in ("weight_kg", "variant") or not 0 <= index < len(items):
        return False
    resolved = list((evaluation or {}).get("items") or [])
    product = (resolved[index] or {}).get("product") if index < len(resolved) else None
    variants = [v for v in (product or {}).get("variants") or [] if isinstance(v, dict)]
    if not variants:
        return False
    sizes = [saas_quotations._weight_of(v) for v in variants]  # noqa: SLF001
    item = items[index]
    if str(number) in sizes:
        item["weight_kg"] = number
        item["variant"] = None
        return True
    if 1 <= number <= len(variants):
        size = sizes[number - 1]
        if size is not None and size.isdigit():
            item["weight_kg"] = int(size)
            item["variant"] = None
        else:
            item["weight_kg"] = None
            item["variant"] = str(variants[number - 1].get("name") or "")[:100] or None
        return item["weight_kg"] is not None or item["variant"] is not None
    return False


def _replace_single(items: list[dict[str, Any]], new: Mapping[str, Any]) -> bool:
    """Audit fix D2: "Actually make it Vanilla" replaces the one open item.

    Only when exactly one item is open and the customer names exactly one different
    product in correction wording. The quantity and size carry over unless the message
    states new ones; a chosen option does not (it belonged to the old product).
    """
    if len(items) != 1:
        return False
    old = items[0]
    if (saas_quotations._normalize_name(old.get("product_name"))  # noqa: SLF001
            == saas_quotations._normalize_name(new.get("product_name"))):  # noqa: SLF001
        return False
    items[0] = {
        "product_name": new.get("product_name"),
        "weight_kg": new.get("weight_kg") if new.get("weight_kg") is not None
        else old.get("weight_kg"),
        "quantity": new.get("quantity") if new.get("quantity") is not None
        else old.get("quantity"),
        "variant": new.get("variant"),
    }
    return True


def _recent_items(settings: Settings, tenant_id: str,
                  conversation_id: str) -> list[dict[str, Any]]:
    """Items of this conversation's latest request if it was recent (Audit fix D4).

    Used only when the customer refers back to it ("3 such cakes") with no request open.
    """
    try:
        row = saas_quotations.load_brief(settings, tenant_id, conversation_id)
    except Exception as error:  # noqa: BLE001 - context is optional
        logger.info("Quotation request lookup failed tenant=%s error=%s",
                    tenant_id, type(error).__name__)
        return []
    if not row:
        return []
    updated = row.get("updated_at")
    if updated is not None:
        from datetime import datetime, timedelta, timezone  # noqa: PLC0415

        if updated < datetime.now(timezone.utc) - timedelta(
                hours=saas_quotations.BRIEF_MAX_AGE_HOURS):
            return []
    items = []
    for item in (row.get("brief") or {}).get("items") or []:
        if isinstance(item, dict) and item.get("product_name") and (
                item.get("weight_kg") is not None or item.get("variant")
                or item.get("quantity") is not None):
            items.append({"product_name": item.get("product_name"),
                          "weight_kg": item.get("weight_kg"),
                          "quantity": item.get("quantity"),
                          "variant": item.get("variant")})
    return items


def _merge(items: list[dict[str, Any]], found: Sequence[Mapping[str, Any]]) -> None:
    for new in found:
        target = next(
            (i for i in items
             if saas_quotations._normalize_name(i.get("product_name"))  # noqa: SLF001
             == saas_quotations._normalize_name(new.get("product_name"))),  # noqa: SLF001
            None,
        )
        if target is None:
            target = {"product_name": new.get("product_name"), "weight_kg": None,
                      "quantity": None, "variant": None}
            items.append(target)
        for key in ("weight_kg", "quantity", "variant"):
            if new.get(key) is not None:
                target[key] = new.get(key)


def _extract(provider: Any, text: str, products: Sequence[Mapping[str, Any]],
             open_items: Sequence[Mapping[str, Any]] = ()):
    """The single strict extraction call. None on any failure (then we ask).

    Audit fix D4: the request collected so far is given to the call, so a follow-up such
    as "just one" or "such cakes" can be read against it instead of in isolation.
    """
    if provider is None:
        return None
    import json  # noqa: PLC0415

    from saas.bot import BotResponseRequest  # noqa: PLC0415

    system = intent.extraction_prompt(products)
    if open_items:
        summary = [{k: i.get(k) for k in ("product_name", "weight_kg", "quantity", "variant")}
                   for i in open_items[:10]]
        system += (
            "\nOPEN REQUEST (already collected in this conversation; a follow-up message "
            "usually changes or completes it, and words such as 'one', 'such' or 'them' "
            "refer to it -- never put them in \"unlisted\"): "
            + json.dumps(summary, ensure_ascii=True)
        )
    try:
        raw = provider.generate(
            BotResponseRequest(
                system_prompt=system,
                history=(),
                customer_message=text[:1000],
                channel="whatsapp",
                safe_test_mode=False,
                messages=(
                    {"role": "system", "content": system},
                    {"role": "user", "content": text[:1000]},
                ),
            )
        )
    except Exception as error:  # noqa: BLE001 - extraction is optional
        logger.warning("Quotation extraction call failed error=%s", type(error).__name__)
        return None
    parsed = intent.parse_extraction(str(raw or ""), products)
    if parsed is None:
        logger.warning("Quotation extraction returned an invalid shape; asking instead")
    return parsed


def applies(text: str, brief: Mapping[str, Any] | None) -> bool:
    """Should this message enter the quotation flow at all?"""
    return intent.is_quote_request(text) or brief is not None


def run(
    settings: Settings,
    *,
    tenant_id: str,
    conversation_id: str,
    customer_key: str,
    customer_name: str | None,
    message_id: str,
    text: str,
    provider: Any = None,
    brief: Mapping[str, Any] | None = None,
) -> FlowStep | None:
    """Advance the conversation's quotation request by one customer message.

    Returns None when the message is not about the request (the AI answers it and the
    request stays open). ``brief`` is the active request (``active_brief``), if any.
    """
    explicit = intent.is_quote_request(text)
    try:
        products = CatalogueService(tenant_id, settings.app_database_url).list_active_products()
    except Exception as error:  # noqa: BLE001 - never price without the catalogue
        logger.error("Quotation catalogue read failed tenant=%s error=%s",
                     tenant_id, type(error).__name__)
        return FlowStep("unavailable", UNAVAILABLE_TEXT)

    stored = dict((brief or {}).get("brief") or {})
    items: list[dict[str, Any]] = [dict(i) for i in stored.get("items") or []
                                   if isinstance(i, dict)]
    # A name the customer typed is kept on the request; a profile/record name is not.
    typed_name = stored.get("customer_name")
    awaiting_name = bool(brief) and (brief or {}).get("status") == "ready" and not (
        typed_name or customer_name)

    previous = None
    if items:
        try:
            previous = saas_quotations.evaluate_brief(settings, tenant_id, {"items": items})
        except saas_quotations.QuotationError:
            previous = None

    match = intent.match_items(text, products)
    progressed = False

    # Audit fix D4: "I wanted a quotation of 3 such cakes" with no request open refers to
    # the request just quoted; start from its items instead of from nothing.
    if (not items and not match.items
            and interpretation.referential_quantity(text) is not None):
        items = _recent_items(settings, tenant_id, conversation_id)
        if items:
            try:
                previous = saas_quotations.evaluate_brief(settings, tenant_id,
                                                          {"items": items})
            except saas_quotations.QuotationError:
                previous = None

    if awaiting_name and not match.has_details:
        typed = intent.looks_like_name(text, products)
        if typed:
            typed_name = typed
            progressed = True

    # Audit fix D3: a bare number answers the pending size/option question.
    if (not match.items and match.loose_weight_kg is None
            and _answer_pending_option(items, previous, interpretation.bare_number(text))):
        match.loose_quantity = None
        progressed = True

    if match.items:
        # Audit fix D2: a correction naming one new product replaces the open item.
        if not (len(match.items) == 1 and interpretation.is_correction(text)
                and not _ADDITION_RE.search(text)
                and _replace_single(items, match.items[0])):
            _merge(items, match.items)
        progressed = True
    if _apply_loose(items, previous, weight=match.loose_weight_kg,
                    quantity=match.loose_quantity, each=match.applies_to_each):
        progressed = True
    # Audit fix D4: "Just one" / "3 such cakes" changes the quantity of the one open item.
    if (not match.items and match.loose_quantity is None and len(items) == 1):
        referential = interpretation.referential_quantity(text)
        if referential is not None:
            items[0]["quantity"] = referential
            progressed = True

    unlisted: list[str] = []
    if explicit and not match.items and intent.mentions_unknown_item(text):
        extraction = _extract(provider, text, products, items)
        if extraction is not None:
            if extraction.items:
                _merge(items, extraction.items)
                progressed = True
            # Audit fix D4: a fragment of the message is never an unlisted product.
            unlisted = [u for u in extraction.unlisted if not intent.is_fragment(u)]

    if not explicit and not progressed:
        # Not about the open request: let the AI answer; the request stays open.
        return None

    if unlisted:
        request = ", ".join(unlisted)[:200]
        # Audit fix D5: the valid part of the request stays open (resumable) while staff
        # handle the unlisted part; with nothing valid it is closed as before.
        resumable = bool(items)
        missing: list[str] = []
        if resumable:
            try:
                missing = list(saas_quotations.evaluate_brief(
                    settings, tenant_id, {"items": items}).get("missing") or [])
            except saas_quotations.QuotationError:
                missing = ["items"]
        try:
            saas_quotations.save_brief(
                settings, tenant_id, conversation_id, customer_key,
                {"items": items if resumable else [{"product_name": u} for u in unlisted],
                 **({"customer_name": typed_name} if typed_name else {})},
                status="collecting" if resumable else "handed_over", missing=missing,
            )
        except Exception as error:  # noqa: BLE001 - the handover itself still happens
            logger.error("Quotation request save failed tenant=%s error=%s",
                         tenant_id, type(error).__name__)
        return FlowStep(
            "staff",
            f"Thanks. {request[:1].upper() + request[1:]} is not on our standard menu, "
            "so I've passed your request to our team. They will confirm the price with "
            "you here.",
            reason="unlisted_product",
            request=f"Quotation request: {request}",
            details={"source": "quotation_flow", "items": items, "unlisted": unlisted},
        )

    try:
        evaluation = saas_quotations.evaluate_brief(settings, tenant_id, {"items": items})
    except saas_quotations.QuotationError as error:
        logger.error("Quotation evaluation failed tenant=%s error=%s", tenant_id, error)
        return FlowStep("unavailable", UNAVAILABLE_TEXT)

    name = typed_name or customer_name
    document = {"items": items, **({"customer_name": typed_name} if typed_name else {})}
    if evaluation["needs_staff"]:
        request = ", ".join(str(i.get("product_name")) for i in items if i.get("product_name"))
        try:
            saas_quotations.save_brief(settings, tenant_id, conversation_id, customer_key,
                                       document, status="handed_over", missing=[])
        except Exception as error:  # noqa: BLE001
            logger.error("Quotation request save failed tenant=%s error=%s",
                         tenant_id, type(error).__name__)
        return FlowStep(
            "staff",
            f"Thanks. {evaluation.get('message') or 'Our team needs to price this request.'} "
            "I've passed it to our team and they will confirm the price with you here.",
            reason=evaluation.get("reason") or "custom_order",
            request=f"Quotation request: {request or text[:150]}",
            details={"source": "quotation_flow", "items": items},
        )

    if evaluation["missing"] or match.unreadable_size:
        try:
            saas_quotations.save_brief(settings, tenant_id, conversation_id, customer_key,
                                       document, status="collecting",
                                       missing=evaluation["missing"] or ["size"])
        except Exception as error:  # noqa: BLE001
            logger.error("Quotation request save failed tenant=%s error=%s",
                         tenant_id, type(error).__name__)
            return FlowStep("unavailable", UNAVAILABLE_TEXT)
        if match.unreadable_size and not evaluation["missing"]:
            product = (evaluation["items"][0] or {}).get("product") or {}
            return FlowStep(
                "ask",
                f"We quote whole sizes only. Which size would you like for the "
                f"{product.get('name') or 'cake'}: {_variants_text(product)}?",
            )
        return FlowStep("ask", _question(evaluation, items, products))

    if not name:
        try:
            saas_quotations.save_brief(settings, tenant_id, conversation_id, customer_key,
                                       document, status="ready", missing=[])
        except Exception as error:  # noqa: BLE001
            logger.error("Quotation request save failed tenant=%s error=%s",
                         tenant_id, type(error).__name__)
            return FlowStep("unavailable", UNAVAILABLE_TEXT)
        return FlowStep("ask", NAME_QUESTION)

    try:
        saas_quotations.save_brief(settings, tenant_id, conversation_id, customer_key,
                                   document, status="ready",
                                   missing=[])
        quotation = saas_quotations.create_quotation_from_brief(
            settings,
            tenant_id,
            conversation_id=conversation_id,
            customer_wa_id=customer_key,
            customer_name=name,
            message_id=message_id,
        )
    except saas_quotations.QuotationNeedsStaff as error:
        return FlowStep(
            "staff",
            f"Thanks. {error} I've passed it to our team and they will confirm the price "
            "with you here.",
            reason=error.reason,
            request=f"Quotation request: {text[:150]}",
            details={"source": "quotation_flow", "items": items},
        )
    except saas_quotations.QuotationConflictError:
        logger.error("Quotation idempotency conflict tenant=%s", tenant_id)
        return FlowStep("unavailable", UNAVAILABLE_TEXT)
    except saas_quotations.QuotationError as error:
        # A user-correctable catalogue problem (e.g. that size is out of stock). The
        # request stays open so the customer can change it.
        try:
            saas_quotations.save_brief(settings, tenant_id, conversation_id, customer_key,
                                       document, status="collecting", missing=["items"])
        except Exception:  # noqa: BLE001
            pass
        return FlowStep("ask", f"{error} Would you like a different size or product?")
    except Exception as error:  # noqa: BLE001
        logger.error("Quotation creation failed tenant=%s error=%s",
                     tenant_id, type(error).__name__)
        return FlowStep("unavailable", UNAVAILABLE_TEXT)

    pdf = None
    try:
        pdf = quotation_service.store_pdf(settings, tenant_id, quotation)
    except Exception as error:  # noqa: BLE001 - generation succeeded; sending will fail honestly
        logger.error("Quotation PDF render/store failed tenant=%s quotation=%s error=%s",
                     tenant_id, quotation.get("quotation_number"), type(error).__name__)
    return FlowStep(
        "quotation",
        saas_quotations.format_customer_summary(quotation),
        quotation=quotation,
        pdf=pdf,
    )


def has_active_request(settings: Settings, tenant_id: str, conversation_id: str | None):
    try:
        return saas_quotations.active_brief(settings, tenant_id, conversation_id)
    except Exception as error:  # noqa: BLE001 - treat as no request
        logger.info("Quotation request lookup failed tenant=%s error=%s",
                    tenant_id, type(error).__name__)
        return None
