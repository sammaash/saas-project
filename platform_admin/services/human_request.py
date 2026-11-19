"""Deterministic detection of a customer asking to speak to a person.

Runs on the raw inbound text BEFORE any model call, so a request for a human never
depends on the model choosing to call a tool. The model's ``request_staff_handover``
tool and the handover safety net remain as backstops for phrasings this misses.

Deliberately conservative: it matches explicit requests ("can I talk to a human",
"real person please", "connect me to an agent") and ignores negations ("I don't need
a human", "no need for an agent"). A false negative falls back to the model; a false
positive would silence the AI for that customer until staff resume it, so precision
matters more than recall here.
"""

from __future__ import annotations

import re

_PERSON = (
    r"(?:a\s+|an\s+|the\s+|some\s+|your\s+|real\s+|actual\s+|live\s+)*"
    r"(?:human(?:\s+being)?|person|someone|somebody|agent|staff(?:\s+member)?|"
    r"representative|rep|customer\s+(?:care|service|support)|operator|manager|"
    r"member\s+of\s+(?:your\s+|the\s+)?(?:staff|team)|team\s+member|real\s+one)"
)

_VERB = (
    r"(?:speak|talk|chat|be\s+connected|connect\s+me|transfer\s+me|put\s+me\s+through|"
    r"get\s+me|pass\s+me|hand\s+me\s+over|escalate\s+me|i\s+(?:want|need|would\s+like|'d\s+like))"
)

_REQUEST_PATTERNS = (
    # "can I speak to a human", "connect me to an agent", "I want a real person"
    re.compile(rf"\b{_VERB}\s+(?:(?:to|with)\s+)?{_PERSON}\b"),
    # "human please", "real person pls", "agent!!"
    re.compile(rf"^\s*{_PERSON}\s*(?:please|pls|plz|now)?\s*[.!?]*\s*$"),
    # "is there a human", "are you a bot? i want a person"
    re.compile(r"\b(?:is\s+there|are\s+there)\s+(?:a\s+|any\s+)?(?:human|real\s+person|person|staff|agent)\b"),
    # Swahili, common in the Kenyan market: "nataka kuongea na mtu", "niongee na mtu"
    re.compile(r"\b(?:nataka|ningependa|naomba)\s+(?:kuongea|kuzungumza)\s+na\s+(?:mtu|binadamu|mhudumu)\b"),
    re.compile(r"\bniongee\s+na\s+(?:mtu|binadamu|mhudumu)\b"),
)

_NEGATION = re.compile(
    r"\b(?:don'?t|do\s+not|dont|no\s+need|not|never|without|no)\b[^.!?]{0,25}"
    r"\b(?:human|person|someone|agent|staff|representative|operator)\b"
)


def _normalise(text: str) -> str:
    cleaned = (text or "").lower().replace("’", "'")
    return re.sub(r"\s+", " ", cleaned).strip()


def is_human_request(text: str | None) -> bool:
    """True when the message explicitly asks to talk to a person."""
    cleaned = _normalise(text or "")
    if not cleaned or len(cleaned) > 500:
        return False
    if _NEGATION.search(cleaned):
        return False
    return any(pattern.search(cleaned) for pattern in _REQUEST_PATTERNS)


ACKNOWLEDGEMENT = (
    "Thanks — I've asked a member of our team to take over this chat. "
    "They'll reply here as soon as they can."
)
