"""HTTP routes. No SQL and no business rules here: routes call services."""

from __future__ import annotations

import logging
import io
import re
import time
import uuid
from datetime import datetime

from flask import (
    Blueprint,
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from .. import csrf
from ..auth import AuthError
from ..authz import admin_required, context
from ..services import catalogue as catalogue_service
from ..services import catalogue_import as catalogue_import_service
from ..services import bot_config as bot_config_service
from ..services import channels as channel_service
from ..services import customers as customer_service
from ..services import conversation_control
from ..services import handover as handover_service
from ..services import human_request as human_request_service
from ..services import members as members_service
from ..services import menu as menu_service
from ..services import menu_simulator as menu_simulator_service
from ..services import outbound_dispatch
from ..services import playground as playground_service
from ..services import quotation_flow
from ..services import quotations as quotation_service
from ..services import tenant_config
from ..services import tenants as tenant_service
from ..services import whatsapp as whatsapp_service
from ..services.validation import ServiceError
from saas import quotation_intent
from saas.bot import BotResponseEngine, ConversationTurn, TenantBotContextLoader
from saas import quotations as saas_quotations

auth_bp = Blueprint("auth", __name__)
admin_bp = Blueprint("admin", __name__)
whatsapp_bp = Blueprint("whatsapp", __name__)

GENERIC_LOGIN_ERROR = "Invalid email or password."
logger = logging.getLogger("platform_admin.playground")


def _send_menu_reply(
    settings,
    channel: dict,
    phone_number_id: str,
    tenant_id: str,
    recipient: str,
    reply: "menu_service.MenuReply",
) -> "menu_service.MenuSendResult":
    """Try the native shape, then fall back to text only when it is safe.

    A definite rejection (4xx, builder error, sending disabled) means Meta did not accept
    the interactive message, so a text fallback cannot duplicate it. A timeout, reset,
    5xx or malformed response is *ambiguous*: the message may already be delivered, so no
    fallback is attempted and the caller must record an uncertain outcome.
    """
    body = reply.body or reply.text
    interactive = reply.kind in ("list", "buttons") and len(body) <= 1024
    if interactive:
        try:
            if reply.kind == "list":
                wamid = whatsapp_service.send_list(
                    settings,
                    phone_number_id=phone_number_id,
                    tenant_id=tenant_id,
                    credentials_ref=channel.get("credentials_ref"),
                    recipient=recipient,
                    body=body,
                    button_text=reply.button_text,
                    sections=list(reply.sections),
                    header=reply.header,
                    footer=reply.footer,
                )
            else:
                wamid = whatsapp_service.send_buttons(
                    settings,
                    phone_number_id=phone_number_id,
                    tenant_id=tenant_id,
                    credentials_ref=channel.get("credentials_ref"),
                    recipient=recipient,
                    body=body,
                    buttons=list(reply.buttons),
                    header=reply.header,
                    footer=reply.footer,
                )
            return menu_service.MenuSendResult("sent", wamid=wamid)
        except whatsapp_service.WhatsAppSendError as error:
            if error.ambiguous:
                logger.warning(
                    "Ambiguous interactive send; not resending tenant=%s kind=%s code=%s",
                    tenant_id,
                    reply.kind,
                    error.provider_code or error.http_status,
                )
                return menu_service.MenuSendResult(
                    "uncertain",
                    error_code=error.provider_code or "ambiguous_send",
                    error_message=(error.provider_message or "WhatsApp outcome unknown")[:500],
                )
            logger.warning(
                "Interactive send rejected; using text tenant=%s kind=%s code=%s",
                tenant_id,
                reply.kind,
                error.provider_code or error.http_status,
            )
        except (ServiceError, ValueError) as error:
            logger.warning(
                "Interactive send unavailable; using text tenant=%s kind=%s error=%s",
                tenant_id,
                reply.kind,
                type(error).__name__,
            )
        except Exception as error:  # noqa: BLE001 - outcome is unknown; never resend
            logger.exception(
                "Unexpected interactive send failure tenant=%s kind=%s",
                tenant_id,
                reply.kind,
            )
            return menu_service.MenuSendResult(
                "uncertain",
                error_code="menu_send_exception",
                error_message=type(error).__name__,
            )
    try:
        wamid = whatsapp_service.send_text(
            settings,
            phone_number_id=phone_number_id,
            tenant_id=tenant_id,
            credentials_ref=channel.get("credentials_ref"),
            recipient=recipient,
            text=reply.text,
        )
        return menu_service.MenuSendResult("sent", wamid=wamid)
    except whatsapp_service.WhatsAppSendError as error:
        return menu_service.MenuSendResult(
            "uncertain" if error.ambiguous else "failed",
            error_code=error.provider_code
            or ("ambiguous_send" if error.ambiguous else "menu_delivery_failed"),
            error_message=(error.provider_message or type(error).__name__)[:500],
        )
    except ServiceError as error:
        return menu_service.MenuSendResult(
            "failed",
            error_code=type(error).__name__,
            error_message=str(getattr(error, "message", error))[:500],
        )
    except ValueError as error:
        return menu_service.MenuSendResult(
            "failed", error_code="invalid_text", error_message=str(error)[:500]
        )
    except Exception as error:  # noqa: BLE001 - outcome is unknown; never resend
        logger.exception("Unexpected text send failure tenant=%s", tenant_id)
        return menu_service.MenuSendResult(
            "uncertain",
            error_code="menu_send_exception",
            error_message=type(error).__name__,
        )


def _record_menu_outbound(
    settings,
    tenant_id: str,
    channel_id: str,
    conversation_id: str | None,
    inbound_message_id: str,
    reply: "menu_service.MenuReply",
) -> tuple[dict | None, str]:
    """Record the outbound intent before sending, reusing the existing message service.

    Returns ``(row, state)`` where state is one of:
    ``new`` (safe to send), ``already_sent`` (do not send), ``ambiguous`` (do not send),
    ``unavailable`` (could not record; send anyway, without delivery correlation).
    """
    if not conversation_id:
        return None, "unavailable"
    message_type = "interactive" if reply.kind in ("list", "buttons") else "text"
    common = {
        "direction": "outbound",
        "sender_type": "assistant",
        "message_type": message_type,
        "body_text": reply.text,
        "payload": {"menu": {"kind": reply.kind}},
    }
    try:
        try:
            row = customer_service.save_message(
                settings,
                tenant_id,
                channel_id,
                conversation_id,
                reply_to_provider_message_id=inbound_message_id,
                **common,
            )
        except customer_service.SchemaOutOfDateError:
            # 0035 is not applied: keep the outbound row and receipts, lose only the
            # inbound-reply correlation and the at-most-one-reply backstop.
            logger.warning(
                "0035 not applied; recording menu reply without correlation tenant=%s",
                tenant_id,
            )
            row = customer_service.save_message(
                settings, tenant_id, channel_id, conversation_id, **common
            )
        return row, "new"
    except customer_service.DuplicateReplyError:
        try:
            existing = customer_service.find_outbound_reply(
                settings, tenant_id, channel_id, inbound_message_id
            )
        except Exception as error:  # noqa: BLE001 - do not resend on unknown state
            logger.error(
                "Duplicate menu reply lookup failed tenant=%s error=%s",
                tenant_id,
                type(error).__name__,
            )
            return None, "ambiguous"
        if existing and existing.get("provider_message_id"):
            return existing, "already_sent"
        return existing, "ambiguous"
    except Exception as error:  # noqa: BLE001 - never block a menu reply on bookkeeping
        logger.error(
            "Menu outbound intent failed tenant=%s error=%s",
            tenant_id,
            type(error).__name__,
        )
        return None, "unavailable"


def _dispatch_menu_reply(
    settings,
    *,
    channel: dict,
    phone_number_id: str,
    tenant_id: str,
    conversation: dict | None,
    customer_key: str,
    menu_reply: "menu_service.MenuReply",
    outbox_row: dict | None,
    inbound_message_id: str,
):
    """Send one recorded menu reply through the dispatch boundary.

    The provider call runs inside the conversation lock after the AI-mode
    recheck (see ``outbound_dispatch``). Suppressed replies are recorded and
    acknowledged without sending; outcomes are reported exactly as observed.
    """

    def _send() -> str:
        result = _send_menu_reply(
            settings, channel, phone_number_id, tenant_id, customer_key, menu_reply
        )
        if result.outcome == "sent" and result.wamid:
            return result.wamid
        raise outbound_dispatch.SendFailed(
            result.error_code
            or ("ambiguous_send" if result.outcome == "uncertain" else "menu_delivery_failed"),
            (
                result.error_message
                or (
                    "WhatsApp outcome unknown"
                    if result.outcome == "uncertain"
                    else "Menu delivery failed"
                )
            )[:500],
            ambiguous=result.outcome == "uncertain",
        )

    outcome = outbound_dispatch.dispatch_reply(
        settings,
        tenant_id=tenant_id,
        conversation_id=str(conversation["id"]) if conversation else None,
        message_row_id=str(outbox_row["id"]) if (outbox_row or {}).get("id") else None,
        sender_type="assistant",
        send=_send,
        notice=bool(getattr(menu_reply, "notice", False)),
    )
    if outcome.kind == "suppressed":
        whatsapp_service.update_event(
            settings, tenant_id, inbound_message_id, "failed", "ai_reply_suppressed"
        )
        return jsonify(ok=True, menu=True, suppressed=True)
    if outcome.kind == "sent":
        whatsapp_service.update_event(
            settings,
            tenant_id,
            inbound_message_id,
            "processed",
            response_message_id=outcome.wamid,
        )
        return jsonify(ok=True, menu=True, delivery="sent")
    whatsapp_service.update_event(
        settings,
        tenant_id,
        inbound_message_id,
        "failed",
        "menu_delivery_uncertain" if outcome.kind == "unknown" else (outcome.error_code or "menu_delivery_failed"),
    )
    return jsonify(ok=True, menu=True, delivery="uncertain" if outcome.kind == "unknown" else "failed")


QUOTATION_FAILED_TEXT = (
    "I'm sorry — I couldn't create the quotation just now. Nothing was charged and no "
    "order was placed. Please try again, or reply STAFF and our team will help."
)
QUOTATION_PDF_FAILED_TEXT = (
    "I couldn't send the quotation PDF just now. Please try again shortly, or reply "
    "STAFF and our team will send it to you."
)


def _deliver_quotation_pdf(
    settings,
    *,
    tenant_id: str,
    channel: dict,
    channel_id: str | None,
    conversation: dict | None,
    customer_key: str,
    quotation: dict,
    inbound_message_id: str,
    caption: str | None = None,
    pdf: bytes | None = None,
    idempotency_key: str | None = None,
    sender_type: str = "assistant",
    phone_number_id: str | None = None,
) -> dict:
    """Send a persisted quotation as a WhatsApp document.

    The delivery row is claimed **before** anything is uploaded or sent. A replay
    therefore finds the existing row: a ``sent`` or ``uncertain`` attempt is never
    repeated, and only a definite ``failed`` attempt is retried.

    The document is the stored PDF (migration 0044), rendered from the quotation
    snapshot once; a document message row is recorded before the send so the inbox
    shows it and Meta's delivered/read/failed receipts reach it. ``sent`` is recorded
    only with the WhatsApp message id. With sending disabled nothing is uploaded and the
    attempt is recorded as ``failed / sending_disabled`` (shown as "Not sent").
    """
    quotation_id = str(quotation.get("id"))
    try:
        claim = saas_quotations.begin_delivery(
            settings,
            tenant_id,
            quotation_id=quotation_id,
            conversation_id=str(conversation["id"]) if conversation else None,
            channel_id=channel_id,
            customer_wa_id=customer_key,
            idempotency_key=idempotency_key
            or saas_quotations.delivery_key(quotation_id, inbound_message_id),
        )
    except Exception as error:  # noqa: BLE001 - never fail the webhook on bookkeeping
        logger.error(
            "Quotation delivery claim failed tenant=%s error=%s",
            tenant_id,
            type(error).__name__,
        )
        return {"outcome": "unavailable"}
    if not claim.get("should_send"):
        return {"outcome": "already", "status": claim.get("status")}

    delivery_id = str(claim["row"]["id"])
    filename = f"{quotation.get('quotation_number') or 'quotation'}.pdf"
    # The webhook's resolved channel carries no phone number id (the inbound event
    # does); a tenant_channels row (portal resend) carries it as a column.
    phone_id = phone_number_id or (channel or {}).get("phone_number_id")
    if not phone_id:
        saas_quotations.finish_delivery(
            settings,
            tenant_id,
            delivery_id,
            status="failed",
            error_code="channel_unavailable",
            error_message="No WhatsApp phone number id for this channel.",
        )
        return {"outcome": "failed", "error": "channel_unavailable"}
    if pdf is None:
        try:
            # Built from the persisted quotation and its saved line snapshots only, and
            # stored once: a later download returns exactly these bytes.
            pdf = quotation_service.store_pdf(settings, tenant_id, quotation)
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Quotation PDF rendering failed tenant=%s error=%s",
                tenant_id,
                type(error).__name__,
            )
            saas_quotations.finish_delivery(
                settings,
                tenant_id,
                delivery_id,
                status="failed",
                error_code="pdf_render_failed",
                error_message=f"PDF rendering failed ({type(error).__name__})",
            )
            return {"outcome": "failed", "error": "pdf_render_failed"}

    if not settings.whatsapp_send_enabled:
        saas_quotations.finish_delivery(
            settings,
            tenant_id,
            delivery_id,
            status="failed",
            error_code="sending_disabled",
            error_message="WhatsApp sending is disabled in this environment.",
        )
        return {"outcome": "failed", "error": "sending_disabled"}

    media_id = None
    try:
        media_id = whatsapp_service.upload_media(
            settings,
            phone_number_id=phone_id,
            tenant_id=tenant_id,
            credentials_ref=channel.get("credentials_ref"),
            filename=filename,
            mime_type="application/pdf",
            content=pdf,
        )
    except whatsapp_service.WhatsAppSendError as error:
        status = "uncertain" if error.ambiguous else "failed"
        saas_quotations.finish_delivery(
            settings,
            tenant_id,
            delivery_id,
            status=status,
            media_id=media_id,
            error_code=error.provider_code
            or ("ambiguous_send" if error.ambiguous else "document_rejected"),
            error_message=(error.provider_message or type(error).__name__)[:500],
        )
        logger.warning(
            "Quotation media upload %s tenant=%s code=%s",
            status,
            tenant_id,
            error.provider_code or error.http_status,
        )
        return {"outcome": status}
    except Exception as error:  # noqa: BLE001 - the outcome is unknown; never resend
        saas_quotations.finish_delivery(
            settings,
            tenant_id,
            delivery_id,
            status="uncertain",
            media_id=media_id,
            error_code="document_send_exception",
            error_message=type(error).__name__,
        )
        logger.exception("Quotation media upload failed tenant=%s", tenant_id)
        return {"outcome": "uncertain"}

    document_payload = {
        "document": {"filename": filename, "media_id": media_id},
        "quotation_id": quotation_id,
        "quotation_number": quotation.get("quotation_number"),
    }
    document_row = None
    if conversation:
        try:
            document_row = customer_service.save_message(
                settings,
                tenant_id,
                channel_id,
                str(conversation["id"]),
                direction="outbound",
                sender_type=sender_type,
                message_type="document",
                body_text=filename,
                payload=document_payload,
            )
        except Exception as error:  # noqa: BLE001 - recorded after the send instead
            logger.error(
                "Quotation document intent not recorded tenant=%s error=%s",
                tenant_id,
                type(error).__name__,
            )

    def _send_document() -> str:
        try:
            return whatsapp_service.send_document(
                settings,
                phone_number_id=phone_id,
                tenant_id=tenant_id,
                credentials_ref=channel.get("credentials_ref"),
                recipient=customer_key,
                media_id=media_id,
                filename=filename,
                caption=caption,
            )
        except whatsapp_service.WhatsAppSendError as error:
            raise outbound_dispatch.SendFailed(
                error.provider_code
                or ("ambiguous_send" if error.ambiguous else "document_rejected"),
                (error.provider_message or type(error).__name__)[:500],
                ambiguous=error.ambiguous,
            ) from error
        except Exception as error:  # noqa: BLE001 - outcome unknown; never resend
            raise outbound_dispatch.SendFailed(
                "document_send_exception", type(error).__name__, ambiguous=True
            ) from error

    # The document send is a customer-facing send: it runs through the dispatch
    # boundary (conversation lock + AI-mode recheck) like every other. A staff resend
    # (sender_type="staff") is allowed while the AI is paused, like a staff reply.
    try:
        outcome = outbound_dispatch.dispatch_reply(
            settings,
            tenant_id=tenant_id,
            conversation_id=str(conversation["id"]) if conversation else None,
            message_row_id=str(document_row["id"]) if (document_row or {}).get("id") else None,
            sender_type=sender_type,
            send=_send_document,
        )
    except Exception as error:  # noqa: BLE001
        wamid = getattr(error, "sent_wamid", None)
        if not wamid:
            # Raised before the provider call (busy conversation, lost connection): the
            # send callback converts every provider failure into an outcome, so no
            # document reached WhatsApp. A definite, retryable failure.
            saas_quotations.finish_delivery(
                settings,
                tenant_id,
                delivery_id,
                status="failed",
                media_id=media_id,
                error_code="document_dispatch_error",
                error_message=type(error).__name__,
            )
            logger.error(
                "Quotation document dispatch failed tenant=%s error=%s",
                tenant_id,
                type(error).__name__,
            )
            return {"outcome": "failed", "error": "document_dispatch_error"}
        # WhatsApp accepted it; only our record failed. Record the wamid on the ledger.
        saas_quotations.finish_delivery(
            settings,
            tenant_id,
            delivery_id,
            status="sent",
            media_id=media_id,
            provider_message_id=wamid,
        )
        return {"outcome": "sent", "wamid": wamid, "media_id": media_id, "recorded": False}
    if outcome.kind == "suppressed":
        saas_quotations.finish_delivery(
            settings,
            tenant_id,
            delivery_id,
            status="failed",
            media_id=media_id,
            error_code="ai_reply_suppressed",
            error_message="Suppressed: automation is paused for this conversation",
        )
        return {"outcome": "suppressed"}
    if outcome.kind != "sent":
        status = "uncertain" if outcome.kind == "unknown" else "failed"
        saas_quotations.finish_delivery(
            settings,
            tenant_id,
            delivery_id,
            status=status,
            media_id=media_id,
            error_code=(outcome.error_code or "")[:40] or None,
            error_message=(outcome.error_message or "")[:500] or None,
        )
        logger.warning(
            "Quotation document send %s tenant=%s code=%s",
            status,
            tenant_id,
            outcome.error_code,
        )
        return {"outcome": status}
    wamid = outcome.wamid

    try:
        saas_quotations.finish_delivery(
            settings,
            tenant_id,
            delivery_id,
            status="sent",
            media_id=media_id,
            provider_message_id=wamid,
        )
    except Exception as error:  # noqa: BLE001 - the document already reached Meta
        logger.error(
            "UNRECORDED OUTBOUND: quotation document delivery persistence failed "
            "tenant=%s delivery=%s wamid=%s error=%s",
            tenant_id,
            delivery_id,
            wamid,
            type(error).__name__,
        )
    if conversation and document_row is None:
        try:
            customer_service.save_message(
                settings,
                tenant_id,
                channel_id,
                str(conversation["id"]),
                direction="outbound",
                sender_type=sender_type,
                provider_message_id=wamid,
                message_type="document",
                body_text=filename,
                payload=document_payload,
                delivery_status="sent",
            )
        except Exception as error:  # noqa: BLE001 - the document already reached Meta
            logger.error(
                "Quotation document history failed tenant=%s wamid=%s error=%s",
                tenant_id,
                wamid,
                type(error).__name__,
            )
    return {"outcome": "sent", "wamid": wamid, "media_id": media_id}


@whatsapp_bp.get("/webhooks/whatsapp")
def whatsapp_verify():
    settings = context().settings
    if (
        request.args.get("hub.mode") != "subscribe"
        or request.args.get("hub.verify_token") != settings.whatsapp_verify_token
        or not settings.whatsapp_verify_token
    ):
        return "Forbidden", 403
    challenge = request.args.get("hub.challenge")
    return challenge if challenge else ("Bad Request", 400)


def _touch_conversation(settings, tenant_id: str, conversation: dict | None) -> None:
    if not conversation:
        return
    try:
        customer_service.update_conversation_activity(
            settings, tenant_id, str(conversation["id"])
        )
    except Exception as error:  # noqa: BLE001 - non-fatal
        logger.error(
            "Conversation activity refresh failed tenant=%s error=%s",
            tenant_id,
            type(error).__name__,
        )


def _automation_state(settings, tenant_id: str, conversation: dict | None):
    """``(ai_mode, needs_attention)`` for the gate; ``("unavailable", False)`` on error.

    No conversation row (persistence failed) keeps the pre-existing behaviour: the
    dispatch boundary has nothing to lock either, so ``("ai_active", False)``.
    """
    if not conversation:
        return "ai_active", False
    try:
        state = conversation_control.get_state(settings, tenant_id, str(conversation["id"]))
    except Exception as error:  # noqa: BLE001 - fail closed below
        logger.error(
            "Conversation state read failed tenant=%s error=%s",
            tenant_id,
            type(error).__name__,
        )
        return "unavailable", False
    return state if state is not None else ("unavailable", False)


def _skip_automation(
    settings,
    *,
    tenant_id: str,
    conversation: dict | None,
    message_id: str,
    mode: str,
    human_asked: bool,
):
    """AI is not active here: no model call, no tools, no menu, no automated reply.

    The inbound message is already stored. A fresh request for a person still raises
    the orange alert so staff notice it; nothing is sent to the customer.
    """
    if conversation and human_asked:
        try:
            conversation_control.request_human(
                settings,
                tenant_id=tenant_id,
                conversation_id=str(conversation["id"]),
                source="customer_message",
            )
        except Exception as error:  # noqa: BLE001 - the skip itself must still happen
            logger.error(
                "Human request flag failed tenant=%s error=%s", tenant_id, type(error).__name__
            )
    if conversation and mode != "unavailable":
        try:
            conversation_control.record_generation_skipped(
                settings,
                tenant_id=tenant_id,
                conversation_id=str(conversation["id"]),
                mode=mode,
                message_id=message_id,
            )
        except Exception as error:  # noqa: BLE001 - audit gap is logged, not fatal
            logger.error(
                "Suppression audit failed tenant=%s error=%s", tenant_id, type(error).__name__
            )
    whatsapp_service.update_event(
        settings,
        tenant_id,
        message_id,
        "failed",
        "ai_reply_suppressed" if mode != "unavailable" else "ai_state_unavailable",
    )
    return jsonify(ok=True, suppressed=True, stage="before_generation")


def _handle_human_request(
    settings,
    *,
    tenant_id: str,
    channel: dict,
    channel_id: str,
    phone_number_id: str,
    conversation: dict,
    customer: dict | None,
    customer_key: str,
    message_id: str,
    source: str = "customer_message",
    requested_product: str | None = None,
    acknowledgement: str | None = None,
    ack_kind: str = "human_request_ack",
):
    """The customer asked for a person while AI was active.

    Also used for discount / special-pricing requests (``source="discount_request"``):
    those are human-handled too, so the AI is paused for this conversation exactly the
    same way and staff take over the negotiation.

    Order matters: pause + orange alert FIRST (under the dispatch lock), so no AI
    reply already in flight for this customer can start sending afterwards. Then the
    durable handover record and staff notification, then one platform-worded
    acknowledgement sent as a ``notice`` (the only automated send allowed while paused).
    """
    conversation_id = str(conversation["id"])
    try:
        conversation_control.request_human(
            settings,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            source=source,
        )
    except Exception as error:  # noqa: BLE001 - the dispatch recheck still guards sends
        logger.error(
            "Human request flag failed tenant=%s error=%s", tenant_id, type(error).__name__
        )
    try:
        document = menu_service.load_document(settings, tenant_id) or {}
        handover_service.request_staff_assistance(
            settings,
            tenant_id=tenant_id,
            channel=channel,
            phone_number_id=phone_number_id,
            customer_key=customer_key,
            customer=customer,
            conversation=conversation,
            message_id=message_id,
            settings_document=document,
            business_name=str(document.get("business_name") or "our team"),
            source=source,
            requested_product=requested_product,
        )
    except Exception as error:  # noqa: BLE001 - the inbox alert is already raised
        logger.error(
            "Staff notification for human request failed tenant=%s error=%s",
            tenant_id,
            type(error).__name__,
        )
    text = acknowledgement or human_request_service.ACKNOWLEDGEMENT
    try:
        outbox_row = customer_service.save_message(
            settings,
            tenant_id,
            channel_id,
            conversation_id,
            direction="outbound",
            sender_type="system",
            message_type="text",
            body_text=text,
            payload={"text": text, "kind": ack_kind},
            reply_to_provider_message_id=message_id,
        )
    except customer_service.DuplicateReplyError:
        existing = customer_service.find_outbound_reply(
            settings, tenant_id, channel_id, message_id
        )
        already_sent = (existing or {}).get("provider_message_id")
        if already_sent:
            whatsapp_service.update_event(
                settings, tenant_id, message_id, "processed",
                response_message_id=str(already_sent),
            )
            return jsonify(ok=True, human_requested=True, duplicate_reply=True)
        whatsapp_service.update_event(
            settings, tenant_id, message_id, "failed", "ambiguous_send"
        )
        return jsonify(ok=True, human_requested=True, ambiguous=True)
    except Exception as error:  # noqa: BLE001 - still acknowledge, as the AI path does
        # The intent row could not be written. Duplicate sends stay impossible (the
        # inbound id is already claimed, so a redelivery is dropped), and the send below
        # is still gated and locked. The row is written after the send instead.
        logger.error(
            "Human-request acknowledgement intent not recorded tenant=%s message_id=%s "
            "error=%s",
            tenant_id,
            message_id,
            type(error).__name__,
        )
        outbox_row = None
    _touch_conversation(settings, tenant_id, conversation)
    try:
        outcome = outbound_dispatch.dispatch_reply(
            settings,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_row_id=str(outbox_row["id"]) if (outbox_row or {}).get("id") else None,
            sender_type="system",
            notice=True,
            send=lambda: whatsapp_service.send_text(
                settings,
                phone_number_id=phone_number_id,
                tenant_id=tenant_id,
                credentials_ref=channel.get("credentials_ref"),
                recipient=customer_key,
                text=text,
            ),
        )
    except Exception as error:  # noqa: BLE001
        wamid = getattr(error, "sent_wamid", None)
        if not wamid:
            raise
        # Sent, but the row could not be finalised (already logged by the boundary).
        # Keep the wamid on the event for reconciliation; a redelivery is dropped by
        # the claim, so this can never produce a second acknowledgement.
        whatsapp_service.update_event(
            settings, tenant_id, message_id, "failed", "ack_sent_unrecorded",
            response_message_id=wamid,
        )
        return jsonify(ok=True, human_requested=True, delivery="sent", recorded=False)
    if outcome.kind == "sent":
        recorded = outbox_row is not None
        if not recorded:
            # Post-send persistence, exactly like the AI path's pre-0035 fallback.
            try:
                customer_service.save_message(
                    settings,
                    tenant_id,
                    channel_id,
                    conversation_id,
                    direction="outbound",
                    sender_type="system",
                    provider_message_id=outcome.wamid,
                    message_type="text",
                    body_text=text,
                    payload={"text": text, "kind": ack_kind},
                    delivery_status="sent",
                )
                recorded = True
            except Exception as error:  # noqa: BLE001 - reported below, never hidden
                logger.error(
                    "UNRECORDED OUTBOUND: human-request acknowledgement was sent but its "
                    "message row could not be saved tenant=%s inbound=%s wamid=%s error=%s",
                    tenant_id,
                    message_id,
                    outcome.wamid,
                    type(error).__name__,
                )
        # The wamid is kept on the webhook event either way: that is the reconciliation
        # key for an acknowledgement whose message row is missing.
        whatsapp_service.update_event(
            settings,
            tenant_id,
            message_id,
            "processed",
            None if recorded else "ack_sent_unrecorded",
            response_message_id=outcome.wamid,
        )
        return jsonify(ok=True, human_requested=True, delivery="sent", recorded=recorded)
    whatsapp_service.update_event(
        settings,
        tenant_id,
        message_id,
        "failed",
        "ambiguous_send" if outcome.kind == "unknown" else (outcome.error_code or "send_failed"),
    )
    return jsonify(
        ok=True,
        human_requested=True,
        delivery="uncertain" if outcome.kind == "unknown" else "failed",
    )


DISCOUNT_ACKNOWLEDGEMENT = (
    "Thanks for asking. Pricing changes are handled by our team, so I've passed your "
    "request to them. A member of our team will reply here as soon as they can."
)
DELIVERY_LOCATION_QUESTION = (
    "Delivery charges are calculated separately based on the delivery location. Where "
    "would you like your order delivered? Please send the area or estate name."
)
STAFF_HANDOVER_FAILED_TEXT = (
    "Sorry, I couldn't pass your request to our team automatically. Please reply "
    "\"talk to a person\" and a member of our team will take over."
)
_LOCATION_RE = re.compile(
    r"\b(?:to|in|at|around|near)\s+([A-Za-z][A-Za-z'\- ]{1,40}?)"
    r"\s*(?=[?.!,]|$|\s+(?:please|pls|today|tomorrow|area|estate)\b)",
    re.I,
)
_NOT_A_PLACE = re.compile(r"^(?:my|our|your|me|us|the|a|an|this|that|there|here|you)\b", re.I)


def _delivery_location(text: str) -> str | None:
    match = _LOCATION_RE.search(text or "")
    if not match:
        return None
    place = match.group(1).strip(" '-")
    if not place or _NOT_A_PLACE.match(place):
        return None
    return place[:80]


def _send_platform_text(
    settings,
    *,
    tenant_id: str,
    channel: dict,
    channel_id: str,
    phone_number_id: str,
    conversation: dict,
    customer_key: str,
    message_id: str,
    text: str,
    kind: str,
) -> dict:
    """Send one platform-worded reply to this inbound message (intent row first).

    Same guarantees as the AI path: the reply row is unique per inbound message, the
    send runs inside the dispatch boundary (Stage 2 pause recheck), and a reply whose
    outcome is unknown is never resent. Returns ``{"state", "wamid"}``.
    """
    conversation_id = str(conversation["id"])
    payload = {"text": text, "kind": kind}
    try:
        outbox_row = customer_service.save_message(
            settings,
            tenant_id,
            channel_id,
            conversation_id,
            direction="outbound",
            sender_type="assistant",
            message_type="text",
            body_text=text,
            payload=payload,
            reply_to_provider_message_id=message_id,
        )
    except customer_service.DuplicateReplyError:
        existing = customer_service.find_outbound_reply(
            settings, tenant_id, channel_id, message_id
        )
        already = (existing or {}).get("provider_message_id")
        return {"state": "duplicate" if already else "ambiguous", "wamid": already}
    except Exception as error:  # noqa: BLE001 - recorded after the send instead
        logger.error(
            "Platform reply intent not recorded tenant=%s message_id=%s error=%s",
            tenant_id,
            message_id,
            type(error).__name__,
        )
        outbox_row = None
    try:
        outcome = outbound_dispatch.dispatch_reply(
            settings,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_row_id=str(outbox_row["id"]) if (outbox_row or {}).get("id") else None,
            sender_type="assistant",
            send=lambda: whatsapp_service.send_text(
                settings,
                phone_number_id=phone_number_id,
                tenant_id=tenant_id,
                credentials_ref=channel.get("credentials_ref"),
                recipient=customer_key,
                text=text,
            ),
        )
    except Exception as error:  # noqa: BLE001
        wamid = getattr(error, "sent_wamid", None)
        if not wamid:
            logger.error(
                "Platform reply dispatch failed tenant=%s error=%s",
                tenant_id,
                type(error).__name__,
            )
            return {"state": "failed", "wamid": None, "error_code": "dispatch_error"}
        return {"state": "sent_unrecorded", "wamid": wamid}
    if outcome.kind == "sent" and outbox_row is None:
        try:
            customer_service.save_message(
                settings,
                tenant_id,
                channel_id,
                conversation_id,
                direction="outbound",
                sender_type="assistant",
                provider_message_id=outcome.wamid,
                message_type="text",
                body_text=text,
                payload=payload,
                delivery_status="sent",
            )
        except Exception as error:  # noqa: BLE001
            logger.error(
                "UNRECORDED OUTBOUND: platform reply was sent but its message row could "
                "not be saved tenant=%s inbound=%s wamid=%s error=%s",
                tenant_id,
                message_id,
                outcome.wamid,
                type(error).__name__,
            )
            return {"state": "sent_unrecorded", "wamid": outcome.wamid}
    return {
        "state": outcome.kind,
        "wamid": outcome.wamid,
        "error_code": outcome.error_code,
    }


def _record_platform_turn(
    settings, *, tenant_id: str, customer_key: str, message_id: str, customer_text: str,
    reply_text: str, sent: dict,
) -> None:
    """Keep the AI's short history in step, and close the webhook event truthfully."""
    try:
        history = whatsapp_service.conversation_history(settings, tenant_id, customer_key)
        history.extend(
            [
                {"role": "user", "text": customer_text[:1000]},
                {"role": "assistant", "text": reply_text[:1000]},
            ]
        )
        whatsapp_service.save_conversation(settings, tenant_id, customer_key, history)
    except Exception as error:  # noqa: BLE001 - history is context only
        logger.error(
            "Conversation history save failed tenant=%s error=%s",
            tenant_id,
            type(error).__name__,
        )
    state = sent.get("state")
    if state in ("sent", "duplicate"):
        whatsapp_service.update_event(
            settings, tenant_id, message_id, "processed",
            response_message_id=sent.get("wamid"),
        )
    elif state == "sent_unrecorded":
        whatsapp_service.update_event(
            settings, tenant_id, message_id, "failed", "reply_sent_unrecorded",
            response_message_id=sent.get("wamid"),
        )
    elif state == "suppressed":
        whatsapp_service.update_event(
            settings, tenant_id, message_id, "failed", "ai_reply_suppressed"
        )
    else:
        whatsapp_service.update_event(
            settings,
            tenant_id,
            message_id,
            "failed",
            "ambiguous_send"
            if state in ("unknown", "ambiguous")
            else str(sent.get("error_code") or "send_failed")[:40],
        )


def _staff_handover(
    settings, *, tenant_id: str, channel: dict, phone_number_id: str, customer_key: str,
    customer: dict | None, conversation: dict, message_id: str, source: str,
    request: str, reason: str, details: dict,
) -> bool:
    """A product handover (quotation or delivery). The AI is NOT paused for these."""
    try:
        document = menu_service.load_document(settings, tenant_id) or {}
        handover_service.request_staff_assistance(
            settings,
            tenant_id=tenant_id,
            channel=channel,
            phone_number_id=phone_number_id,
            customer_key=customer_key,
            customer=customer,
            conversation=conversation,
            message_id=message_id,
            settings_document=document,
            business_name=str(document.get("business_name") or "our team"),
            source=source,
            requested_product=request[:200],
            reason=reason,
            details=details,
        )
        return True
    except Exception as error:  # noqa: BLE001 - reply honestly instead
        logger.error(
            "Staff handover failed tenant=%s source=%s error=%s",
            tenant_id,
            source,
            type(error).__name__,
        )
        return False


def _quotation_or_delivery_turn(
    settings,
    *,
    tenant_id: str,
    channel: dict,
    channel_id: str,
    phone_number_id: str,
    conversation: dict,
    customer: dict | None,
    customer_key: str,
    message_id: str,
    text: str,
    profile_name: str | None,
):
    """Stage 3: explicit quotation requests and delivery enquiries, handled
    deterministically for tenants that enabled quotations. None = let the AI answer.

    Ordinary price questions ("how much is a 1kg cake?") are NOT handled here: they
    reach the AI, which answers from the catalogue and never creates a quotation.
    """
    conversation_id = str(conversation["id"])
    # Decide cheaply whether this message can belong here at all, so an ordinary
    # message never reads the tenant settings (it goes straight to the AI).
    brief = quotation_flow.has_active_request(settings, tenant_id, conversation_id)
    quote_turn = quotation_flow.applies(text, brief)
    delivery_enquiry = quotation_intent.is_delivery_enquiry(text)
    awaiting_location = False
    if not quote_turn and not delivery_enquiry:
        if quotation_intent.classify(text) is not None or len(text) > 120:
            return None
        try:
            history = whatsapp_service.conversation_history(settings, tenant_id, customer_key)
        except Exception:  # noqa: BLE001
            history = []
        last = next(
            (item for item in reversed(history or [])
             if isinstance(item, dict) and item.get("role") == "assistant"),
            None,
        )
        awaiting_location = bool(last) and str(last.get("text") or "").strip() == (
            DELIVERY_LOCATION_QUESTION
        )
        if not awaiting_location:
            return None
    try:
        document = menu_service.load_document(settings, tenant_id) or {}
    except Exception as error:  # noqa: BLE001 - fall through to the AI path
        logger.error(
            "Quotation settings read failed tenant=%s error=%s", tenant_id, type(error).__name__
        )
        return None
    configuration = document.get("configuration") or {}
    if not saas_quotations.quotations_enabled(configuration):
        return None
    common = dict(
        tenant_id=tenant_id, channel=channel, channel_id=channel_id,
        phone_number_id=phone_number_id, conversation=conversation,
        customer_key=customer_key, message_id=message_id,
    )

    if quote_turn:
        step = quotation_flow.run(
            settings,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            customer_key=customer_key,
            customer_name=(customer or {}).get("name") or profile_name,
            message_id=message_id,
            text=text,
            provider=context().bot_provider,
            brief=brief,
        )
        if step is not None:
            reply = step.text
            if step.kind == "staff":
                if not _staff_handover(
                    settings, tenant_id=tenant_id, channel=channel,
                    phone_number_id=phone_number_id, customer_key=customer_key,
                    customer=customer, conversation=conversation, message_id=message_id,
                    source="quotation_flow", request=step.request or text[:150],
                    reason=step.reason or "custom_order", details=step.details,
                ):
                    reply = STAFF_HANDOVER_FAILED_TEXT
            sent = _send_platform_text(settings, text=reply, kind=f"quotation_{step.kind}",
                                       **common)
            delivery = None
            if step.kind == "quotation" and step.quotation:
                try:
                    delivery = _deliver_quotation_pdf(
                        settings,
                        tenant_id=tenant_id,
                        channel=channel,
                        channel_id=channel_id,
                        conversation=conversation,
                        customer_key=customer_key,
                        quotation=step.quotation,
                        inbound_message_id=message_id,
                        pdf=step.pdf,
                        caption=f"Quotation {step.quotation.get('quotation_number')}",
                        phone_number_id=phone_number_id,
                    )
                except Exception as error:  # noqa: BLE001 - recorded on the ledger
                    logger.error(
                        "Quotation document delivery failed tenant=%s error=%s",
                        tenant_id,
                        type(error).__name__,
                    )
                    delivery = {"outcome": "failed"}
            _record_platform_turn(
                settings, tenant_id=tenant_id, customer_key=customer_key,
                message_id=message_id, customer_text=text, reply_text=reply, sent=sent,
            )
            return jsonify(
                ok=True,
                quotation_step=step.kind,
                quotation_number=(step.quotation or {}).get("quotation_number"),
                reply=sent.get("state"),
                document=(delivery or {}).get("outcome"),
            )

    if configuration.get("delivery_available") is False:
        return None
    if not delivery_enquiry and not awaiting_location:
        return None
    location = (
        text.strip().strip(".!?")[:80] if awaiting_location else _delivery_location(text)
    )
    if not location:
        reply = DELIVERY_LOCATION_QUESTION
        kind = "delivery_question"
    else:
        handed = _staff_handover(
            settings, tenant_id=tenant_id, channel=channel, phone_number_id=phone_number_id,
            customer_key=customer_key, customer=customer, conversation=conversation,
            message_id=message_id, source="delivery_enquiry",
            request=f"Delivery charge to {location}", reason="custom_order",
            details={"location": location},
        )
        reply = (
            "Thanks. Delivery charges are calculated separately based on the delivery "
            f"location. I've passed \"{location}\" to our team and they will confirm the "
            "delivery charge with you here."
            if handed
            else STAFF_HANDOVER_FAILED_TEXT
        )
        kind = "delivery_handover"
    sent = _send_platform_text(settings, text=reply, kind=kind, **common)
    _record_platform_turn(
        settings, tenant_id=tenant_id, customer_key=customer_key, message_id=message_id,
        customer_text=text, reply_text=reply, sent=sent,
    )
    return jsonify(ok=True, delivery_step=kind, reply=sent.get("state"))


@whatsapp_bp.post("/webhooks/whatsapp")
def whatsapp_webhook():
    settings = context().settings
    raw_body = request.get_data(cache=True)
    if not whatsapp_service.verify_signature(
        raw_body, request.headers.get("X-Hub-Signature-256"), settings.whatsapp_app_secret
    ):
        return jsonify(ok=False, error="Invalid webhook signature."), 403
    payload = None
    try:
        payload = request.get_json(silent=True)
        event = whatsapp_service.parse_event(payload)
    except (TypeError, ValueError):
        event = None
    if event is None:
        # A delivery-status webhook describes a message we sent. It carries no customer
        # message id, so it is never claimed in whatsapp_webhook_events; applying it is
        # idempotent instead (see customers.update_delivery_status).
        statuses = whatsapp_service.parse_status_events(payload)
        if statuses:
            _apply_delivery_statuses(settings, statuses)
            return jsonify(ok=True, statuses=len(statuses))
        return jsonify(ok=True, ignored=True)
    if event["kind"] != "text":
        action_id = str(event.get("action_id") or "")
        if not action_id.startswith(menu_service.ACTION_PREFIX + "."):
            # Not one of our menu ids (or menus are off): acknowledge and ignore, exactly
            # as an unsupported interactive message behaved before menus existed.
            logger.info(
                "Interactive inbound acknowledged without routing channel=%s action_type=%s",
                event["phone_number_id"],
                event["action_type"],
            )
            return jsonify(ok=True, ignored=True, kind=event["kind"])
        channel = whatsapp_service.resolve_channel(settings, event["phone_number_id"])
        if channel is None:
            return jsonify(ok=False, error="Unknown or inactive channel."), 404
        tenant_id = str(channel["tenant_id"])
        channel_id = str(channel["channel_id"])
        document = menu_service.load_document(settings, tenant_id)
        parsed = menu_service.parse_action(action_id)
        if not menu_service.is_enabled(document) or (
            parsed.selection is None and not parsed.expired
        ):
            logger.info(
                "Interactive menu ignored (disabled or unknown) tenant=%s", tenant_id
            )
            return jsonify(ok=True, ignored=True, kind=event["kind"])
        if not whatsapp_service.claim_event(
            settings, tenant_id, channel_id, event["message_id"], event["customer_key"]
        ):
            return jsonify(ok=True, duplicate=True)

        customer = None
        conversation = None
        try:
            customer = customer_service.get_or_create_customer(
                settings, tenant_id, channel_id, event["customer_key"]
            )
            conversation = customer_service.get_or_create_conversation(
                settings, tenant_id, customer["id"], channel_id
            )
            customer_service.save_message(
                settings,
                tenant_id,
                channel_id,
                str(conversation["id"]),
                direction="inbound",
                sender_type="customer",
                provider_message_id=event["message_id"],
                message_type="interactive",
                body_text=event.get("action_title") or None,
                payload=event,
            )
        except Exception as error:  # noqa: BLE001 - never block the acknowledgement
            logger.error(
                "Interactive persistence failed tenant=%s error=%s",
                tenant_id,
                type(error).__name__,
            )

        _touch_conversation(settings, tenant_id, conversation)
        mode, _attention = _automation_state(settings, tenant_id, conversation)
        if mode != "ai_active":
            return _skip_automation(
                settings,
                tenant_id=tenant_id,
                conversation=conversation,
                message_id=event["message_id"],
                mode=mode,
                human_asked=bool(
                    parsed.selection is not None and parsed.selection.step == "staff"
                ),
            )
        ctx = menu_service.MenuContext(
            settings=settings,
            tenant_id=tenant_id,
            wa_id=event["customer_key"],
            document=document,
            channel=channel,
            phone_number_id=event["phone_number_id"],
            customer=customer,
            conversation=conversation,
            message_id=event["message_id"],
        )
        reply = menu_service.handle_action(ctx, action_id)
        if reply is None:
            whatsapp_service.update_event(
                settings, tenant_id, event["message_id"], "failed", "menu_no_reply"
            )
            return jsonify(ok=True, ignored=True, kind=event["kind"])
        outbox_row, outbox_state = _record_menu_outbound(
            settings,
            tenant_id,
            channel_id,
            str(conversation["id"]) if conversation else None,
            event["message_id"],
            reply,
        )
        if outbox_state == "already_sent":
            existing = (outbox_row or {}).get("provider_message_id")
            whatsapp_service.update_event(
                settings,
                tenant_id,
                event["message_id"],
                "processed",
                response_message_id=existing,
            )
            return jsonify(ok=True, menu=True, duplicate_reply=True)
        if outbox_state == "ambiguous":
            whatsapp_service.update_event(
                settings, tenant_id, event["message_id"], "failed", "ambiguous_send"
            )
            return jsonify(ok=True, ambiguous=True)
        return _dispatch_menu_reply(
            settings,
            channel=channel,
            phone_number_id=event["phone_number_id"],
            tenant_id=tenant_id,
            conversation=conversation,
            customer_key=event["customer_key"],
            menu_reply=reply,
            outbox_row=outbox_row,
            inbound_message_id=event["message_id"],
        )
    channel = whatsapp_service.resolve_channel(settings, event["phone_number_id"])
    if channel is None:
        return jsonify(ok=False, error="Unknown or inactive channel."), 404
    if not whatsapp_service.claim_event(
        settings,
        str(channel["tenant_id"]),
        str(channel["channel_id"]),
        event["message_id"],
        event["customer_key"],
    ):
        return jsonify(ok=True, duplicate=True)

    tenant_id = str(channel["tenant_id"])
    channel_id = str(channel["channel_id"])
    customer_key = event["customer_key"]

    # --- tenant-isolated customer / conversation / message persistence -------
    # These writes are non-fatal: a persistence problem must not prevent a
    # 200 to Meta (which would trigger redelivery), but they are logged so
    # the gap is visible. The existing whatsapp_conversations jsonb writes are
    # preserved during the transition.
    customer = None
    conversation = None
    try:
        customer = customer_service.get_or_create_customer(
            settings, tenant_id, channel_id, customer_key
        )
        conversation = customer_service.get_or_create_conversation(
            settings, tenant_id, customer["id"], channel_id
        )
        customer_service.save_message(
            settings,
            tenant_id,
            channel_id,
            str(conversation["id"]),
            direction="inbound",
            sender_type="customer",
            provider_message_id=event["message_id"],
            message_type="text",
            body_text=event["text"],
            payload=event,
        )
    except Exception as error:  # noqa: BLE001 - never block webhook ack on persistence
        logger.error(
            "Customer/conversation/inbound persistence failed tenant=%s error=%s",
            tenant_id,
            type(error).__name__,
        )
    _touch_conversation(settings, tenant_id, conversation)

    # --- per-conversation AI control, decided BEFORE any automated work ---------
    # Paused / human-handled conversations get no model call, no tools, no menu and
    # no automated reply. The dispatch boundary rechecks the mode under the row lock
    # immediately before any send, which covers turns already in flight.
    human_asked = human_request_service.is_human_request(event["text"])
    mode, _attention = _automation_state(settings, tenant_id, conversation)
    if mode != "ai_active":
        return _skip_automation(
            settings,
            tenant_id=tenant_id,
            conversation=conversation,
            message_id=event["message_id"],
            mode=mode,
            human_asked=human_asked,
        )
    if human_asked and conversation:
        return _handle_human_request(
            settings,
            tenant_id=tenant_id,
            channel=channel,
            channel_id=channel_id,
            phone_number_id=event["phone_number_id"],
            conversation=conversation,
            customer=customer,
            customer_key=customer_key,
            message_id=event["message_id"],
        )
    # Discounts, special pricing and price negotiation are human decisions (approved
    # Stage 3 decision Q2): pause the AI for this conversation and hand over to staff.
    if conversation and quotation_intent.is_discount_request(event["text"]):
        return _handle_human_request(
            settings,
            tenant_id=tenant_id,
            channel=channel,
            channel_id=channel_id,
            phone_number_id=event["phone_number_id"],
            conversation=conversation,
            customer=customer,
            customer_key=customer_key,
            message_id=event["message_id"],
            source="discount_request",
            requested_product=(
                "Discount or special pricing requested: " + event["text"][:150]
            ),
            acknowledgement=DISCOUNT_ACKNOWLEDGEMENT,
            ack_kind="discount_request_ack",
        )

    # Explicit menu commands: "menu", "order <number>", a legacy option name, or a
    # possibly configured row label. Lowercase ordinary chat stays on the AI path
    # without a settings read; the authoritative label check happens after load.
    if (
        menu_service.is_menu_command(event["text"])
        or menu_service.could_be_configured_label(event["text"])
    ):
        document = menu_service.load_document(settings, tenant_id)
        if menu_service.is_enabled(document) and menu_service.is_menu_command(
            event["text"], document
        ):
            menu_ctx = menu_service.MenuContext(
                settings=settings,
                tenant_id=tenant_id,
                wa_id=customer_key,
                document=document,
                channel=channel,
                phone_number_id=event["phone_number_id"],
                customer=customer,
                conversation=conversation,
                message_id=event["message_id"],
            )
            try:
                menu_reply = menu_service.handle_text(menu_ctx, event["text"])
            except Exception:  # noqa: BLE001 - a menu bug must not break the AI path
                logger.exception("Menu text handling failed tenant=%s", tenant_id)
                menu_reply = None
            if menu_reply is not None:
                outbox_row, outbox_state = _record_menu_outbound(
                    settings,
                    tenant_id,
                    channel_id,
                    str(conversation["id"]) if conversation else None,
                    event["message_id"],
                    menu_reply,
                )
                if outbox_state == "already_sent":
                    existing = (outbox_row or {}).get("provider_message_id")
                    whatsapp_service.update_event(
                        settings,
                        tenant_id,
                        event["message_id"],
                        "processed",
                        response_message_id=existing,
                    )
                    return jsonify(ok=True, menu=True, duplicate_reply=True)
                if outbox_state == "ambiguous":
                    whatsapp_service.update_event(
                        settings, tenant_id, event["message_id"], "failed", "ambiguous_send"
                    )
                    return jsonify(ok=True, ambiguous=True)
                return _dispatch_menu_reply(
                    settings,
                    channel=channel,
                    phone_number_id=event["phone_number_id"],
                    tenant_id=tenant_id,
                    conversation=conversation,
                    customer_key=customer_key,
                    menu_reply=menu_reply,
                    outbox_row=outbox_row,
                    inbound_message_id=event["message_id"],
                )

    # An explicit request for the PDF of an existing quotation. This is handled
    # deterministically (and only when the tenant has opted in and sending works), so
    # the document is built from the persisted quotation rather than model output.
    if saas_quotations.is_pdf_request(event["text"]):
        try:
            document = menu_service.load_document(settings, tenant_id)
            quotations_enabled = saas_quotations.quotations_enabled(
                (document or {}).get("configuration")
            )
        except Exception as error:  # noqa: BLE001 - fall through to the AI path
            logger.error(
                "Quotation settings read failed tenant=%s error=%s",
                tenant_id,
                type(error).__name__,
            )
            quotations_enabled = False
        if quotations_enabled and settings.whatsapp_send_enabled:
            try:
                quotation = saas_quotations.latest_quotation_for_customer(
                    settings, tenant_id, customer_key
                )
            except Exception as error:  # noqa: BLE001 - never fail on a lookup
                logger.error(
                    "Quotation lookup failed tenant=%s error=%s",
                    tenant_id,
                    type(error).__name__,
                )
                quotation = None
            if quotation is not None:
                delivery = _deliver_quotation_pdf(
                    settings,
                    tenant_id=tenant_id,
                    channel=channel,
                    channel_id=channel_id,
                    conversation=conversation,
                    customer_key=customer_key,
                    quotation=quotation,
                    inbound_message_id=event["message_id"],
                    phone_number_id=event["phone_number_id"],
                )
                if delivery["outcome"] in {"sent", "already"}:
                    whatsapp_service.update_event(
                        settings,
                        tenant_id,
                        event["message_id"],
                        "processed",
                        response_message_id=delivery.get("wamid"),
                    )
                else:
                    whatsapp_service.update_event(
                        settings,
                        tenant_id,
                        event["message_id"],
                        "failed",
                        "quotation_document_uncertain"
                        if delivery["outcome"] == "uncertain"
                        else "quotation_document_failed",
                    )
                return jsonify(
                    ok=True,
                    quotation_pdf=True,
                    delivery=delivery["outcome"],
                )

    # Deterministic quotation workflow and delivery enquiries (Stage 3). Each returns a
    # response when it handled the message, or None to let the AI answer it.
    if conversation:
        handled = _quotation_or_delivery_turn(
            settings,
            tenant_id=tenant_id,
            channel=channel,
            channel_id=channel_id,
            phone_number_id=event["phone_number_id"],
            conversation=conversation,
            customer=customer,
            customer_key=customer_key,
            message_id=event["message_id"],
            text=event["text"],
            profile_name=event.get("profile_name"),
        )
        if handled is not None:
            return handled

    try:
        history = whatsapp_service.conversation_history(
            settings, tenant_id, customer_key
        )
        turns = [
            ConversationTurn(str(item.get("role", "")), str(item.get("text", "")))
            for item in history
            if isinstance(item, dict)
        ]
        provider = context().bot_provider
        if provider is None:
            raise ServiceError("The AI provider is not configured.", 503)
        brief = None
        if conversation:
            try:
                brief = saas_quotations.load_brief(
                    settings, tenant_id, str(conversation["id"])
                )
            except Exception as error:  # noqa: BLE001 - quotations may be unbuilt here
                logger.info(
                    "Quotation brief unavailable tenant=%s error=%s",
                    tenant_id,
                    type(error).__name__,
                )
                brief = None
        response = BotResponseEngine(
            TenantBotContextLoader(settings.app_database_url), provider
        ).respond(
            tenant_id,
            event["text"],
            turns,
            channel="whatsapp",
            safe_test_mode=False,
            customer_key=customer_key,
            message_id=event["message_id"],
            conversation_id=str(conversation["id"]) if conversation else None,
            customer_name=(customer or {}).get("name"),
            send_enabled=settings.whatsapp_send_enabled,
            brief=brief,
        )
        handover_outcome: dict = {}
        # A turn that needs a human handover is answered with wording the platform owns,
        # chosen from whether the staff notification actually succeeded. Handover trouble
        # must never silence the bot, so it falls back to the honest apology instead.
        try:
            handover_reply = handover_service.handle_turn(
                settings,
                tenant_id=tenant_id,
                channel=channel,
                phone_number_id=event["phone_number_id"],
                customer_key=customer_key,
                customer=customer,
                conversation=conversation,
                message_id=event["message_id"],
                customer_message=event["text"],
                bot_response=response,
                history=history,
                outcome=handover_outcome,
            )
        except Exception as error:  # noqa: BLE001 - never silence the bot
            logger.error(
                "Handover handling failed tenant=%s error=%s", tenant_id, type(error).__name__
            )
            handover_reply = handover_service.safe_reply(response)
        # Only a real payload or an explicit True flag may replace the model's own text;
        # anything else (an unexpected object on a mocked response) falls through.
        if handover_reply:
            reply_text = handover_reply
        elif isinstance(response.quotation, dict):
            # Platform-owned wording built from the persisted row, never the model's.
            try:
                reply_text = saas_quotations.format_customer_summary(response.quotation)
            except Exception:  # noqa: BLE001
                reply_text = saas_quotations.fallback_customer_summary(
                    response.quotation
                )
        elif response.quotation_failed is True:
            reply_text = QUOTATION_FAILED_TEXT
        else:
            reply_text = response.text
        blocked = getattr(response, "output_blocked", None)
        if isinstance(blocked, str) and blocked and not handover_reply:
            # The engine already replaced the unsafe text with the platform fallback.
            logger.warning(
                "AI OUTPUT BLOCKED tenant=%s conversation=%s inbound=%s reason=%s",
                tenant_id,
                (conversation or {}).get("id"),
                event["message_id"],
                blocked,
            )

        # Record the intent to reply BEFORE sending. The partial unique index on
        # (tenant_id, channel_id, reply_to_provider_message_id) is what makes a second
        # outbound row for the same inbound message impossible, so a redelivery cannot
        # produce a duplicate reply even if the claim table is ever bypassed.
        outbox_row = None
        outbox_supported = True
        if conversation:
            try:
                outbox_row = customer_service.save_message(
                    settings,
                    tenant_id,
                    channel_id,
                    str(conversation["id"]),
                    direction="outbound",
                    sender_type="assistant",
                    message_type="text",
                    body_text=reply_text,
                    payload={"text": reply_text},
                    reply_to_provider_message_id=event["message_id"],
                )
            except customer_service.DuplicateReplyError:
                existing = customer_service.find_outbound_reply(
                    settings, tenant_id, channel_id, event["message_id"]
                )
                already_sent = (existing or {}).get("provider_message_id")
                if already_sent:
                    # The reply was already sent for this inbound message. Finish the
                    # bookkeeping without sending a second one.
                    whatsapp_service.update_event(
                        settings,
                        tenant_id,
                        event["message_id"],
                        "processed",
                        response_message_id=str(already_sent),
                    )
                    return jsonify(
                        ok=True, message_id=str(already_sent), duplicate_reply=True
                    )
                # Ambiguous: an earlier attempt recorded the reply but we never saw a
                # successful send response. A free-form reply is not repeatable, so it is
                # never resent automatically. The row is left for manual reconciliation
                # (docs/OUTBOUND_DELIVERY.md): this code does not send
                # biz_opaque_callback_data yet, so a receipt cannot identify it.
                logger.error(
                    "Ambiguous outbound reply, not resending tenant=%s message_id=%s",
                    tenant_id,
                    event["message_id"],
                )
                whatsapp_service.update_event(
                    settings, tenant_id, event["message_id"], "failed", "ambiguous_send"
                )
                return jsonify(ok=True, ambiguous=True)
            except customer_service.SchemaOutOfDateError:
                # Migration 0035 has not been applied here. Keep the pre-0035 behaviour
                # (persist the outbound row after the send) instead of losing history.
                outbox_supported = False
                logger.warning(
                    "Outbound correlation columns are absent; using pre-0035 persistence. "
                    "Apply migration 0035 to enable reply correlation."
                )
            except Exception as error:  # noqa: BLE001 - never silence the bot
                logger.error(
                    "Outbound intent persistence failed tenant=%s error=%s",
                    tenant_id,
                    type(error).__name__,
                )
        # The model recorded the customer's own request for a person: stop AI for
        # this conversation now, then send the platform-worded confirmation as the
        # one notice allowed after the pause.
        human_notice = bool(
            handover_reply
            and conversation
            and handover_outcome.get("reason") == "customer_request"
        )
        if human_notice:
            try:
                conversation_control.request_human(
                    settings,
                    tenant_id=tenant_id,
                    conversation_id=str(conversation["id"]),
                    source="ai_handover_tool",
                )
            except Exception as error:  # noqa: BLE001 - reply still goes out
                logger.error(
                    "Human request flag failed tenant=%s error=%s",
                    tenant_id,
                    type(error).__name__,
                )
        outcome = outbound_dispatch.dispatch_reply(
            settings,
            tenant_id=tenant_id,
            conversation_id=str(conversation["id"]) if conversation else None,
            message_row_id=str(outbox_row["id"]) if outbox_row else None,
            sender_type="assistant",
            notice=human_notice,
            send=lambda: whatsapp_service.send_text(
                settings,
                phone_number_id=event["phone_number_id"],
                tenant_id=tenant_id,
                credentials_ref=channel.get("credentials_ref"),
                recipient=customer_key,
                text=reply_text,
            ),
        )
        if outcome.kind == "suppressed":
            whatsapp_service.update_event(
                settings, tenant_id, event["message_id"], "failed", "ai_reply_suppressed"
            )
            return jsonify(ok=True, suppressed=True)
        if outcome.kind in ("failed", "unknown"):
            # The disposition is already recorded inside the boundary. Report the
            # observed provider outcome against the event and fail the webhook call
            # exactly as before. Nothing here (or anywhere) resends.
            whatsapp_service.update_event(
                settings,
                tenant_id,
                event["message_id"],
                "failed",
                "ambiguous_send"
                if outcome.kind == "unknown"
                else (outcome.error_type or "ServiceError"),
            )
            return jsonify(ok=False, error=outcome.error_message or "Send failed."), 502
        outbound_id = outcome.wamid
        if not outbox_row and not outbox_supported and conversation:
            # Pre-0035 schema: preserve the original behaviour of recording the outbound
            # message after a successful send. Correlation is unavailable, history is not.
            try:
                customer_service.save_message(
                    settings,
                    tenant_id,
                    channel_id,
                    str(conversation["id"]),
                    direction="outbound",
                    sender_type="assistant",
                    provider_message_id=outbound_id,
                    message_type="text",
                    body_text=reply_text,
                    payload={"text": reply_text},
                    delivery_status="sent",
                )
            except Exception as error:  # noqa: BLE001 - non-fatal
                logger.error(
                    "Outbound message persistence failed tenant=%s error=%s",
                    tenant_id,
                    type(error).__name__,
                )
        # The customer asked for a quotation PDF during the AI turn (tool-driven path).
        # It is sent after the text reply, and the delivery ledger prevents a replay.
        if isinstance(response.quotation_pdf, dict):
            try:
                _deliver_quotation_pdf(
                    settings,
                    tenant_id=tenant_id,
                    channel=channel,
                    channel_id=channel_id,
                    conversation=conversation,
                    customer_key=customer_key,
                    quotation=response.quotation_pdf,
                    inbound_message_id=event["message_id"],
                    phone_number_id=event["phone_number_id"],
                )
            except Exception as error:  # noqa: BLE001 - the text reply already went out
                logger.error(
                    "Quotation document delivery failed tenant=%s error=%s",
                    tenant_id,
                    type(error).__name__,
                )
        if conversation:
            try:
                customer_service.update_conversation_activity(
                    settings, tenant_id, str(conversation["id"])
                )
            except Exception as error:  # noqa: BLE001 - non-fatal
                logger.error(
                    "Conversation activity refresh failed tenant=%s error=%s",
                    tenant_id,
                    type(error).__name__,
                )
        history.extend(
            [
                {"role": "user", "text": event["text"][:1000]},
                {"role": "assistant", "text": reply_text[:1000]},
            ]
        )
        whatsapp_service.save_conversation(
            settings, tenant_id, customer_key, history
        )
        whatsapp_service.update_event(
            settings,
            tenant_id,
            event["message_id"],
            "processed",
            response_message_id=outbound_id,
        )
        return jsonify(ok=True, message_id=outbound_id)
    except ServiceError as error:
        whatsapp_service.update_event(
            settings, str(channel["tenant_id"]), event["message_id"], "failed", type(error).__name__
        )
        return jsonify(ok=False, error=error.message), error.status
    except Exception as error:
        whatsapp_service.update_event(
            settings, str(channel["tenant_id"]), event["message_id"], "failed", "processing_error"
        )
        logger.error(
            "WhatsApp webhook processing failed type=%s", type(error).__name__
        )
        return jsonify(ok=False, error="WhatsApp message processing failed."), 502


def _apply_delivery_statuses(settings, statuses: list[dict[str, str]]) -> None:
    """Apply delivery receipts to the outbound rows they refer to.

    Delivery is Meta's own signal, so this never creates a row and never fails the
    webhook: an unknown wamid or channel is logged and ignored. Matching is by wamid,
    falling back to the outbox row id only if a sender supplied it as
    ``biz_opaque_callback_data``. No caller supplies it yet, so every receipt today is
    matched by wamid, and an outbound row without one needs manual reconciliation
    (see docs/OUTBOUND_DELIVERY.md).
    """
    for item in statuses:
        channel = whatsapp_service.resolve_channel(settings, item["phone_number_id"])
        if channel is None:
            logger.warning("Delivery status for an unknown channel was ignored.")
            continue
        row_id = None
        callback = item.get("biz_opaque_callback_data") or ""
        if callback:
            try:
                row_id = str(uuid.UUID(callback))
            except (ValueError, AttributeError, TypeError):
                row_id = None
        try:
            changed = customer_service.update_delivery_status(
                settings,
                str(channel["tenant_id"]),
                status=item["status"],
                provider_message_id=item["provider_message_id"],
                row_id=row_id,
                error_code=item.get("error_code") or None,
                error_message=item.get("error_message") or None,
            )
        except Exception as error:  # noqa: BLE001 - never fail a status webhook
            logger.error(
                "Delivery status write failed tenant=%s error=%s",
                str(channel["tenant_id"]),
                type(error).__name__,
            )
            continue
        if not changed:
            logger.info("Delivery status matched no message and was ignored.")


# ----------------------------------------------------------------- customers
@admin_bp.get("/tenants/<tenant_id>/customers")
@admin_required
def customer_list(tenant_id):
    _tenant(tenant_id)
    settings = context().settings
    page = max(1, int(request.args.get("page", "1") or "1"))
    page_size = max(1, min(int(request.args.get("page_size", "20") or "20"), 100))
    search = (request.args.get("search") or "").strip() or None
    rows, total = customer_service.list_customers(
        settings, tenant_id, search=search, page=page, page_size=page_size
    )
    pages = (total + page_size - 1) // page_size if total else 1
    return render_template(
        "customer_list.html",
        tenant=g.tenant,
        customers=rows,
        total=total,
        page=page,
        pages=pages,
        page_size=page_size,
        search=search or "",
        tab="customers",
    )


@admin_bp.get("/tenants/<tenant_id>/customers/<customer_id>")
@admin_required
def customer_detail(tenant_id, customer_id):
    _tenant(tenant_id)
    settings = context().settings
    customer = customer_service.get_customer(settings, tenant_id, customer_id)
    if customer is None:
        abort(404)
    page = max(1, int(request.args.get("page", "1") or "1"))
    page_size = max(1, min(int(request.args.get("page_size", "50") or "50"), 200))
    conversations, total = customer_service.list_conversations(
        settings, tenant_id, customer_id=customer_id, page=page, page_size=page_size
    )
    pages = (total + page_size - 1) // page_size if total else 1
    return render_template(
        "customer_detail.html",
        tenant=g.tenant,
        customer=customer,
        conversations=conversations,
        page=page,
        pages=pages,
        page_size=page_size,
        tab="customers",
    )


@admin_bp.get("/tenants/<tenant_id>/conversations/<conversation_id>")
@admin_required
def conversation_detail(tenant_id, conversation_id):
    _tenant(tenant_id)
    settings = context().settings
    conversation = customer_service.get_conversation(settings, tenant_id, conversation_id)
    if conversation is None:
        abort(404)
    page = max(1, int(request.args.get("page", "1") or "1"))
    page_size = max(1, min(int(request.args.get("page_size", "50") or "50"), 200))
    messages, total = customer_service.list_messages(
        settings, tenant_id, str(conversation["id"]), page=page, page_size=page_size
    )
    pages = (total + page_size - 1) // page_size if total else 1
    return render_template(
        "conversation_detail.html",
        tenant=g.tenant,
        conversation=conversation,
        messages=messages,
        page=page,
        pages=pages,
        page_size=page_size,
        tab="customers",
    )


# --------------------------------------------------------------------- auth
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    ctx = context()
    email = (request.form.get("email") or "").strip().lower()[:254]
    ip = request.remote_addr or "unknown"
    if not ctx.limiter.allow(ip, email):
        flash("Too many attempts. Try again later.", "error")
        return render_template("login.html"), 429
    try:
        identity = ctx.authenticator.login(email, request.form.get("password") or "")
        from saas import platform as platform_db  # noqa: PLC0415

        if not platform_db.is_platform_admin(
            identity.user_id, url=ctx.settings.platform_database_url
        ):
            raise AuthError(GENERIC_LOGIN_ERROR)
    except AuthError:
        ctx.limiter.record_failure(ip, email)
        flash(GENERIC_LOGIN_ERROR, "error")
        return render_template("login.html"), 401

    ctx.limiter.reset(ip, email)
    session.clear()
    session.permanent = True
    session["user_id"] = identity.user_id
    session["email"] = identity.email
    session["exp"] = time.time() + ctx.settings.session_minutes * 60
    csrf.rotate()
    return redirect(url_for("admin.overview"))


@auth_bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


# ------------------------------------------------------------------ helpers
def _tenant(tenant_id: str) -> dict:
    tenant = tenant_service.get_tenant(context().settings, g.admin_id, tenant_id)
    g.tenant = tenant
    return tenant


def _act(redirect_endpoint: str, tenant_id: str, success: str, action):
    """Run a service call; flash the outcome; redirect back to the tab."""
    try:
        action()
        flash(success, "ok")
    except ServiceError as error:
        flash(error.message, "error")
    return redirect(url_for(redirect_endpoint, tenant_id=tenant_id))


def _form() -> dict:
    return request.form.to_dict()


def _preferred_tenant_id(
    tenant_id: str | None = None,
    tenant_slug: str | None = None,
    *,
    tenants: list[dict] | None = None,
) -> str | None:
    candidates = []
    if tenant_id:
        candidates.append(("id", tenant_id))
    if tenant_slug:
        candidates.append(("slug", tenant_slug))
    if tenants is None:
        tenants = tenant_service.list_tenants(context().settings, g.admin_id)
    by_id = {str(t["id"]): t for t in tenants}
    by_slug = {str(t["slug"]).lower(): t for t in tenants}
    for _, value in candidates:
        if value and isinstance(value, str):
            exact = by_id.get(value) or by_slug.get(value.lower())
            if exact:
                return str(exact["id"])
    preferred = next((t for t in tenants if str(t.get("slug") or "").lower() == "saas-test"), None)
    if preferred is not None:
        return str(preferred["id"])
    return str(tenants[0]["id"]) if tenants else None


def _playground_histories() -> dict[str, list[dict[str, str]]]:
    histories = session.get("playground_histories")
    return histories if isinstance(histories, dict) else {}


def _playground_carts() -> dict[str, list[dict]]:
    carts = session.get("playground_carts")
    return carts if isinstance(carts, dict) else {}


def _render_playground(
    tenant_id: str | None,
    *,
    form_message: str = "",
    result: dict | None = None,
    panel: str = "chat",
    selected_product_id: str | None = None,
    context_data: dict | None = None,
):
    settings = context().settings
    tenants = tenant_service.list_tenants(settings, g.admin_id)
    selected_id = _preferred_tenant_id(
        tenant_id,
        request.args.get("tenant_slug") or request.form.get("tenant_slug"),
        tenants=tenants,
    )
    if selected_id is None:
        return render_template("bot_playground.html", tenant=None, tenants=[], tab="playground", history=[], result=None, form_message=form_message)
    selected = next((t for t in tenants if str(t["id"]) == str(selected_id)), None)
    if selected is None:
        selected = tenant_service.get_tenant(settings, g.admin_id, selected_id)
    if selected is None:
        return render_template("bot_playground.html", tenant=None, tenants=[], tab="playground", history=[], result=None, form_message=form_message)
    if context_data is None:
        context_data = playground_service.load_tenant_context(settings, selected["id"])
    elif str(context_data["tenant"]["id"]) != str(selected["id"]):
        raise ServiceError("The supplied playground context does not match the selected tenant.")
    history = _playground_histories().get(str(selected["id"]), [])
    cart = playground_service.cart_summary(
        context_data["catalogue"],
        _playground_carts().get(str(selected["id"]), []),
        context_data["settings"].get("currency"),
    )
    return render_template(
        "bot_playground.html",
        tenant=context_data["tenant"],
        tenants=tenants,
        settings=context_data["settings"],
        catalogue=context_data["catalogue"],
        tab="playground",
        history=history,
        result=result,
        form_message=form_message,
        cart=cart,
        panel=panel,
        selected_product_id=selected_product_id,
        selected_tenant_id=str(selected_id),
        checkout=playground_service.checkout_capabilities(context_data["settings"]),
        demo_time=datetime.now().astimezone().strftime("%H:%M"),
    )


def _playground_view_data(
    context_data: dict,
    tenant_id: str,
    *,
    panel: str,
    result: dict | None = None,
    selected_product_id: str | None = None,
) -> dict:
    cart = playground_service.cart_summary(
        context_data["catalogue"],
        _playground_carts().get(str(tenant_id), []),
        context_data["settings"].get("currency"),
    )
    return {
        "tenant": context_data["tenant"],
        "settings": context_data["settings"],
        "catalogue": context_data["catalogue"],
        "history": _playground_histories().get(str(tenant_id), []),
        "cart": cart,
        "checkout": playground_service.checkout_capabilities(context_data["settings"]),
        "panel": panel,
        "result": result,
        "selected_product_id": selected_product_id,
        "demo_time": datetime.now().astimezone().strftime("%H:%M"),
    }


def _perform_playground_action(
    settings,
    tenant: dict,
    context_data: dict,
    form: dict,
) -> dict:
    action = "reset" if form.get("start_new") == "1" else form.get("action") or "message"
    tenant_key = str(tenant["id"])
    catalogue = context_data["catalogue"]
    carts = _playground_carts()
    cart = carts.get(tenant_key, [])
    output = {
        "action": action,
        "panel": "chat",
        "result": None,
        "selected_product_id": None,
        "messages": [],
        "notice": None,
        "reset": False,
    }

    if action == "reset":
        histories = _playground_histories()
        histories.pop(tenant_key, None)
        session["playground_histories"] = histories
        carts.pop(tenant_key, None)
        session["playground_carts"] = carts
        output.update(
            reset=True,
            notice="Started a new demo conversation and cleared its cart.",
        )
        return output
    if action == "browse":
        output["panel"] = "catalogue"
        return output
    if action == "talk":
        return output
    if action == "view_product":
        product_id = form.get("product_id", "")
        playground_service.validate_product(catalogue, product_id)
        output.update(panel="catalogue", selected_product_id=product_id)
        return output
    if action == "add_cart":
        cart = playground_service.add_cart_item(
            catalogue,
            cart,
            form.get("product_id", ""),
            form.get("variant_id", ""),
            form.get("quantity"),
        )
        carts[tenant_key] = cart
        session["playground_carts"] = carts
        output.update(panel="cart", notice="Added to the demo cart.")
        return output
    if action in {"update_cart", "remove_cart"}:
        quantity = 0 if action == "remove_cart" else form.get("quantity")
        cart = playground_service.change_cart_item(
            catalogue,
            cart,
            form.get("product_id", ""),
            form.get("variant_id", ""),
            quantity,
        )
        carts[tenant_key] = cart
        session["playground_carts"] = carts
        output["panel"] = "cart"
        return output
    if action == "clear_cart":
        carts.pop(tenant_key, None)
        session["playground_carts"] = carts
        output.update(panel="cart", notice="Demo cart cleared.")
        return output
    if action == "view_cart":
        output["panel"] = "cart"
        return output
    if action == "checkout":
        output["panel"] = "checkout"
        return output
    if action == "confirm_checkout":
        summary = playground_service.cart_summary(
            catalogue, cart, context_data["settings"].get("currency")
        )
        confirmation = playground_service.demo_confirmation(
            context_data["settings"],
            summary,
            form.get("customer_name"),
            form.get("delivery_details"),
        )
        carts.pop(tenant_key, None)
        session["playground_carts"] = carts
        output.update(
            panel="confirmation",
            result={"mode": "safe-test", "confirmation": confirmation},
        )
        return output
    if action != "message":
        raise ServiceError("Unknown playground action.")

    customer_message = form.get("customer_message", "")
    histories = _playground_histories()
    history = histories.get(tenant_key, [])
    response = playground_service.respond_to_customer(
        settings,
        context().bot_provider,
        tenant["id"],
        customer_message,
        history,
        context_data.get("bot_context"),
    )
    now = datetime.now().astimezone().strftime("%H:%M")
    messages = (
        {"role": "user", "text": response["customer_message"][:750], "time": now},
        {"role": "assistant", "text": response["response"][:750], "time": now},
    )
    history.extend(messages)
    histories[tenant_key] = history[-4:]
    session["playground_histories"] = histories
    output.update(result=response, messages=list(messages))
    return output


def _playground_json_payload(context_data: dict, tenant_id: str, outcome: dict) -> dict:
    view_data = _playground_view_data(
        context_data,
        tenant_id,
        panel=outcome["panel"],
        result=outcome["result"],
        selected_product_id=outcome["selected_product_id"],
    )
    cart = {key: value for key, value in view_data["cart"].items() if key != "normalized"}
    return {
        "ok": True,
        "action": outcome["action"],
        "messages": outcome["messages"],
        "reset": outcome["reset"],
        "notice": outcome["notice"],
        "panel": outcome["panel"],
        "cart": cart,
        "catalogue": view_data["catalogue"],
        "checkout": view_data["checkout"],
        "confirmation": (
            outcome["result"].get("confirmation") if outcome["result"] else None
        ),
        "chat_html": render_template("_bot_playground_messages.html", **view_data),
        "panel_html": render_template("_bot_playground_panel.html", **view_data),
    }


# ----------------------------------------------------------------- overview
@admin_bp.get("/")
@admin_required
def overview():
    data = tenant_service.overview(context().settings, g.admin_id)
    return render_template("overview.html", data=data)


# ------------------------------------------------------------------ tenants
@admin_bp.get("/tenants")
@admin_required
def tenants():
    status = request.args.get("status")
    if status not in (None, "", "draft", "active", "suspended", "inactive"):
        abort(400)
    rows = tenant_service.list_tenants(context().settings, g.admin_id, status or None)
    return render_template("tenants.html", tenants=rows, status=status)


@admin_bp.route("/tenants/new", methods=["GET", "POST"])
@admin_required
def tenant_new():
    settings = context().settings
    plans = tenant_service.list_plans(settings, g.admin_id)
    if request.method == "POST":
        try:
            tenant_id = tenant_service.create_tenant(settings, g.admin_id, _form())
        except ServiceError as error:
            flash(error.message, "error")
            return render_template("tenant_new.html", plans=plans, form=_form()), error.status
        flash("Tenant created as a draft.", "ok")
        return redirect(url_for("admin.tenant_overview", tenant_id=tenant_id))
    return render_template("tenant_new.html", plans=plans, form={})


@admin_bp.get("/tenants/<tenant_id>")
@admin_required
def tenant_overview(tenant_id):
    tenant = _tenant(tenant_id)
    readiness = tenant_service.readiness(context().settings, g.admin_id, tenant["id"])
    return render_template("tenant_overview.html", tenant=tenant, readiness=readiness, tab="overview")


@admin_bp.post("/tenants/<tenant_id>/status")
@admin_required
def tenant_status(tenant_id):
    tenant = _tenant(tenant_id)
    return _act(
        "admin.tenant_overview", tenant["id"], "Tenant status updated.",
        lambda: tenant_service.change_status(
            context().settings, g.admin_id, tenant["id"], request.form.get("status", ""),
            request.form.get("waive_owner") == "1",
        ),
    )


@admin_bp.route("/tenants/<tenant_id>/profile", methods=["GET", "POST"])
@admin_required
def tenant_profile(tenant_id):
    tenant = _tenant(tenant_id)
    settings = context().settings
    if request.method == "POST":
        return _act("admin.tenant_profile", tenant["id"], "Profile saved.",
                    lambda: tenant_service.update_profile(settings, g.admin_id, tenant["id"], _form()))
    profile = tenant_service.get_profile(settings, tenant["id"])
    return render_template("tenant_profile.html", tenant=tenant, profile=profile, tab="profile")


def _render_rules(
    tenant: dict, *, errors: dict | None = None, submitted: dict | None = None
) -> str:
    """Render the rules tab from the tenant's *stored* configuration.

    ``submitted`` overlays what a rejected save posted, so a failed change keeps the
    operator's edits on screen next to the error instead of silently discarding them.
    """
    settings = context().settings
    stored = bot_config_service.load_configuration(settings, tenant["id"])
    fields = bot_config_service.field_values(stored)
    if submitted:
        fields = {**fields, **{key: value for key, value in submitted.items() if key in fields}}
        if submitted.get("present_menu_options"):
            submitted_options = []
            for index in range(menu_service.MAX_MENU_OPTIONS):
                option = {
                    "id": submitted.get(f"option_id_{index}", ""),
                    "label": submitted.get(f"option_label_{index}", ""),
                    "action": submitted.get(f"option_action_{index}", ""),
                    "enabled": submitted.get(f"option_enabled_{index}") == "1",
                    "position": submitted.get(f"option_position_{index}", ""),
                }
                if any(option[key] for key in ("id", "label", "action")):
                    submitted_options.append(option)
            fields["menu_options"] = submitted_options
    products = catalogue_service.list_catalogue(settings, tenant["id"])
    categories = sorted({p["category"] for p in products if p.get("category")})
    return render_template(
        "tenant_rules.html",
        tenant=tenant,
        documents=tenant_service.mask_documents(stored),
        labels=tenant_service.RULE_DOCUMENTS,
        tab="rules",
        fields=fields,
        categories=categories,
        constants=tenant_service,
        menu_actions=menu_service.MENU_ACTIONS,
        version=bot_config_service.version_of(stored),
        errors=errors or {},
        preview=tenant_service.bot_config_preview(stored),
    )


def _section_payload() -> dict:
    """The submitted section form: repeated controls become lists, uploads join documents."""
    payload = _form()
    for key in bot_config_service.MULTI_VALUE_FIELDS:
        if key in request.form or f"{bot_config_service.PRESENCE_PREFIX}{key}" in request.form:
            payload[key] = request.form.getlist(key)
    if "documents" in payload:
        uploaded = [f.filename for f in request.files.getlist("document_uploads") if f.filename]
        payload["documents"] = sorted(set(payload["documents"]) | set(uploaded))
    return payload


@admin_bp.route("/tenants/<tenant_id>/rules", methods=["GET", "POST"])
@admin_required
def tenant_rules(tenant_id):
    tenant = _tenant(tenant_id)
    settings = context().settings
    if request.method == "POST":
        if request.form.get("form_kind") == "whatsapp_onboarding":
            return _act("admin.tenant_rules", tenant["id"], "Rules and AI configuration saved.",
                        lambda: tenant_service.update_rules_from_form(
                            settings, g.admin_id, tenant["id"], _section_payload()))
        return _act("admin.tenant_rules", tenant["id"], "Rules and AI configuration saved.",
                    lambda: tenant_service.update_rules(settings, g.admin_id, tenant["id"], _form()))
    return _render_rules(tenant)


@admin_bp.get("/tenants/<tenant_id>/rules/config")
@admin_required
def tenant_rules_config(tenant_id):
    """The saved configuration for one tenant: the section forms load their state from this."""
    tenant = _tenant(tenant_id)
    return jsonify(
        ok=True, **bot_config_service.configuration_snapshot(context().settings, tenant["id"])
    )


@admin_bp.route("/tenants/<tenant_id>/rules/<section>", methods=["POST", "PATCH"])
@admin_required
def tenant_rules_section(tenant_id, section):
    """Save one section -- or, with ``focus``, one field -- without touching anything else."""
    tenant = _tenant(tenant_id)
    settings = context().settings
    payload = (
        dict(request.get_json(silent=True) or {}) if request.is_json else _section_payload()
    )
    try:
        stored = bot_config_service.apply_section(
            settings,
            g.admin_id,
            tenant["id"],
            section,
            payload,
            expected_version=payload.get(bot_config_service.VERSION_FIELD) or None,
        )
    except ServiceError as error:
        if request.is_json:
            return jsonify(ok=False, error=error.message), error.status
        if error.status == 404:
            abort(404)
        return _render_rules(tenant, errors={section: error.message}, submitted=payload), error.status
    if request.is_json:
        return jsonify(
            ok=True,
            version=bot_config_service.version_of(stored),
            fields=bot_config_service.field_values(stored),
        )
    flash(f"{bot_config_service.SECTION_LABELS.get(section, 'Configuration')} saved.", "ok")
    return redirect(url_for("admin.tenant_rules", tenant_id=tenant["id"]))


# ---------------------------------------------------------------- handovers
@admin_bp.get("/tenants/<tenant_id>/handovers")
@admin_required
def tenant_handovers(tenant_id):
    """Human-handover requests for one tenant: what staff still have to answer."""
    tenant = _tenant(tenant_id)
    settings = context().settings
    status = (request.args.get("status") or "").strip()
    return render_template(
        "tenant_handovers.html",
        tenant=tenant,
        tab="handovers",
        requests=handover_service.tenant_requests(settings, tenant["id"], status=status or None),
        status=status,
        statuses=handover_service.STAFF_STATUSES,
        summary=handover_service.status_summary(settings, tenant["id"]),
    )


@admin_bp.post("/tenants/<tenant_id>/handovers/<handover_id>/status")
@admin_required
def tenant_handover_status(tenant_id, handover_id):
    tenant = _tenant(tenant_id)
    settings = context().settings
    status = request.form.get("status") or ""
    return _act("admin.tenant_handovers", tenant["id"], "Handover updated.",
                lambda: handover_service.change_status(
                    settings, g.admin_id, tenant["id"], handover_id, status))


@admin_bp.post("/tenants/<tenant_id>/handovers/<handover_id>/notify")
@admin_required
def tenant_handover_notify(tenant_id, handover_id):
    """Retry a staff notification that failed, using the tenant's active channel."""
    tenant = _tenant(tenant_id)
    settings = context().settings
    return _act("admin.tenant_handovers", tenant["id"], "Staff notified.",
                lambda: handover_service.renotify(
                    settings, g.admin_id, tenant, handover_id))


# ---------------------------------------------------------------- catalogue
@admin_bp.get("/tenants/<tenant_id>/catalogue")
@admin_required
def tenant_catalogue(tenant_id):
    tenant = _tenant(tenant_id)
    products = catalogue_service.list_catalogue(context().settings, tenant["id"])
    return render_template("tenant_catalogue.html", tenant=tenant, products=products, tab="catalogue")


@admin_bp.post("/tenants/<tenant_id>/catalogue/products")
@admin_required
def product_create(tenant_id):
    tenant = _tenant(tenant_id)
    return _act("admin.tenant_catalogue", tenant["id"], "Product created.",
                lambda: catalogue_service.create_product(context().settings, g.admin_id, tenant["id"], _form()))


@admin_bp.post("/tenants/<tenant_id>/catalogue/products/<product_id>")
@admin_required
def product_update(tenant_id, product_id):
    tenant = _tenant(tenant_id)
    return _act("admin.tenant_catalogue", tenant["id"], "Product saved.",
                lambda: catalogue_service.update_product(context().settings, g.admin_id, tenant["id"], product_id, _form()))


@admin_bp.post("/tenants/<tenant_id>/catalogue/products/<product_id>/active")
@admin_required
def product_active(tenant_id, product_id):
    tenant = _tenant(tenant_id)
    return _act("admin.tenant_catalogue", tenant["id"], "Product updated.",
                lambda: catalogue_service.set_product_active(
                    context().settings, g.admin_id, tenant["id"], product_id,
                    request.form.get("active") == "1"))


@admin_bp.post("/tenants/<tenant_id>/catalogue/products/<product_id>/variants")
@admin_required
def variant_create(tenant_id, product_id):
    tenant = _tenant(tenant_id)
    return _act("admin.tenant_catalogue", tenant["id"], "Variant created.",
                lambda: catalogue_service.create_variant(context().settings, g.admin_id, tenant["id"], product_id, _form()))


@admin_bp.post("/tenants/<tenant_id>/catalogue/variants/<variant_id>")
@admin_required
def variant_update(tenant_id, variant_id):
    tenant = _tenant(tenant_id)
    return _act("admin.tenant_catalogue", tenant["id"], "Variant saved.",
                lambda: catalogue_service.update_variant(context().settings, g.admin_id, tenant["id"], variant_id, _form()))


@admin_bp.post("/tenants/<tenant_id>/catalogue/variants/<variant_id>/active")
@admin_required
def variant_active(tenant_id, variant_id):
    tenant = _tenant(tenant_id)
    return _act("admin.tenant_catalogue", tenant["id"], "Variant updated.",
                lambda: catalogue_service.set_variant_active(
                    context().settings, g.admin_id, tenant["id"], variant_id,
                    request.form.get("active") == "1"))


@admin_bp.post("/tenants/<tenant_id>/catalogue/variants/<variant_id>/availability")
@admin_required
def variant_availability(tenant_id, variant_id):
    tenant = _tenant(tenant_id)
    return _act(
        "admin.tenant_catalogue",
        tenant["id"],
        "Variant availability updated.",
        lambda: catalogue_service.set_variant_availability(
            context().settings, g.admin_id, tenant["id"], variant_id, _form()
        ),
    )


@admin_bp.get("/tenants/<tenant_id>/catalogue/template.xlsx")
@admin_required
def catalogue_template(tenant_id):
    _tenant(tenant_id)
    return send_file(
        io.BytesIO(catalogue_import_service.template_bytes()),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="catalogue-import-template.xlsx",
    )


@admin_bp.post("/tenants/<tenant_id>/catalogue/import/preview")
@admin_required
def catalogue_import_preview(tenant_id):
    tenant = _tenant(tenant_id)
    upload = request.files.get("workbook")
    if upload is None or not upload.filename:
        flash("Choose an .xlsx workbook to preview.", "error")
        return redirect(url_for("admin.tenant_catalogue", tenant_id=tenant["id"]))
    try:
        preview = catalogue_import_service.preview_import(
            context().settings,
            g.admin_id,
            tenant["id"],
            upload.filename,
            upload.stream.read(catalogue_import_service.MAX_FILE_BYTES + 1),
        )
    except ServiceError as error:
        flash(error.message, "error")
        return redirect(url_for("admin.tenant_catalogue", tenant_id=tenant["id"]))
    return render_template(
        "catalogue_import_preview.html",
        tenant=tenant,
        preview=preview,
        tab="catalogue",
    )


@admin_bp.post("/tenants/<tenant_id>/catalogue/import/<batch_id>/apply")
@admin_required
def catalogue_import_apply(tenant_id, batch_id):
    tenant = _tenant(tenant_id)
    return _act(
        "admin.tenant_catalogue",
        tenant["id"],
        "Catalogue import applied.",
        lambda: catalogue_import_service.apply_import(
            context().settings, g.admin_id, tenant["id"], batch_id
        ),
    )


# --------------------------------------------------------------- quotations
@admin_bp.get("/tenants/<tenant_id>/quotations")
@admin_required
def tenant_quotations(tenant_id):
    tenant = _tenant(tenant_id)
    quotations = quotation_service.list_quotations(context().settings, tenant["id"])
    return render_template(
        "tenant_quotations.html",
        tenant=tenant,
        quotations=quotations,
        tab="quotations",
    )


@admin_bp.route("/tenants/<tenant_id>/quotations/new", methods=["GET", "POST"])
@admin_required
def quotation_create(tenant_id):
    tenant = _tenant(tenant_id)
    if request.method == "POST":
        selections = list(
            zip(
                request.form.getlist("variant_id"),
                request.form.getlist("quantity"),
                strict=False,
            )
        )
        try:
            quotation_id = quotation_service.create_quotation(
                context().settings,
                g.admin_id,
                tenant["id"],
                request.form.get("customer_name"),
                request.form.get("customer_contact"),
                request.form.get("valid_days"),
                selections,
                request.form.get("terms"),
            )
        except ServiceError as error:
            flash(error.message, "error")
        else:
            flash("Quotation issued.", "ok")
            return redirect(
                url_for(
                    "admin.quotation_detail",
                    tenant_id=tenant["id"],
                    quotation_id=quotation_id,
                )
            )
    form_context = quotation_service.quotation_form_context(
        context().settings, tenant["id"]
    )
    return render_template(
        "quotation_form.html",
        tenant=tenant,
        form_context=form_context,
        tab="quotations",
    )


@admin_bp.get("/tenants/<tenant_id>/quotations/<quotation_id>")
@admin_required
def quotation_detail(tenant_id, quotation_id):
    tenant = _tenant(tenant_id)
    quotation = quotation_service.get_quotation(
        context().settings, tenant["id"], quotation_id
    )
    return render_template(
        "quotation_detail.html",
        tenant=tenant,
        quotation=quotation,
        tab="quotations",
    )


@admin_bp.get("/tenants/<tenant_id>/quotations/<quotation_id>.pdf")
@admin_required
def quotation_download(tenant_id, quotation_id):
    tenant = _tenant(tenant_id)
    # The stored, immutable document (0044); rendered and stored on first access for
    # quotations issued before it existed.
    quotation, pdf = quotation_service.document_for(
        context().settings, tenant["id"], quotation_id
    )
    response = send_file(
        io.BytesIO(pdf),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{quotation['quotation_number']}.pdf",
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


# ----------------------------------------------------------------- whatsapp
def _render_whatsapp(tenant: dict):
    channels = channel_service.list_channels(context().settings, tenant["id"])
    return render_template("tenant_whatsapp.html", tenant=tenant, channels=channels, tab="whatsapp")


@admin_bp.get("/tenants/<tenant_id>/whatsapp")
@admin_required
def tenant_whatsapp(tenant_id):
    return _render_whatsapp(_tenant(tenant_id))


@admin_bp.post("/tenants/<tenant_id>/whatsapp/connect")
@admin_required
def channel_connect(tenant_id):
    tenant = _tenant(tenant_id)
    return _act("admin.tenant_whatsapp", tenant["id"], "Channel recorded as pending.",
                lambda: channel_service.connect_channel(context().settings, g.admin_id, tenant, _form()))


@admin_bp.post("/tenants/<tenant_id>/whatsapp/<channel_id>")
@admin_required
def channel_update(tenant_id, channel_id):
    tenant = _tenant(tenant_id)
    return _act("admin.tenant_whatsapp", tenant["id"], "Channel saved.",
                lambda: channel_service.update_channel(
                    context().settings, g.admin_id, tenant["id"], channel_id, _form()))


@admin_bp.post("/tenants/<tenant_id>/whatsapp/<channel_id>/status")
@admin_required
def channel_status(tenant_id, channel_id):
    tenant = _tenant(tenant_id)
    return _act("admin.tenant_whatsapp", tenant["id"], "Channel status updated.",
                lambda: channel_service.set_channel_status(
                    context().settings, g.admin_id, tenant["id"], channel_id,
                    request.form.get("status", "")))


# ------------------------------------------------------------ members etc.
# Onboarding and member management. Every route is platform-admin only; Supabase Auth
# calls run server-side through context().auth_admin and the database enforces the rest
# (membership belongs to the URL's tenant, final active owner, audit in the same transaction).
@admin_bp.route("/tenants/onboard", methods=["GET", "POST"])
@admin_required
def tenant_onboard():
    ctx = context()
    plans = tenant_service.list_plans(ctx.settings, g.admin_id)
    form = _form() if request.method == "POST" else {"owner_mode": "invite"}
    if request.method == "POST":
        try:
            result = members_service.onboard(ctx.settings, ctx.auth_admin, g.admin_id, form)
        except ServiceError as error:
            flash(error.message, "error")
            return render_template(
                "tenant_onboard.html", plans=plans, form=form,
                auth_admin_configured=ctx.settings.auth_admin_configured,
            ), error.status
        if result.invited:
            flash(f"Tenant created as a draft. Invitation sent to {result.owner_email}; "
                  "they become the active owner and set their password from the email.", "ok")
        else:
            flash(f"Tenant created as a draft with {result.owner_email} as its active owner.", "ok")
        return redirect(url_for("admin.tenant_members", tenant_id=result.tenant_id))
    return render_template(
        "tenant_onboard.html", plans=plans, form=form,
        auth_admin_configured=ctx.settings.auth_admin_configured,
    )


@admin_bp.get("/members")
@admin_required
def members():
    settings = context().settings
    filters = {key: request.args.get(key, "") for key in ("q", "tenant_id", "role", "status", "view")}
    view = filters["view"] if filters["view"] in members_service.MEMBER_VIEWS else "all"
    filters["view"] = view
    rows: list = []
    accounts: list = []
    try:
        if view == "unattached":
            accounts = members_service.list_unattached_accounts(settings, g.admin_id, filters["q"])
        else:
            rows = members_service.list_members(settings, g.admin_id, filters)
            if view == "unverified":
                rows = [row for row in rows if not row.get("email")]
    except ServiceError as error:
        flash(error.message, "error")
    tenants = tenant_service.list_tenants(settings, g.admin_id)
    return render_template(
        "members.html", members=rows, accounts=accounts, tenants=tenants, filters=filters,
        roles=members_service.MEMBER_ROLES, statuses=members_service.MEMBER_STATUSES,
    )


@admin_bp.post("/members/link")
@admin_required
def members_link_account():
    """Give an account without a membership access to a tenant (verified by email)."""
    ctx = context()
    form = _form()
    tenant = _tenant(form.get("tenant_id") or "")
    try:
        flash(members_service.add_existing_member(
            ctx.settings, ctx.auth_admin, g.admin_id, tenant["id"], form), "ok")
    except ServiceError as error:
        flash(error.message, "error")
    return redirect(url_for("admin.members", view="unattached"))


def _members_return(tenant_id: str):
    """Back to the page the action came from: the global list or the tenant's tab."""
    if request.form.get("return_to") == "members":
        return redirect(url_for("admin.members"))
    return redirect(url_for("admin.tenant_members", tenant_id=tenant_id))


def _member_act(tenant_id: str, action):
    try:
        flash(action(), "ok")
    except ServiceError as error:
        flash(error.message, "error")
    return _members_return(tenant_id)


@admin_bp.route("/tenants/<tenant_id>/members", methods=["GET", "POST"])
@admin_required
def tenant_members(tenant_id):
    tenant = _tenant(tenant_id)
    ctx = context()
    if request.method == "POST":
        return _member_act(tenant["id"], lambda: members_service.add_existing_member(
            ctx.settings, ctx.auth_admin, g.admin_id, tenant["id"], _form()))
    rows = members_service.list_members(ctx.settings, g.admin_id, {"tenant_id": tenant["id"]})
    return render_template(
        "tenant_members.html", tenant=tenant, members=rows, tab="members",
        roles=members_service.MEMBER_ROLES,
        auth_admin_configured=ctx.settings.auth_admin_configured,
    )


@admin_bp.post("/tenants/<tenant_id>/members/invite")
@admin_required
def tenant_member_invite(tenant_id):
    tenant = _tenant(tenant_id)
    ctx = context()
    return _member_act(tenant["id"], lambda: members_service.invite_member(
        ctx.settings, ctx.auth_admin, g.admin_id, tenant["id"], _form()))


@admin_bp.post("/tenants/<tenant_id>/members/<membership_id>/role")
@admin_required
def tenant_member_role(tenant_id, membership_id):
    tenant = _tenant(tenant_id)
    return _member_act(tenant["id"], lambda: members_service.change_role(
        context().settings, g.admin_id, tenant["id"], membership_id, request.form.get("role")))


@admin_bp.post("/tenants/<tenant_id>/members/<membership_id>/status")
@admin_required
def tenant_member_status(tenant_id, membership_id):
    tenant = _tenant(tenant_id)
    return _member_act(tenant["id"], lambda: members_service.set_status(
        context().settings, g.admin_id, tenant["id"], membership_id, request.form.get("status")))


@admin_bp.post("/tenants/<tenant_id>/members/<membership_id>/remove")
@admin_required
def tenant_member_remove(tenant_id, membership_id):
    tenant = _tenant(tenant_id)
    if request.form.get("confirm") != "revoke":
        flash("Tick the confirmation box to revoke access.", "error")
        return _members_return(tenant["id"])
    return _member_act(tenant["id"], lambda: members_service.remove(
        context().settings, g.admin_id, tenant["id"], membership_id))


@admin_bp.post("/tenants/<tenant_id>/members/<membership_id>/resend-invite")
@admin_required
def tenant_member_resend(tenant_id, membership_id):
    tenant = _tenant(tenant_id)
    ctx = context()
    return _member_act(tenant["id"], lambda: members_service.resend_invitation(
        ctx.settings, ctx.auth_admin, g.admin_id, tenant["id"], membership_id))


@admin_bp.post("/tenants/<tenant_id>/members/<membership_id>/password-reset")
@admin_required
def tenant_member_password_reset(tenant_id, membership_id):
    tenant = _tenant(tenant_id)
    ctx = context()
    return _member_act(tenant["id"], lambda: members_service.send_password_reset(
        ctx.settings, ctx.auth_admin, g.admin_id, tenant["id"], membership_id))


@admin_bp.route("/tenants/<tenant_id>/subscription", methods=["GET", "POST"])
@admin_required
def tenant_subscription(tenant_id):
    tenant = _tenant(tenant_id)
    settings = context().settings
    if request.method == "POST":
        return _act("admin.tenant_subscription", tenant["id"], "Subscription saved.",
                    lambda: tenant_config.set_subscription(settings, g.admin_id, tenant["id"], _form()))
    data = tenant_config.get_subscription(settings, tenant["id"])
    return render_template("tenant_subscription.html", tenant=tenant, data=data,
                           statuses=tenant_config.SUBSCRIPTION_STATUSES, tab="subscription")


@admin_bp.get("/tenants/<tenant_id>/audit")
@admin_required
def tenant_audit(tenant_id):
    tenant = _tenant(tenant_id)
    rows = tenant_service.tenant_audit(context().settings, tenant["id"])
    return render_template("tenant_audit.html", tenant=tenant, rows=rows, tab="audit")


@admin_bp.route("/playground", methods=["GET", "POST"])
@admin_required
def bot_playground():
    request_started = time.perf_counter()
    settings = context().settings
    if request.method == "POST":
        tenant_id = request.form.get("tenant_id")
        if not tenant_id:
            abort(400)
        tenant = tenant_service.get_tenant(settings, g.admin_id, tenant_id)
        if tenant is None:
            abort(404)
        customer_message = request.form.get("customer_message", "")
        context_data = None
        try:
            context_data = playground_service.load_tenant_context(settings, tenant["id"])
            outcome = _perform_playground_action(
                settings, tenant, context_data, request.form.to_dict()
            )
            if outcome["notice"]:
                flash(outcome["notice"], "ok")
            if outcome["reset"]:
                return redirect(url_for("admin.bot_playground", tenant_id=tenant["id"]))
            rendered = _render_playground(
                tenant["id"],
                result=outcome["result"],
                panel=outcome["panel"],
                selected_product_id=outcome["selected_product_id"],
                context_data=context_data,
            )
            if outcome["action"] == "message":
                logger.info(
                    "Playground request timing total_ms=%.1f outcome=success",
                    (time.perf_counter() - request_started) * 1_000,
                )
            return rendered
        except ServiceError as error:
            flash(error.message, "error")
            rendered = _render_playground(
                tenant["id"],
                form_message=customer_message,
                panel=request.form.get("panel", "chat"),
                context_data=context_data,
            )
            if (request.form.get("action") or "message") == "message":
                logger.info(
                    "Playground request timing total_ms=%.1f outcome=error status=%d",
                    (time.perf_counter() - request_started) * 1_000,
                    error.status,
                )
            return rendered, error.status
    return _render_playground(request.args.get("tenant_id"), form_message="")


@admin_bp.post("/playground/api")
@admin_required
def bot_playground_api():
    request_started = time.perf_counter()
    settings = context().settings
    tenant_id = request.form.get("tenant_id")
    if not tenant_id:
        return jsonify(ok=False, error="Tenant is required."), 400
    try:
        tenant = tenant_service.get_tenant(settings, g.admin_id, tenant_id)
        if tenant is None:
            return jsonify(ok=False, error="Tenant not found."), 404
        context_data = playground_service.load_tenant_context(settings, tenant["id"])
        outcome = _perform_playground_action(
            settings, tenant, context_data, request.form.to_dict()
        )
        payload = _playground_json_payload(context_data, tenant["id"], outcome)
        if outcome["action"] == "message":
            logger.info(
                "Playground request timing total_ms=%.1f outcome=success transport=json",
                (time.perf_counter() - request_started) * 1_000,
            )
        return jsonify(payload)
    except ServiceError as error:
        logger.info(
            "Playground request timing total_ms=%.1f outcome=error status=%d transport=json",
            (time.perf_counter() - request_started) * 1_000,
            error.status,
        )
        return jsonify(ok=False, error=error.message), error.status


@admin_bp.get("/tenants/<tenant_id>/playground")
@admin_required
def tenant_bot_playground(tenant_id):
    return _render_playground(tenant_id, form_message="")


# ------------------------------------------------------- WhatsApp menu simulator
MENU_SIMULATOR_SESSION = "menu_simulator_states"


def _menu_simulator_states() -> dict:
    states = session.get(MENU_SIMULATOR_SESSION)
    return states if isinstance(states, dict) else {}


def _menu_simulator_key(tenant_id: str) -> str:
    return f"{g.admin_id}:{tenant_id}"


def _menu_simulator_signature(document: dict) -> str:
    """A stable signature so a saved builder change refreshes the simulator."""
    return repr(menu_service.menu_config(document))


def _new_menu_simulator_state(settings, tenant_id: str) -> tuple[bool, dict]:
    enabled, document = menu_simulator_service.status(settings, tenant_id)
    signature = _menu_simulator_signature(document)
    if not enabled:
        return False, {
            "enabled": False,
            "signature": signature,
            "messages": [
                {
                    "role": "assistant",
                    "text": menu_simulator_service.disabled_message(tenant_id),
                    "reply": None,
                }
            ],
        }
    reply = menu_service.main_menu(document)
    return True, {
        "enabled": True,
        "signature": signature,
        "messages": [
            {
                "role": "assistant",
                "text": reply.text,
                "reply": menu_simulator_service.reply_view(reply),
            }
        ],
    }


def _render_menu_simulator(
    tenant_id: str | None,
    *,
    state: dict | None = None,
    error: str | None = None,
):
    settings = context().settings
    tenants = tenant_service.list_tenants(settings, g.admin_id)
    selected_id = _preferred_tenant_id(
        tenant_id,
        request.args.get("tenant_slug") or request.form.get("tenant_slug"),
        tenants=tenants,
    )
    if selected_id is None:
        return render_template(
            "menu_simulator.html",
            tenants=[],
            tenant=None,
            selected_tenant_id=None,
            menu_enabled=False,
            state={"messages": []},
            error=error,
        )
    tenant = next((entry for entry in tenants if str(entry["id"]) == str(selected_id)), None)
    if tenant is None:
        tenant = tenant_service.get_tenant(settings, g.admin_id, selected_id)
    if tenant is None:
        return render_template(
            "menu_simulator.html",
            tenants=tenants,
            tenant=None,
            selected_tenant_id=None,
            menu_enabled=False,
            state={"messages": []},
            error=error,
        )
    enabled, document = menu_simulator_service.status(settings, tenant["id"])
    signature = _menu_simulator_signature(document)
    if state is not None and (
        bool(state.get("enabled")) != enabled or state.get("signature") != signature
    ):
        state = None
    if state is None:
        states = _menu_simulator_states()
        key = _menu_simulator_key(str(tenant["id"]))
        state = states.get(key)
        if (
            not state
            or bool(state.get("enabled")) != enabled
            or state.get("signature") != signature
        ):
            _, state = _new_menu_simulator_state(settings, tenant["id"])
            states[key] = state
            session[MENU_SIMULATOR_SESSION] = states
    return render_template(
        "menu_simulator.html",
        tenants=tenants,
        tenant=tenant,
        selected_tenant_id=str(selected_id),
        menu_enabled=enabled,
        state=state,
        error=error,
    )


@admin_bp.route("/playground/menu", methods=["GET", "POST"])
@admin_required
def menu_simulator():
    if request.method == "GET":
        return _render_menu_simulator(request.args.get("tenant_id"))

    tenant_id = request.form.get("tenant_id", "")
    try:
        tenant = _tenant(tenant_id)
        settings = context().settings
        enabled, document = menu_simulator_service.status(settings, tenant["id"])
        if not enabled:
            _, state = _new_menu_simulator_state(settings, tenant["id"])
            return _render_menu_simulator(tenant["id"], state=state)

        states = _menu_simulator_states()
        key = _menu_simulator_key(str(tenant["id"]))
        state = states.get(key)
        if not state or not state.get("enabled"):
            _, state = _new_menu_simulator_state(settings, tenant["id"])

        action = request.form.get("action") or "text"
        if action == "reset":
            _, state = _new_menu_simulator_state(settings, tenant["id"])
        elif action == "text":
            text = (request.form.get("customer_message") or "").strip()[:2000]
            if not text:
                raise ServiceError("Type a customer message first.")
            state["messages"].append({"role": "customer", "text": text, "reply": None})
            menu_context = menu_simulator_service.make_context(
                settings,
                tenant["id"],
                g.admin_id,
                document,
                message_id=f"sim-{uuid.uuid4().hex}",
                include_identity=True,
            )
            reply = menu_service.handle_text(menu_context, text)
            if reply is None:
                state["messages"].append(
                    {
                        "role": "assistant",
                        "text": (
                            "That isn't a menu command. Reply MENU or choose one of "
                            "the options shown above."
                        ),
                        "reply": None,
                    }
                )
            else:
                state["messages"].append(
                    {
                        "role": "assistant",
                        "text": reply.text,
                        "reply": menu_simulator_service.reply_view(reply),
                    }
                )
        else:
            action_id = (request.form.get("action_id") or "").strip()
            if not action_id:
                raise ServiceError("Choose a menu option first.", 400)
            action_title = (
                request.form.get("action_title") or "Menu option"
            ).strip()[:100]
            state["messages"].append(
                {"role": "customer", "text": action_title, "reply": None}
            )
            menu_context = menu_simulator_service.make_context(
                settings,
                tenant["id"],
                g.admin_id,
                document,
                message_id=f"sim-{uuid.uuid4().hex}",
                include_identity=True,
            )
            reply = menu_service.handle_action(menu_context, action_id)
            if reply is None:
                state["messages"].append(
                    {
                        "role": "assistant",
                        "text": (
                            "That menu option is no longer available. Reply MENU to "
                            "start again."
                        ),
                        "reply": None,
                    }
                )
            else:
                state["messages"].append(
                    {
                        "role": "assistant",
                        "text": reply.text,
                        "reply": menu_simulator_service.reply_view(reply),
                    }
                )

        state["messages"] = state["messages"][-20:]
        states[key] = state
        session[MENU_SIMULATOR_SESSION] = states
        return _render_menu_simulator(tenant["id"], state=state)
    except ServiceError as error:
        return _render_menu_simulator(tenant_id, error=error.message), error.status


def register_routes(app: Flask) -> None:
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(whatsapp_bp)
