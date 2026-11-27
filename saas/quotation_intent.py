"""Deterministic reading of quotation-related customer messages.

Nothing here prices anything. It answers three questions for the webhook:

* ``classify`` -- is this a discount/special-pricing request, an explicit quotation
  request, a delivery enquiry, an ordinary price enquiry, or none of these?
* ``match_items`` -- which products, sizes and quantities from *this tenant's* active
  catalogue does the text name? (primary extraction method)
* ``parse_extraction`` -- validate the JSON returned by the single strict extraction
  call used only when the catalogue matcher cannot read the wording. Any product name
  that is not an exact catalogue name is reported as unlisted, never guessed.

Precedence (``classify``): discount > quotation > delivery > price. "Can I get a discount
on the quote?" is a negotiation and goes to staff; "how much is a 2kg cake?" is an
ordinary price enquiry and never creates a quotation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import interpretation, variant_resolver

DISCOUNT = "discount"
QUOTE = "quote"
DELIVERY = "delivery"
PRICE = "price"

_DISCOUNT_RE = re.compile(
    r"\b(?:discount\w*|disc(?:ount)?\b|offer\s+(?:me\s+)?(?:a\s+)?(?:better|lower|special)|"
    r"(?:better|best|lower|cheaper|special|reduced|negotiable|wholesale|bulk)\s+"
    r"(?:price|pricing|rate|deal|offer)s?|"
    r"(?:reduce|lower|drop|bring\s+down|cut)\s+(?:the\s+)?(?:price|cost|total|amount)|"
    r"negotiat\w*|bargain\w*|promo(?:tion)?\s*(?:code)?|coupon|"
    r"(?:too|very|so)\s+(?:expensive|pricey|costly)|"
    r"can\s+you\s+do\s+(?:it\s+)?(?:for|at)\s+(?:ksh|kes|sh)?\.?\s*\d+|"
    r"punguz\w*|bei\s+(?:ya\s+)?chini)\b",
    re.I,
)

_QUOTE_RE = re.compile(
    r"\b(?:quot(?:e|es|ation|ations)|quotation|estimate\s+for|proforma|pro[\s-]?forma)\b",
    re.I,
)

# "send me the quotation pdf again" is a document request, handled by its own path.
_DOC_ONLY_RE = re.compile(
    r"\b(?:send|resend|share|forward|get|download|need)\b[^.?!]{0,40}"
    r"\b(?:quotation|quote)\b[^.?!]{0,20}\b(?:pdf|document|copy|file)\b"
    r"|\b(?:quotation|quote)\s+(?:pdf|document)\b",
    re.I,
)

_DELIVERY_RE = re.compile(
    r"\b(?:deliver\w*|shipping|ship\s+to|transport(?:ation)?\s+(?:to|fee|cost|charge)|"
    r"courier|drop[\s-]?off|bring\s+it\s+to|send\s+it\s+to|"
    r"(?:delivery|shipping)\s+(?:fee|cost|charge)s?|kuleta|kufikisha)\b",
    re.I,
)

_PRICE_RE = re.compile(
    r"\b(?:how\s+much|price|prices|pricing|cost|costs|charge|rate|bei|ngapi|kiasi\s+gani)\b",
    re.I,
)

_EACH_RE = re.compile(r"\b(?:each|both|all\s+of\s+them|every\s+one)\b", re.I)

# Shared grammar (S2, D8): sizes in either order and number words live in
# saas.interpretation; this module keeps its matcher and delegates to it.
_NUMBER_WORDS = interpretation.NUMBER_WORDS
_QUANTITY_TOKEN = r"(\d{1,4}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
_QUANTITY_BEFORE_RE = re.compile(
    r"(?:^|[\s,(])" + _QUANTITY_TOKEN + r"\s+(?:x\s+)?(?:(?:of\s+)?(?:the\s+)?)$", re.I
)
_QUANTITY_X_RE = re.compile(r"(?:\bx\s*(\d{1,4})\b|\b(\d{1,4})\s*x\b)", re.I)
_QUANTITY_PIECES_RE = re.compile(
    r"\b(\d{1,4})\s*(?:pcs|pieces|units|cakes|items|of\s+them)\b", re.I
)
_BARE_QUANTITY_RE = re.compile(
    r"^\s*(?:make\s+it\s+|just\s+|only\s+)?" + _QUANTITY_TOKEN
    + r"\s*(?:please|pls|thanks|thank\s+you|cakes|pieces|pcs|units|of\s+them)?\s*[.!]?\s*$",
    re.I,
)

# Words that describe the product family rather than a specific product. "2 chocolate
# cakes" must match a product named "Chocolate" as well as one named "Chocolate Cake".
_GENERIC_WORDS = frozenset(
    {"cake", "cakes", "the", "a", "an", "of", "and", "with", "flavour", "flavor",
     "flavoured", "flavored"}
)


@dataclass
class MatchResult:
    """What the catalogue matcher could read from one message."""

    items: list[dict[str, Any]] = field(default_factory=list)
    #: A size or quantity was present without any product name.
    loose_weight_kg: int | None = None
    loose_quantity: int | None = None
    applies_to_each: bool = False
    #: The text mentions a size that is not a whole kilogram (e.g. half a kilo).
    unreadable_size: bool = False

    @property
    def has_details(self) -> bool:
        return bool(self.items) or self.loose_weight_kg is not None or (
            self.loose_quantity is not None
        )


def _normalize(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_discount_request(text: str) -> bool:
    return bool(_DISCOUNT_RE.search(text or ""))


def is_quote_request(text: str) -> bool:
    value = text or ""
    return bool(_QUOTE_RE.search(value)) and not _DOC_ONLY_RE.search(value)


def is_delivery_enquiry(text: str) -> bool:
    return bool(_DELIVERY_RE.search(text or ""))


def is_price_enquiry(text: str) -> bool:
    return bool(_PRICE_RE.search(text or ""))


def classify(text: str) -> str | None:
    """``discount`` > ``quote`` > ``delivery`` > ``price`` > None."""
    if is_discount_request(text):
        return DISCOUNT
    if is_quote_request(text):
        return QUOTE
    if is_delivery_enquiry(text):
        return DELIVERY
    if is_price_enquiry(text):
        return PRICE
    return None


def _core_tokens(name: Any) -> tuple[str, ...]:
    tokens = tuple(t for t in _normalize(name).split() if t not in _GENERIC_WORDS)
    return tokens or tuple(_normalize(name).split())


def _token_pattern(tokens: Sequence[str]) -> re.Pattern[str]:
    parts = [re.escape(t) + r"(?:s|es)?" for t in tokens]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b")


def _quantity_value(token: str | None) -> int | None:
    if not token:
        return None
    token = token.lower()
    if token.isdigit():
        value = int(token)
        return value if 1 <= value <= 10000 else None
    return _NUMBER_WORDS.get(token)


def _weight_value(text: str) -> tuple[int | None, bool]:
    """(whole-kilogram weight, unreadable) from the first size mention in ``text``.

    "2 kg", "two kilos", "kilo 2" (S2, D8); "half a kilo" is unreadable.
    """
    return interpretation.size_value(text)


def _strip_weights(text: str) -> str:
    return interpretation.strip_sizes(text)


def _quantity_near(segment: str, start: int) -> int | None:
    """A quantity written just before the product name, or as 'x2'/'2 pieces'."""
    before = _strip_weights(segment[:start]).rstrip()
    match = _QUANTITY_BEFORE_RE.search(before + " ")
    if match:
        return _quantity_value(match.group(1))
    # "2 x Chocolate" / "Chocolate x2" / "3 pieces"
    for pattern in (_QUANTITY_X_RE, _QUANTITY_PIECES_RE):
        found = pattern.search(_strip_weights(segment))
        if found:
            return _quantity_value(next(g for g in found.groups() if g))
    return None


def _segments(text: str) -> list[str]:
    return [part for part in re.split(r"\s*(?:,|;|\band\b|&|\bplus\b|\n)\s*", text) if part]


def match_items(text: str, products: Sequence[Mapping[str, Any]]) -> MatchResult:
    """Read products, whole-kg sizes and quantities from ``text``.

    Only the tenant's own active products are considered. A product is matched by its
    distinctive name words (generic words such as "cake" are ignored); when two products
    share the same words (e.g. "Forest") only the full distinctive name counts.
    """
    result = MatchResult()
    value = text or ""
    names = [(p, _core_tokens(p.get("name"))) for p in products if p.get("name")]
    # Longest names first so "White Forest" wins over a hypothetical "Forest".
    names.sort(key=lambda item: len(item[1]), reverse=True)

    for segment in _segments(value):
        normalized = _normalize(segment)
        claimed: list[tuple[int, int]] = []
        found: list[tuple[int, Mapping[str, Any]]] = []
        for product, tokens in names:
            if not tokens:
                continue
            match = _token_pattern(tokens).search(normalized)
            if not match:
                continue
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in claimed):
                continue
            claimed.append(span)
            found.append((span[0], product))
        found.sort(key=lambda item: item[0])
        weight, unreadable = _weight_value(segment)
        result.unreadable_size = result.unreadable_size or unreadable
        for position, product in found:
            if len(found) == 1:
                quantity = _quantity_near(normalized, position)
                if quantity is None:
                    # "Change the Vanilla quantity to two" (S2, D8).
                    quantity = interpretation.correction_quantity(normalized)
            else:
                # Several products in one clause: only a number written directly before
                # each name counts ("2 vanilla 1 chocolate").
                before = _strip_weights(normalized[:position]).rstrip() + " "
                written = _QUANTITY_BEFORE_RE.search(before)
                quantity = _quantity_value(written.group(1)) if written else None
            result.items.append(
                {
                    "product_name": str(product.get("name")),
                    "weight_kg": weight if len(found) == 1 else None,
                    "quantity": quantity,
                }
            )
        if not found:
            if weight is not None and result.loose_weight_kg is None:
                result.loose_weight_kg = weight
            bare = _BARE_QUANTITY_RE.match(_strip_weights(segment))
            if bare and result.loose_quantity is None:
                result.loose_quantity = _quantity_value(bare.group(1))
            else:
                pieces = _QUANTITY_PIECES_RE.search(_strip_weights(segment))
                if pieces and result.loose_quantity is None:
                    result.loose_quantity = _quantity_value(pieces.group(1))
                elif result.loose_quantity is None:
                    # "Change the quantity to two" / "qty 3" (S2, D8).
                    result.loose_quantity = interpretation.correction_quantity(segment)

    # One product named and a size/quantity given elsewhere in the message
    # ("2 chocolate cakes, 1kg each") belongs to that product.
    if len(result.items) == 1:
        item = result.items[0]
        if item["weight_kg"] is None and result.loose_weight_kg is not None:
            item["weight_kg"] = result.loose_weight_kg
            result.loose_weight_kg = None
        if item["quantity"] is None and result.loose_quantity is not None:
            item["quantity"] = result.loose_quantity
            result.loose_quantity = None
    result.applies_to_each = bool(_EACH_RE.search(value))
    if result.applies_to_each and result.loose_weight_kg is None and len(result.items) > 1:
        weights = {i["weight_kg"] for i in result.items if i["weight_kg"] is not None}
        if len(weights) == 1:
            shared = weights.pop()
            for item in result.items:
                item["weight_kg"] = item["weight_kg"] or shared
    return result


def looks_like_name(text: str, products: Sequence[Mapping[str, Any]]) -> str | None:
    """A plausible personal name typed in reply to "What name should I put on it?"."""
    value = re.sub(r"^(?:my\s+name\s+is|it'?s|i\s+am|i'm|name\s*:)\s+", "", (text or "").strip(),
                   flags=re.I).strip(" .!")
    if not 2 <= len(value) <= 80 or any(ch.isdigit() for ch in value):
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z' .-]*", value) or len(value.split()) > 5:
        return None
    if match_items(value, products).items or classify(value):
        return None
    return value


# ---------------------------------------------------------------------------
# Strict extraction (used only when the matcher cannot read the wording)
# ---------------------------------------------------------------------------
EXTRACTION_SYSTEM_PROMPT = (
    "You extract a customer's quotation request into JSON. You never price anything.\n"
    "Return ONLY one JSON object, no prose, exactly this shape:\n"
    '{"items": [{"product_name": string, "weight_kg": integer or null, '
    '"variant": string or null, "quantity": integer or null}], '
    '"unlisted": [string]}\n'
    "Rules: product_name must be copied EXACTLY from the CATALOGUE list below. If the "
    "customer asks for something not in the list, put their words in \"unlisted\" "
    "instead. Use null for any size, option or quantity the customer did not state "
    "clearly; never guess or assume a default. weight_kg is a whole number of kilograms.\n"
)


def extraction_prompt(products: Sequence[Mapping[str, Any]]) -> str:
    catalogue = []
    for product in products:
        options = []
        for variant in product.get("variants") or []:
            weight = variant_resolver.size_label(variant_resolver.variant_size_kg(variant))
            options.append(f"{weight}kg" if weight is not None else str(variant.get("name")))
        catalogue.append({"product_name": product.get("name"), "options": options})
    return EXTRACTION_SYSTEM_PROMPT + "CATALOGUE: " + json.dumps(catalogue, ensure_ascii=True)


@dataclass
class Extraction:
    items: list[dict[str, Any]]
    unlisted: list[str]


def parse_extraction(raw: str, products: Sequence[Mapping[str, Any]]) -> Extraction | None:
    """Validate the extraction call's output. None when it is not the strict shape."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
        return None
    exact = {str(p.get("name")): str(p.get("name")) for p in products if p.get("name")}
    folded = {name.casefold(): name for name in exact}
    items: list[dict[str, Any]] = []
    unlisted = [str(u)[:200] for u in data.get("unlisted") or [] if str(u).strip()][:5]
    for raw_item in data.get("items", [])[:20]:
        if not isinstance(raw_item, dict):
            return None
        name = str(raw_item.get("product_name") or "").strip()
        canonical = folded.get(name.casefold())
        if canonical is None:
            if name:
                unlisted.append(name[:200])
            continue
        weight = raw_item.get("weight_kg")
        quantity = raw_item.get("quantity")
        weight_value = weight if isinstance(weight, int) and 1 <= weight <= 1000 else None
        quantity_value = quantity if isinstance(quantity, int) and 1 <= quantity <= 10000 else None
        variant = raw_item.get("variant")
        items.append(
            {
                "product_name": canonical,
                "weight_kg": weight_value,
                "quantity": quantity_value,
                **({"variant": str(variant)[:100]} if isinstance(variant, str) and variant.strip()
                   else {}),
            }
        )
    return Extraction(items=items, unlisted=unlisted)


def mentions_unknown_item(text: str) -> bool:
    """True when the wording names *something* beyond the request phrase itself.

    Used to decide whether an unmatched quotation request deserves the extraction call
    ("a quote for a unicorn cake") or simply the "what would you like quoted?" question
    ("can I get a quote?").
    """
    stripped = _QUOTE_RE.sub(" ", text or "")
    stripped = re.sub(
        r"\b(?:hi|hello|hey|please|pls|kindly|can|could|would|i|me|my|you|we|us|get|"
        r"give|send|want|wanted|need|needed|like|to|for|a|an|the|some|of|and|is|it|do|"
        r"have|make|prepare|request|new|habari|tafadhali|naomba)\b",
        " ",
        stripped,
        flags=re.I,
    )
    # Audit fix D4: follow-up fragments ("Just one, get me a quote", "3 such cakes") are
    # counts and references to the request already discussed, not unknown products.
    stripped = re.sub(
        r"\b(?:just|only|such|same|those|these|them|that|this|again|another|more|"
        r"each|cakes?|items?|pieces?|ones?|actually|thanks|thank|ok|okay|yes|now|also|"
        + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")\b",
        " ",
        stripped,
        flags=re.I,
    )
    return len(re.findall(r"[A-Za-z]{3,}", stripped)) > 0


def unmatched_parts(text: str, products: Sequence[Mapping[str, Any]]) -> list[str]:
    """Clauses of ``text`` that name no catalogue product but do name something.

    Review follow-up F1: "2 Black Forest and a unicorn cake" matches Black Forest, and
    the "a unicorn cake" clause must not be silently dropped. Clauses are split the same
    way as :func:`match_items` (commas, "and", "plus", ...); sizes, counts, references and
    request words alone ("2 kg each", "just one please") are not reported.
    """
    names = [_core_tokens(p.get("name")) for p in products if p.get("name")]
    parts: list[str] = []
    for segment in _segments(text or ""):
        normalized = _normalize(segment)
        if any(tokens and _token_pattern(tokens).search(normalized) for tokens in names):
            continue
        if is_delivery_enquiry(segment):
            continue  # "delivered tomorrow" is about delivery, not a product
        if mentions_unknown_item(interpretation.strip_sizes(segment)):
            parts.append(segment.strip()[:200])
    return parts


def is_fragment(text: str) -> bool:
    """True when ``text`` names nothing beyond counts, references and request words.

    Used to drop "unlisted" entries that are really fragments of a follow-up message, so
    "Just one, get me a quote" is never handed to staff as a product (Audit fix D4).
    """
    return not mentions_unknown_item(text)
