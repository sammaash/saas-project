"""Proactive human handover: record the request, tell the staff, answer the customer.

The bot must never invent a price, promise availability, or send the customer off to chase
staff. When the catalogue cannot answer a request, the workflow is:

1. **record** the request durably (``saas.handover``, RLS-scoped, idempotent);
2. **notify** the tenant's own staff contact over the tenant's own WhatsApp channel;
3. **answer** the customer with wording the *server* chooses from the real outcome, so
   "I've shared your request" is only ever said after the request exists.

Step 1 happens inside the bot turn (``request_staff_handover`` tool). Step 2 needs the
tenant's channel and credentials, which only the host has, so it runs immediately after the
turn and stores its outcome on the same row. Notification is therefore *synchronous*: a slow
or failing Meta call delays one webhook response. That is the documented limitation -- it
does not require a queue, and a failure is visible and retryable instead of being lost.

Everything tenant-specific is configuration, so no business is hard-coded here: the staff
contact comes from the tenant's saved settings, and delivery uses the channel the inbound
message arrived on.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from saas import handover as handover_store

from ..config import Settings
from . import audit
from . import channels as channel_service
from . import tenants as tenant_service
from . import validation as v
from . import whatsapp as whatsapp_service

logger = logging.getLogger("platform_admin.handover")

#: Settings keys that may carry the tenant's staff contact, most specific first.
STAFF_FIELDS = ("human_handover_number", "escalation_whatsapp")

#: How many recent turns a staff notification may include.
CONTEXT_TURNS = 4

#: Statuses a member of staff may choose in the dashboard.
STAFF_STATUSES = handover_store.STAFF_STATUSES

APOLOGY = (
    "Sorry — I couldn't record your request just now, so the team could not be notified "
    "automatically. Please try again in a moment."
)


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _tail(value: Any, count: int = 9) -> str:
    return _digits(value)[-count:]


def staff_numbers(settings_document: dict[str, Any] | None) -> list[str]:
    """Every staff contact this tenant has configured, most specific first.

    ``human_handover_number`` is the explicit "human handover number"; the escalation
    number is the fallback. Both are tenant configuration, never platform defaults.
    """
    document = settings_document or {}
    configuration = document.get("configuration") or {}
    config = configuration.get("config") or {}
    rules = document.get("business_rules") or {}
    escalation = rules.get("escalation") or {}
    candidates = (config.get("human_handover_number"), escalation.get("whatsapp"))
    numbers: list[str] = []
    for candidate in candidates:
        cleaned = str(candidate or "").strip()
        if cleaned and cleaned not in numbers:
            numbers.append(cleaned)
    return numbers


def staff_target(settings_document: dict[str, Any] | None) -> str | None:
    """The number a handover notification goes to, or None when none is configured."""
    numbers = staff_numbers(settings_document)
    return numbers[0] if numbers else None


#: A run of characters that plausibly represents one phone number in free text.
_PHONE_RUN_RE = re.compile(r"[+\d][\d\s()\-]{6,}\d")


def _number_runs(text: str) -> list[str]:
    """Digit strings for each phone-looking run in ``text``, one run per number.

    Splitting into runs matters: merging every digit in the message would glue an invented
    price to the staff number and hide it (``4500`` + ``254700000999`` must not hide the
    number behind a merged ``...4500...`` prefix).
    """
    return [_digits(match) for match in _PHONE_RUN_RE.findall(str(text or ""))]


def reply_hands_off_to_staff(text: str, settings_document: dict[str, Any] | None) -> bool:
    """True when a reply pushes one of the tenant's own staff numbers at the customer.

    This is the deterministic safety net behind the tool. Whatever the model wrote, a reply
    that hands the customer the staff's own number is exactly the behaviour this workflow
    exists to replace -- and the signal is tenant configuration, not an English phrase.

    The comparison uses the last nine digits, so ``+254 700 000 999``, ``254700000999`` and
    ``0700 000 999`` are all recognised as the same configured contact.
    """
    runs = _number_runs(text)
    if not runs:
        return False
    for number in staff_numbers(settings_document):
        tail = _tail(number)
        if len(tail) >= 9 and any(tail in run for run in runs):
            return True
    return False


def confirmation_text(business_name: str, product: str | None, *, notified: bool) -> str:
    """The customer-facing answer, chosen by what actually happened.

    ``notified`` distinguishes "the team has your request" (delivery succeeded) from
    "your request is logged" (delivery failed). The second never claims the team was told.
    """
    business = business_name or "our"
    if product:
        opener = f"Thanks for your interest in our {product}!"
    else:
        opener = "Thanks for your message!"
    if notified:
        return (
            f"{opener} I've shared your request with the {business} team. Our staff will get "
            "back to you soon to confirm whether we can make it and provide the actual price."
        )
    return (
        f"{opener} I've logged your request for the {business} team. Our staff will get back "
        "to you soon to confirm whether we can make it and provide the actual price."
    )


def _format_timestamp(value: Any, timezone_name: str | None) -> str:
    if not isinstance(value, datetime):
        return "unknown"
    try:
        zone = ZoneInfo(timezone_name) if timezone_name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        zone = ZoneInfo("UTC")
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


def notification_text(
    business_name: str,
    record: dict[str, Any],
    *,
    timezone_name: str | None = None,
    reference: str | None = None,
) -> str:
    """The WhatsApp message staff receive. Plain text, no customer secrets beyond contact."""
    lines = [f"New customer request — {business_name}", ""]
    lines.append(f"Requested: {record.get('requested_product')}")
    details = record.get("details") or {}
    if isinstance(details, dict) and details:
        joined = " · ".join(f"{key}: {value}" for key, value in details.items())
        lines.append(f"Details: {joined}")
    wa_id = str(record.get("wa_id") or "").strip()
    lines.append(f"Customer WhatsApp: {wa_id or 'unknown'}")
    if record.get("customer_name"):
        lines.append(f"Customer name: {record['customer_name']}")
    lines.append(f"Received: {_format_timestamp(record.get('created_at'), timezone_name)}")
    reason = str(record.get("reason") or "").replace("_", " ")
    if reason:
        lines.append(f"Reason: {reason}")

    turns = record.get("context") or []
    if isinstance(turns, list) and turns:
        lines.append("")
        lines.append("Recent conversation:")
        for turn in turns[-CONTEXT_TURNS:]:
            if not isinstance(turn, dict):
                continue
            text = str(turn.get("text") or "").strip()[:300]
            if text:
                lines.append(f"- {turn.get('role') or 'customer'}: {text}")

    lines.append("")
    lines.append(
        "Please contact the customer, confirm whether you can make this, and send them the "
        "actual price."
    )
    if reference:
        lines.append(f"Reference: {reference}")
    return "\n".join(lines)[:4096]


def _settings_document(bot_response) -> dict[str, Any]:
    context = getattr(bot_response, "context", None)
    return dict(getattr(context, "settings", None) or {})


def _business_name(bot_response) -> str:
    context = getattr(bot_response, "context", None)
    tenant = getattr(context, "tenant", None) or {}
    return str(tenant.get("name") or "").strip()



def _notify(
    settings: Settings,
    *,
    channel: dict[str, Any],
    phone_number_id: str,
    tenant_id: str,
    target: str,
    text: str,
) -> str | None:
    """Send the staff notification. Returns None on success, or the failure reason."""
    try:
        whatsapp_service.send_text(
            settings,
            phone_number_id=phone_number_id,
            tenant_id=tenant_id,
            credentials_ref=channel.get("credentials_ref"),
            recipient=target,
            text=text,
        )
        return None
    except Exception as error:  # noqa: BLE001 - the reason is stored, not swallowed
        message = getattr(error, "message", None) or str(error)
        return f"{type(error).__name__}: {message}"[:500]


def deliver(
    settings: Settings,
    *,
    tenant_id: str,
    channel: dict[str, Any],
    phone_number_id: str,
    record: dict[str, Any],
    settings_document: dict[str, Any],
    business_name: str,
) -> bool:
    """Attempt the staff notification once and persist the outcome on the request.

    True only when the message actually went out. A failure is recorded as ``failed`` with
    the reason, so the dashboard shows it and staff can retry; it is never recorded as a
    success. With no staff contact configured the request is still kept -- that is the
    difference between "nobody is reachable" and "we lost the request".
    """
    handover_id = str(record["id"])
    database_url = settings.app_database_url
    target = staff_target(settings_document)
    if target is None:
        reason = "No staff contact is configured for this tenant."
        handover_store.mark_failed(tenant_id, handover_id, reason, database_url)
        logger.warning(
            "Handover %s recorded for tenant %s but no staff contact is configured",
            handover_id,
            tenant_id,
        )
        return False

    text = notification_text(
        business_name,
        record,
        timezone_name=settings_document.get("timezone"),
        reference=handover_id[:8],
    )
    error = _notify(
        settings,
        channel=channel,
        phone_number_id=phone_number_id,
        tenant_id=tenant_id,
        target=target,
        text=text,
    )
    if error is None:
        handover_store.mark_notified(tenant_id, handover_id, target, database_url)
        return True
    handover_store.mark_failed(tenant_id, handover_id, error, database_url)
    logger.error(
        "Handover notification failed tenant=%s request=%s reason=%s",
        tenant_id,
        handover_id,
        error,
    )
    return False



def _context_turns(
    history: list[dict[str, Any]] | None, customer_message: str
) -> list[dict[str, str]]:
    turns = [
        {"role": str(item.get("role") or "user"), "text": str(item.get("text") or "")[:400]}
        for item in (history or [])
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    turns.append({"role": "user", "text": customer_message[:400]})
    return turns[-CONTEXT_TURNS:]


def _describe_request(customer_message: str) -> str:
    """A usable description when the model did not name the product itself."""
    cleaned = re.sub(r"\s+", " ", str(customer_message or "")).strip()
    return cleaned[:200] or "Unspecified customer request"


def safe_reply(bot_response: Any) -> str:
    """What to send when the handover machinery itself failed.

    If this turn recorded or tried to record a handover, or the model tried to send the
    customer to staff, the only honest answer is the apology -- never the model's text.
    """
    document = _settings_document(bot_response)
    text = getattr(bot_response, "text", "") or ""
    if (
        getattr(bot_response, "handover", None)
        or getattr(bot_response, "handover_failed", False)
        or reply_hands_off_to_staff(text, document)
    ):
        return APOLOGY
    return text


def handle_turn(
    settings: Settings,
    *,
    tenant_id: str,
    channel: dict[str, Any],
    phone_number_id: str,
    customer_key: str,
    customer: dict[str, Any] | None,
    conversation: dict[str, Any] | None,
    message_id: str,
    customer_message: str,
    bot_response: Any,
    history: list[dict[str, Any]] | None = None,
) -> str | None:
    """Complete any handover this turn needs and return the customer-facing text.

    Returns None when the turn needs no handover, so the model's own reply stands. The
    returned text is chosen from what actually happened, never from what the model claimed.
    """
    document = _settings_document(bot_response)
    business = _business_name(bot_response)
    database_url = settings.app_database_url

    handover = getattr(bot_response, "handover", None)
    record: dict[str, Any] | None = None

    if not handover and getattr(bot_response, "handover_failed", False):
        # The model tried to record a request and the record was not written. Nothing may
        # claim the team has it, so the customer gets the apology.
        return APOLOGY

    if handover:
        try:
            record = handover_store.get_request(
                tenant_id, str(handover.get("handover_id") or ""), database_url
            )
        except Exception:  # noqa: BLE001 - a read failure must not break the reply
            logger.exception("Could not read handover %s", handover.get("handover_id"))
            record = None
        if record is None:
            return APOLOGY
    elif reply_hands_off_to_staff(getattr(bot_response, "text", "") or "", document):
        # Safety net: the model deferred to the customer instead of recording a request.
        # Reuse an open request for this customer rather than creating one per message.
        try:
            record = handover_store.latest_open_request(tenant_id, customer_key, database_url)
            if record is None:
                record, _created = handover_store.create_request(
                    tenant_id,
                    wa_id=customer_key,
                    requested_product=_describe_request(customer_message),
                    dedupe_key=message_id,
                    reason="customer_request",
                    customer_id=(customer or {}).get("id"),
                    conversation_id=(conversation or {}).get("id"),
                    customer_name=(customer or {}).get("name"),
                    context=_context_turns(history, customer_message),
                    database_url=database_url,
                )
        except Exception:  # noqa: BLE001 - honest failure beats a false claim
            logger.exception("Could not record a handover for tenant %s", tenant_id)
            return APOLOGY
    else:
        return None

    product = (
        None
        if str(record.get("reason") or "") == "customer_request"
        else record.get("requested_product")
    )
    if str(record.get("status") or "") in ("notified", "in_progress"):
        # Already delivered earlier: do not re-notify staff, but answer consistently.
        return confirmation_text(business, product, notified=True)

    notified = deliver(
        settings,
        tenant_id=tenant_id,
        channel=channel,
        phone_number_id=phone_number_id,
        record=record,
        settings_document=document,
        business_name=business,
    )
    return confirmation_text(business, product, notified=notified)



# --------------------------------------------------------------------- dashboard
def tenant_requests(
    settings: Settings, tenant_id: str, *, status: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """The tenant's handover requests for the dashboard view."""
    return handover_store.list_requests(
        tenant_id, status=status, limit=limit, database_url=settings.app_database_url
    )


def status_summary(settings: Settings, tenant_id: str) -> dict[str, int]:
    """Counts per status, so the dashboard can show at a glance what needs attention."""
    rows = handover_store.list_requests(
        tenant_id, limit=500, database_url=settings.app_database_url
    )
    summary = {name: 0 for name in handover_store.STATUSES}
    for row in rows:
        key = str(row.get("status") or "")
        summary[key] = summary.get(key, 0) + 1
    return summary


def change_status(
    settings: Settings, admin_id: str, tenant_id: str, handover_id: str, status: str
) -> None:
    """Move a request through the staff-driven part of its lifecycle."""
    if status not in handover_store.STAFF_STATUSES:
        raise v.ServiceError("Unsupported handover status.", 400)
    if not handover_store.set_status(
        tenant_id, handover_id, status, settings.app_database_url
    ):
        raise v.ServiceError("Handover not found.", 404)
    audit.record(
        settings, admin_id, tenant_id, "handover.status_changed", "handover_request",
        handover_id, {"status": status},
    )


def _active_channel(settings: Settings, tenant_id: str) -> dict[str, Any] | None:
    for channel in channel_service.list_channels(settings, tenant_id):
        if str(channel.get("status") or "") == "active" and channel.get("phone_number_id"):
            return channel
    return None


def renotify(settings: Settings, admin_id: str, tenant: dict[str, Any], handover_id: str) -> None:
    """Re-attempt a staff notification that failed, from the dashboard.

    Nothing is marked successful unless the message actually went out: a failure raises so
    the operator sees the reason, and the request stays visible for another attempt.
    """
    tenant_id = str(tenant["id"])
    record = handover_store.get_request(tenant_id, handover_id, settings.app_database_url)
    if record is None:
        raise v.ServiceError("Handover not found.", 404)
    channel = _active_channel(settings, tenant_id)
    if channel is None:
        raise v.ServiceError(
            "This tenant has no active WhatsApp channel, so staff cannot be notified.", 409
        )
    document = tenant_service.get_profile(settings, tenant_id)["settings"]
    delivered = deliver(
        settings,
        tenant_id=tenant_id,
        channel=channel,
        phone_number_id=str(channel["phone_number_id"]),
        record=record,
        settings_document=document,
        business_name=str(tenant.get("name") or ""),
    )
    audit.record(
        settings, admin_id, tenant_id, "handover.renotified", "handover_request", handover_id,
        {"delivered": delivered},
    )
    if not delivered:
        raise v.ServiceError(
            "The staff notification could not be delivered. Check the staff contact number and "
            "Meta's 24-hour messaging window, then try again.",
            502,
        )

