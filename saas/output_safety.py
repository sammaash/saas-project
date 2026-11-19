"""Customer-facing output validation for every AI reply (all tenants, all tool workflows).

The bot's tools are driven by the model writing ``TOOL_CALL: {...}`` lines, and tool
results are fed back to it as ``TOOL_RESULT: {...}`` text. A model that echoes that
protocol, repeats a tool error, or stops mid-workflow must never have that text sent to a
customer. Production showed it happening once (2026-11-16): a raw ``TOOL_CALL`` line was
returned when the tool-call budget ran out and was delivered over WhatsApp.

``check_customer_text`` is the single gate. It is applied by ``BotResponseEngine`` to the
final text of every turn, so every host (WhatsApp webhook, admin playground, simulator)
receives either text that passed the gate or the platform-owned ``FALLBACK_TEXT`` plus a
machine-readable reason. The gate is deliberately conservative: it blocks the whole reply
rather than trying to scrub fragments out of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Sent instead of a blocked reply. It promises nothing the platform has not done and
#: names a real next step: "talk to a person" is recognised by the deterministic
#: human-request detector, which pauses the AI and alerts staff.
FALLBACK_TEXT = (
    "Sorry, I couldn't complete that just now. Please send your message again, or reply "
    "\"talk to a person\" and a member of our team will take over."
)

# Tool names and internal argument/result keys. None of these snake_case identifiers
# occurs in natural customer-facing prose.
_INTERNAL_IDENTIFIERS = (
    "get_catalogue", "get_business_rules", "request_staff_handover", "get_order_draft",
    "update_order_draft", "calculate_quote", "confirm_order", "get_order_status",
    "get_payment_status", "update_quotation_brief", "request_quote",
    "request_quotation_pdf", "use_brief", "idempotency_key", "tenant_id",
    "customer_key", "message_id", "conversation_id", "needs_staff", "elapsed_ms",
    "safe_test_mode", "send_enabled", "quotations_enabled", "variant_id",
)

_PROTOCOL_PATTERNS = (
    ("raw_tool_call", re.compile(r"tool[\s_-]*call\s*:", re.I)),
    ("raw_tool_call", re.compile(r"\bTOOL_CALL\b", re.I)),
    ("raw_tool_result", re.compile(r"tool[\s_-]*results?\s*:", re.I)),
    ("raw_tool_result", re.compile(r"\bTOOL_RESULT\b", re.I)),
    # A JSON object that names a tool/arguments pair, however it is spaced or quoted.
    ("tool_arguments", re.compile(r"[{,]\s*[\"']?(?:name|tool)[\"']?\s*:\s*[\"'][a-z_]+[\"']"
                                  r"[^{}]{0,200}[\"']?(?:arguments|ok)[\"']?\s*:", re.I)),
    ("tool_arguments", re.compile(r"[\"']arguments[\"']\s*:\s*\{", re.I)),
)

_INTERNAL_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(name) for name in _INTERNAL_IDENTIFIERS) + r")\b", re.I
)

_ERROR_PATTERNS = (
    re.compile(r"\bTool execution failed\b", re.I),
    re.compile(r"\bUnknown tool\b", re.I),
    re.compile(r"Traceback \(most recent call last\)", re.I),
    re.compile(r"\b(?:psycopg|sqlalchemy|KeyError|TypeError|ValueError|RuntimeError|"
               r"AttributeError|IndexError|Exception)\b"),
    re.compile(r"\bSAAS_DATABASE_URL\b|\bOPENAI_API_KEY\b", re.I),
)

# A reply that promises work the platform will do "later". The bot has no later turn
# (it only speaks when the customer writes), so such a promise is an incomplete tool
# workflow presented as a final answer. A real handover replaces the model's text with
# platform-owned wording before this gate matters, so the host checks it only when no
# handover was recorded.
_PENDING_PROMISES = (
    re.compile(r"\b(?:please\s+)?hold\s+on\b", re.I),
    re.compile(r"\b(?:one|just\s+a)\s+(?:moment|minute|sec(?:ond)?)\b", re.I),
    re.compile(r"\bgive\s+me\s+a\s+(?:moment|minute|sec(?:ond)?)\b", re.I),
    re.compile(r"\bwhile\s+I\s+(?:finali[sz]e|prepare|process|check|generate|create)\b", re.I),
    re.compile(r"\b(?:I\s+will|I'll|I\s+am|I'm)\s+(?:now\s+)?(?:preparing|prepare|"
               r"finali[sz]ing|finali[sz]e|generating|generate|processing)\b"
               r"[^.!?\n]{0,40}\b(?:quot\w*|quote|order|invoice|pdf|document)\b", re.I),
    re.compile(r"\b(?:I\s+will|I'll)\s+(?:get\s+back|send\s+(?:it|you\s+the\s+"
               r"(?:quot\w*|quote|pdf|document)))\b", re.I),
    re.compile(r"\b(?:a\s+)?(?:team\s+member|member\s+of\s+(?:our|the)\s+team|our\s+team|"
               r"the\s+team|staff)\s+will\s+(?:prepare|send|contact|reach\s+out|get\s+back|"
               r"follow\s+up|call)\b", re.I),
    re.compile(r"\bI(?:'ve|\s+have)\s+(?:notified|informed|alerted|let)\s+(?:our|the)\s+"
               r"(?:team|staff)\b", re.I),
)


# Deferred work the bot itself announces ("let me check", "I'll confirm and let you know",
# "checking now..."). Nothing runs after the turn ends, so unless a staff handover was
# recorded the check/confirmation never happens and the customer waits for a reply that
# will not come. A clarification question in the same reply does not make the promise
# true. Matched per sentence; a sentence is exempt only when the action is explicitly
# conditional on the customer's own next message ("once you tell me the size, I'll confirm
# the price"), because the bot does answer that next message.
_ACTION_VERBS = (
    r"(?:double[\s-]?check|check|confirm|verify|find\s+out|look\s+(?:into|up|for)|"
    r"ask\s+(?:our|the|my|a)\s+(?:team|staff|kitchen|baker|bakers|manager|chef|colleague)|"
    r"ask\s+(?:them|around)|see\s+if|enquire|inquire|get\s+back|come\s+back\s+to\s+you|"
    r"revert|follow\s+up|update\s+you|let\s+you\s+know|"
    r"get\s+(?:you\s+)?(?:an?\s+)?(?:answer|update|confirmation))"
)
_DEFERRED_ACTIONS = (
    re.compile(r"\blet\s+me\s+(?:just\s+|quickly\s+|first\s+|now\s+)?" + _ACTION_VERBS + r"\b",
               re.I),
    re.compile(r"\b(?:I\s+will|I'll|I\s+shall|I\s+am\s+going\s+to|I'm\s+going\s+to|"
               r"I'm\s+gonna)\s+(?:just\s+|quickly\s+|now\s+|first\s+|also\s+)?"
               + _ACTION_VERBS + r"\b", re.I),
    re.compile(r"\bwe(?:'ll|\s+will)\s+(?:just\s+|quickly\s+)?(?:find\s+out|look\s+into|"
               r"get\s+back|revert)\b", re.I),
    re.compile(r"\b(?:get|come)\s+back\s+to\s+you\b", re.I),
    re.compile(r"\b(?:I'm|I\s+am|we're|we\s+are)\s+(?:now\s+|currently\s+|just\s+)?"
               r"(?:checking|confirming|verifying|looking\s+into|finding\s+out|enquiring|"
               r"inquiring)\b", re.I),
    re.compile(r"^\W*checking\b", re.I),  # a sentence that starts "Checking ..."
    re.compile(r"\bchecking\s+(?:now|for\s+you)\b", re.I),
)
# A read-back made in this very reply ("Let me confirm your order: 2 x Vanilla 1 kg.") is
# not deferred work.
_READ_BACK_RE = re.compile(
    r"\blet\s+me\s+(?:just\s+)?(?:confirm|recap|summari[sz]e|repeat)\b[^.!?:\n]{0,40}:", re.I
)
_CUSTOMER_CONDITION_RE = re.compile(
    r"\b(?:once|when|after|as\s+soon\s+as)\s+you\b"
    r"|\bif\s+you\s+(?:tell|send|give|share|choose|pick|reply|let\s+me\s+know)\b"
    r"|\b(?:tell|send|give|show)\s+me\b[^.!?\n]{0,80}\band\s+(?:I|we)(?:'ll|\s+will)\b",
    re.I,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def _defers_work(value: str) -> bool:
    """True when a sentence announces work after this turn with nothing behind it."""
    for sentence in _SENTENCE_SPLIT_RE.split(value):
        if not sentence.strip() or _READ_BACK_RE.search(sentence):
            continue
        if any(pattern.search(sentence) for pattern in _DEFERRED_ACTIONS):
            if not _CUSTOMER_CONDITION_RE.search(sentence):
                return True
    return False


# The model can never send, attach or email a document (only the platform's quotation
# workflow sends one, with its own wording), so a claim that it did is always false.
_FALSE_COMPLETIONS = (
    re.compile(r"\bI(?:'ve|\s+have)?\s+(?:just\s+)?(?:sent|attached|emailed|shared|"
               r"forwarded|generated|created|issued)\b[^.!?\n]{0,40}\b(?:quot\w*|quote|pdf|"
               r"document|invoice|receipt|file)\b", re.I),
    re.compile(r"\b(?:here\s+is|here's|attached\s+is|please\s+find(?:\s+attached)?)\s+"
               r"(?:your|the)\s+(?:quot\w*|quote|pdf|document|invoice)\b", re.I),
    re.compile(r"\byour\s+(?:quot\w*|quote|pdf|document|invoice)\s+(?:is|has\s+been)\s+"
               r"(?:ready|attached|sent|on\s+its\s+way|being\s+prepared)\b", re.I),
)


@dataclass(frozen=True)
class OutputCheck:
    safe: bool
    reason: str | None = None


def check_customer_text(text: str | None, *, allow_promises: bool = False) -> OutputCheck:
    """Decide whether ``text`` may be shown to a customer.

    ``allow_promises`` must be True only when a staff handover was successfully recorded
    in this turn: then "our team will get back to you" is true. A failed or missing
    handover never allows it.

    Reasons: ``empty``, ``raw_tool_call``, ``raw_tool_result``, ``tool_arguments``,
    ``internal_identifier``, ``tool_error``, ``unbacked_claim``, ``unbacked_promise``.
    """
    value = (text or "").strip()
    if not value:
        return OutputCheck(False, "empty")
    for reason, pattern in _PROTOCOL_PATTERNS:
        if pattern.search(value):
            return OutputCheck(False, reason)
    if _INTERNAL_RE.search(value):
        return OutputCheck(False, "internal_identifier")
    for pattern in _ERROR_PATTERNS:
        if pattern.search(value):
            return OutputCheck(False, "tool_error")
    for pattern in _FALSE_COMPLETIONS:
        if pattern.search(value):
            return OutputCheck(False, "unbacked_claim")
    if not allow_promises:
        for pattern in _PENDING_PROMISES:
            if pattern.search(value):
                return OutputCheck(False, "unbacked_promise")
        if _defers_work(value):
            return OutputCheck(False, "unbacked_promise")
    return OutputCheck(True, None)


def safe_customer_text(text: str | None, *, allow_promises: bool = False) -> tuple[str, str | None]:
    """``(text_to_send, blocked_reason)``: the original text, or the fallback and why."""
    result = check_customer_text(text, allow_promises=allow_promises)
    if result.safe:
        return (text or "").strip(), None
    return FALLBACK_TEXT, result.reason
