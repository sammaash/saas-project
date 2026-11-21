"""One shared reader for a catalogue variant's size (Phase 1, S2; defect D11).

Quotation pricing and order pricing used to read variant sizes differently:
quotations looked only at ``attributes.weight_kg``, order pricing also parsed the
variant name. Catalogues whose variants are named "1 kg" / "2 kg" without the attribute
(Cindy's) therefore priced orders but could not price quotations. Every size read now
goes through :func:`variant_size_kg`, so the two can no longer disagree.

Precedence: a valid numeric ``attributes.weight_kg`` wins; otherwise the size written
in the variant name ("1 kg", "1.5kg", "2 kilos"); otherwise None. Sizes are returned
as :class:`~decimal.Decimal` so that 1.5 kg is never confused with 1 kg.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

_UNIT = r"(?:kg|kgs|kilo|kilos|kilogram|kilograms|kilogramme|kilogrammes)"
# A number immediately followed by a kilogram unit, not part of a longer number.
_NAME_SIZE_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*" + _UNIT + r"\b", re.I)


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def parse_size(label: Any) -> Decimal | None:
    """The kilogram size written in a variant label, e.g. "1 kg" -> Decimal("1")."""
    if label is None:
        return None
    match = _NAME_SIZE_RE.search(str(label))
    return _decimal(match.group(1)) if match else None


def variant_size_kg(variant: Mapping[str, Any] | None) -> Decimal | None:
    """The variant's size in kilograms, or None when it has no readable size."""
    if not isinstance(variant, Mapping):
        return None
    attributes = variant.get("attributes")
    if isinstance(attributes, Mapping) and attributes.get("weight_kg") is not None:
        size = _decimal(attributes.get("weight_kg"))
        if size is not None:
            return size
    return parse_size(variant.get("name"))


def size_label(size: Decimal | None) -> str | None:
    """Canonical text for a size: "2" for whole kilograms, "1.5" otherwise."""
    if size is None:
        return None
    if size == size.to_integral_value():
        return str(int(size))
    return format(size.normalize(), "f")


def matches_weight(variant: Mapping[str, Any] | None, weight_kg: Any) -> bool:
    """True when the variant's size equals the requested weight exactly."""
    size = variant_size_kg(variant)
    wanted = _decimal(weight_kg)
    return size is not None and wanted is not None and size == wanted
