"""Shared customer-message grammar for sizes and quantities (Phase 1, S2; defect D8).

Pure functions, no I/O. The v1 quotation matcher (:mod:`saas.quotation_intent`)
delegates to them today; engine v2 (later stages) reuses the same grammar.

Sizes are read in either order:

* number first: "2 kg", "2kg", "1.5 kilos", "two kilos", "a kilo";
* unit first:   "kilo 2", "kg 3", "kilo two" -- only when no number-first size is
  present, and never when the number is followed by a count word ("kg 2 cakes").

"half a kilo" is reported as unreadable (not a whole kilogram), as before.

Quantities in corrections ("change the quantity to two", "qty 3",
"change it to 2") are read by :func:`correction_quantity`.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

NUMBER_WORDS: dict[str, int] = {
    "a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "couple": 2, "pair": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "dozen": 12, "moja": 1, "mbili": 2, "tatu": 3, "nne": 4,
    "tano": 5,
}

# Words that may state a size. "couple", "pair", "dozen" and "single" are excluded:
# "a couple of kilos" is not a precise size.
_SIZE_WORDS = {
    word: value
    for word, value in NUMBER_WORDS.items()
    if word not in {"couple", "pair", "dozen", "single"}
}
# Explicit numbers in a correction. Articles are excluded: "change it to a chocolate"
# is not a quantity.
_COUNT_WORDS = {
    word: value
    for word, value in NUMBER_WORDS.items()
    if word not in {"a", "an", "single", "couple", "pair"}
}


def _alternation(words) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


_UNIT = r"(?:kg|kgs|kilo|kilos|kilograms?|kilogrammes?)"
_NUMBER = r"\d+(?:\.\d+)?"
_COUNT_NOUN = r"(?:x\b|pcs\b|pieces?\b|units?\b|cakes?\b|items?\b|of\s+them\b)"

_HALF_RE = re.compile(r"\bhalf\s+(?:a\s+)?(?:kg|kilo)\b", re.I)
_NUMBER_FIRST_RE = re.compile(
    r"(?<![\w.])(" + _NUMBER + r"|" + _alternation(_SIZE_WORDS) + r")\s*" + _UNIT + r"\b",
    re.I,
)
_UNIT_FIRST_RE = re.compile(
    r"\b" + _UNIT + r"\s+("
    + _NUMBER + r"|" + _alternation(w for w in _SIZE_WORDS if w not in {"a", "an"})
    + r")(?![\w.])(?!\s*" + _UNIT + r"\b)(?!\s*" + _COUNT_NOUN + r")",
    re.I,
)
_COUNT_TOKEN = r"(\d{1,4}|" + _alternation(_COUNT_WORDS) + r")"
_CORRECTION_RES = (
    re.compile(
        r"\b(?:quantity|qty|number\s+of\s+(?:cakes|pieces|items|units))\s+"
        r"(?:(?:to|is|be|should\s+be|of|as|=|:)\s+)?" + _COUNT_TOKEN + r"\b",
        re.I,
    ),
    re.compile(
        r"\b(?:change|update|set)\s+(?:it|that|them)\s+to\s+" + _COUNT_TOKEN + r"\b",
        re.I,
    ),
)


def _value(token: str) -> Decimal | None:
    token = token.lower()
    if token in _SIZE_WORDS:
        return Decimal(_SIZE_WORDS[token])
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def _size_pattern(text: str) -> re.Pattern[str] | None:
    """Number-first wins; unit-first is used only when there is no number-first size."""
    if _NUMBER_FIRST_RE.search(text):
        return _NUMBER_FIRST_RE
    if _UNIT_FIRST_RE.search(text):
        return _UNIT_FIRST_RE
    return None


def size_value(text: str) -> tuple[int | None, bool]:
    """``(whole-kilogram size, unreadable)`` from the first size mention in ``text``."""
    value = text or ""
    if _HALF_RE.search(value):
        return None, True
    pattern = _size_pattern(value)
    if pattern is None:
        return None, False
    number = _value(pattern.search(value).group(1))
    if number is None:
        return None, True
    if number != number.to_integral_value() or not 1 <= number <= 1000:
        return None, True
    return int(number), False


def strip_sizes(text: str) -> str:
    """``text`` with its size mentions blanked, so their numbers are not read as counts."""
    value = text or ""
    pattern = _size_pattern(value)
    return pattern.sub(" ", value) if pattern is not None else value


def count_value(token: str | None) -> int | None:
    if not token:
        return None
    token = token.lower()
    if token.isdigit():
        number = int(token)
        return number if 1 <= number <= 10000 else None
    return NUMBER_WORDS.get(token)


def correction_quantity(text: str) -> int | None:
    """A quantity stated as a correction: "change the quantity to two" -> 2."""
    value = strip_sizes(text)
    for pattern in _CORRECTION_RES:
        match = pattern.search(value)
        if match:
            return count_value(match.group(1))
    return None


# Quotation audit fixes (defects D2-D10) -------------------------------------------------------------
#: Wording that marks a message as replacing part of the open request (D2, D8).
_CORRECTION_WORDS_RE = re.compile(
    r"\b(?:change|switch|actually|instead|make\s+it|make\s+that|rather|not\s+that|"
    r"no[,.]?\s+(?:i\s+meant|i\s+want|make)|i\s+meant)\b",
    re.I,
)
#: A message that is only a number ("1", "two"): the answer to whatever was just asked.
_BARE_NUMBER_RE = re.compile(
    r"^\s*(?:option\s+|number\s+|no\.?\s*)?(\d{1,4}|" + _alternation(_COUNT_WORDS)
    + r")\s*[.!]?\s*$",
    re.I,
)
_REFERENTIAL_QUANTITY_RES = (
    re.compile(r"\b(?:just|only)\s+" + _COUNT_TOKEN + r"\b(?!\s*" + _UNIT + r"\b)", re.I),
    re.compile(
        r"\b" + _COUNT_TOKEN + r"\s+(?:more\s+)?(?:such|of\s+(?:those|these|them|the\s+same)|"
        r"(?:of\s+)?the\s+same)\b",
        re.I,
    ),
)


def is_correction(text: str) -> bool:
    """True when the customer is replacing something ("actually make it Vanilla")."""
    return bool(_CORRECTION_WORDS_RE.search(text or ""))


def bare_number(text: str) -> int | None:
    """The number when the whole message is just a number ("1", "option 2", "two")."""
    match = _BARE_NUMBER_RE.match(text or "")
    return count_value(match.group(1)) if match else None


def referential_quantity(text: str) -> int | None:
    """A count that refers to the open request: "just one" -> 1, "3 such cakes" -> 3."""
    value = strip_sizes(text)
    for pattern in _REFERENTIAL_QUANTITY_RES:
        match = pattern.search(value)
        if match:
            return count_value(match.group(1))
    return None
