"""Tenant portal dashboard: read-only views of one tenant's operational data.

Every route runs behind ``member_required``/``owner_required``, which re-check
active membership of the URL's tenant against the database on *every* request;
the services themselves stay inside the per-tenant RLS model
(``tenant_id = app.current_tenant_id()``). Views are read-only: this milestone
adds no writes, no invitations, no member management and no messaging.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from saas import quotations as saas_quotations
from saas import tenant_membership

from ..authz import (
    clear_tenant_session,
    context,
    member_required,
    owner_required,
    tenant_session_required,
)
from ..services import (
    catalogue as catalogue_service,
    conversation_control,
    customers as customers_service,
    handover as handover_service,
    outbound_dispatch,
    quotations as quotations_service,
    tenants as tenants_service,
    whatsapp as whatsapp_service,
)
from ..services.validation import ServiceError

tenant_dashboard_bp = Blueprint("tenant_dashboard", __name__)


MESSAGE_PAGE = 50


def _tenant_zone(settings) -> ZoneInfo:
    try:
        name = (tenants_service.get_profile(settings, g.tenant_id).get("settings") or {}).get(
            "timezone"
        )
        return ZoneInfo(str(name or "Africa/Nairobi"))
    except (ZoneInfoNotFoundError, ValueError, TypeError, ServiceError):
        return ZoneInfo("UTC")


def _time_formatter(zone: ZoneInfo):
    """Render stored UTC instants in the tenant's timezone: today -> HH:MM."""
    today = datetime.now(zone).date()

    def fmt(value) -> str:
        if not isinstance(value, datetime):
            return str(value or "")
        local = value.astimezone(zone)
        if local.date() == today:
            return local.strftime("%H:%M")
        if local.year == today.year:
            return local.strftime("%d %b %H:%M")
        return local.strftime("%d %b %Y %H:%M")

    return fmt


def _page_arg() -> int:
    try:
        return max(1, int(request.args.get("page", "1")))
    except ValueError:
        return 1


@tenant_dashboard_bp.get("/t/")
@tenant_session_required
def home():
    """Entry point: one membership goes straight to its tenant, several offer a picker."""
    memberships = tenant_membership.list_my_active_tenants(
        g.tenant_user_id, url=context().settings.app_database_url
    )
    if not memberships:
        # Membership revoked mid-session: drop the tenant session entirely.
        clear_tenant_session()
        return redirect(url_for("tenant_auth.login"))
    if len(memberships) == 1:
        return redirect(
            url_for("tenant_dashboard.tenant_home", tenant_id=memberships[0]["tenant_id"])
        )
    return render_template("tenant/select.html", memberships=memberships)


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/")
@member_required
def tenant_home(tenant_id):
    settings = context().settings
    profile = tenants_service.get_profile(settings, g.tenant_id)
    summary = handover_service.status_summary(settings, g.tenant_id)
    return render_template(
        "tenant/overview.html", profile=profile, summary=summary
    )


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/catalogue")
@member_required
def catalogue(tenant_id):
    products = catalogue_service.list_catalogue(context().settings, g.tenant_id)
    return render_template("tenant/catalogue.html", products=products)


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/enquiries")
@member_required
def enquiries(tenant_id):
    page = _page_arg()
    rows, total = customers_service.list_customers(
        context().settings, g.tenant_id, page=page
    )
    return render_template(
        "tenant/enquiries.html", rows=rows, total=total, page=page, page_size=20
    )


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/enquiries/<uuid:customer_id>")
@member_required
def customer_detail(tenant_id, customer_id):
    customer_id = str(customer_id)
    customer = customers_service.get_customer(
        context().settings, g.tenant_id, customer_id
    )
    if customer is None:
        abort(404)
    conversations, _ = customers_service.list_conversations(
        context().settings, g.tenant_id, customer_id=customer_id
    )
    return render_template(
        "tenant/enquiry_detail.html", customer=customer, conversations=conversations
    )


@tenant_dashboard_bp.get(
    "/t/<uuid:tenant_id>/enquiries/<uuid:customer_id>/conversations/<uuid:conversation_id>"
)
@member_required
def conversation_detail(tenant_id, customer_id, conversation_id):
    customer_id = str(customer_id)
    conversation_id = str(conversation_id)
    conversation = customers_service.get_conversation(
        context().settings, g.tenant_id, conversation_id
    )
    # A conversation may only be viewed under its own customer, and only inside
    # this tenant (RLS already returns None for other tenants' rows).
    if conversation is None or str(conversation.get("customer_id")) != customer_id:
        abort(404)
    messages, _older = customers_service.latest_messages(
        context().settings, g.tenant_id, conversation_id, limit=MESSAGE_PAGE
    )
    return render_template(
        "tenant/conversation_detail.html", conversation=conversation, messages=messages
    )


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/quotations")
@member_required
def quotations(tenant_id):
    rows = quotations_service.list_quotations(context().settings, g.tenant_id)
    return render_template(
        "tenant/quotations.html", rows=rows, labels=saas_quotations.LIFECYCLE_LABELS
    )


def _quotation_delivery(settings, quotation_id: str) -> tuple[dict, list[dict]]:
    overview = saas_quotations.delivery_overview(settings, g.tenant_id, [quotation_id])
    attempts = saas_quotations.list_deliveries(settings, g.tenant_id, quotation_id)
    return overview.get(quotation_id) or {}, attempts


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/quotations/<uuid:quotation_id>")
@member_required
def quotation_detail(tenant_id, quotation_id):
    settings = context().settings
    quotation = quotations_service.get_quotation(settings, g.tenant_id, str(quotation_id))
    delivery, attempts = _quotation_delivery(settings, quotation["id"])
    lifecycle = saas_quotations.lifecycle_status(
        expiry_date=quotation["expiry_date"],
        delivery_status=delivery.get("delivery_status"),
    )
    return render_template(
        "tenant/quotation_detail.html",
        quotation=quotation,
        delivery=delivery,
        attempts=attempts,
        lifecycle=lifecycle,
        lifecycle_label=saas_quotations.LIFECYCLE_LABELS[lifecycle],
        can_resend=_can_resend(quotation, delivery),
        resend_key=str(uuid.uuid4()),
        sending_enabled=settings.whatsapp_send_enabled,
    )


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/quotations/<uuid:quotation_id>/pdf")
@member_required
def quotation_pdf(tenant_id, quotation_id):
    """View (inline) or download (``?download=1``) the stored quotation PDF.

    Members of this tenant only (owner and staff); another tenant's id is a 404 because
    every read is scoped by ``app.current_tenant_id()``. The stored document is served,
    so staff see exactly what the customer was sent.
    """
    quotation, pdf = quotations_service.document_for(
        context().settings, g.tenant_id, str(quotation_id)
    )
    response = send_file(
        io.BytesIO(pdf),
        mimetype="application/pdf",
        as_attachment=request.args.get("download") == "1",
        download_name=f"{quotation['quotation_number']}.pdf",
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _can_resend(quotation: dict, delivery: dict) -> bool:
    """Only a WhatsApp quotation whose latest attempt DEFINITELY failed and that was
    never sent. A pending or uncertain attempt is never resent from the dashboard."""
    return (
        quotation.get("source") == "whatsapp"
        and bool(quotation.get("customer_wa_id"))
        and bool(delivery)
        and delivery.get("delivery_status") == "failed"
    )


@tenant_dashboard_bp.post("/t/<uuid:tenant_id>/quotations/<uuid:quotation_id>/resend")
@member_required
def quotation_resend(tenant_id, quotation_id):
    settings = context().settings
    if not settings.whatsapp_send_enabled:
        raise ServiceError("Sending is disabled in this environment.", 503)
    try:
        resend_key = str(uuid.UUID(request.form.get("resend_key") or ""))
    except ValueError:
        raise ServiceError("This resend request is not valid. Reload the page.", 400) from None
    quotation = quotations_service.get_quotation(settings, g.tenant_id, str(quotation_id))
    delivery, attempts = _quotation_delivery(settings, quotation["id"])
    if not _can_resend(quotation, delivery):
        raise ServiceError(
            "Only a quotation whose last send definitely failed can be resent. If the "
            "outcome is unknown, check with the customer before sending again.",
            409,
        )
    latest = attempts[0] if attempts else {}
    conversation_id = latest.get("conversation_id")
    channel_id = latest.get("channel_id")
    channel = (
        whatsapp_service.get_channel(settings, g.tenant_id, str(channel_id))
        if channel_id else None
    )
    if not conversation_id or channel is None or not channel.get("phone_number_id"):
        raise ServiceError("This quotation is not linked to a WhatsApp conversation.", 409)
    from . import _deliver_quotation_pdf  # noqa: PLC0415 - avoid a circular import

    outcome = _deliver_quotation_pdf(
        settings,
        tenant_id=g.tenant_id,
        channel=channel,
        channel_id=str(channel_id),
        conversation={"id": str(conversation_id)},
        customer_key=str(quotation["customer_wa_id"]),
        quotation=quotation,
        inbound_message_id=resend_key,
        idempotency_key=f"staff-resend:{resend_key}",
        caption=f"Quotation {quotation['quotation_number']}",
        sender_type="staff",
    )
    messages = {
        "sent": ("Quotation sent on WhatsApp.", "success"),
        "already": ("This resend was already processed.", "info"),
        "uncertain": (
            "WhatsApp did not confirm the send. It will not be retried automatically; "
            "check with the customer.",
            "error",
        ),
    }
    text, category = messages.get(
        outcome.get("outcome"), ("The quotation could not be sent.", "error")
    )
    flash(text, category)
    return redirect(
        url_for("tenant_dashboard.quotation_detail", tenant_id=g.tenant_id,
                quotation_id=quotation_id)
    )


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/handovers")
@member_required
def handovers(tenant_id):
    settings = context().settings
    rows = handover_service.tenant_requests(settings, g.tenant_id)
    summary = handover_service.status_summary(settings, g.tenant_id)
    return render_template("tenant/handovers.html", rows=rows, summary=summary)


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/settings")
@owner_required
def settings(tenant_id):
    profile = tenants_service.get_profile(context().settings, g.tenant_id)
    return render_template("tenant/settings.html", profile=profile)


def _render_inbox(selected_id: str | None):
    settings = context().settings
    search = (request.args.get("q") or "").strip() or None
    status = (request.args.get("status") or "").strip() or None
    view = (request.args.get("view") or "").strip() or None
    page = _page_arg()
    threads, total = customers_service.inbox_threads(
        settings,
        g.tenant_id,
        search=search,
        status=status,
        view=view,
        page=page,
    )
    for thread in threads:
        thread["indicator"] = conversation_control.indicator(
            thread.get("ai_mode"), thread.get("needs_attention")
        )
    selected = None
    messages: list = []
    older = None
    customer = None
    indicator = None
    before = (request.args.get("before") or "").strip() or None
    if selected_id is not None:
        # Cross-tenant ids return None here and on the message queries: 404, never data.
        selected = customers_service.get_conversation(settings, g.tenant_id, selected_id)
        if selected is None:
            abort(404)
        # The NEWEST messages, oldest-first on screen; "Load earlier" pages backwards.
        messages, older = customers_service.latest_messages(
            settings, g.tenant_id, selected_id, limit=MESSAGE_PAGE, before=before
        )
        customer = customers_service.get_customer(
            settings, g.tenant_id, str(selected["customer_id"])
        )
        indicator = conversation_control.indicator(
            selected.get("ai_mode"), selected.get("needs_attention")
        )
    signature = customers_service.inbox_signature(settings, g.tenant_id, selected_id)
    return render_template(
        "tenant/inbox.html",
        threads=threads,
        total=total,
        page=page,
        page_size=20,
        selected=selected,
        messages=messages,
        older=older,
        before=before,
        customer=customer,
        indicator=indicator,
        indicator_labels=conversation_control.INDICATOR_LABELS,
        search=search,
        status=status,
        view=view,
        signature=signature,
        fmt_time=_time_formatter(_tenant_zone(settings)),
        sending_enabled=settings.whatsapp_send_enabled,
    )


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/inbox")
@member_required
def inbox(tenant_id):
    return _render_inbox(None)


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/inbox/<uuid:conversation_id>")
@member_required
def inbox_conversation(tenant_id, conversation_id):
    return _render_inbox(str(conversation_id))


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/inbox/updates")
@member_required
def inbox_updates(tenant_id):
    """Polling endpoint: a change marker only (no message content, no PII)."""
    conversation_id = (request.args.get("c") or "").strip() or None
    if conversation_id is not None:
        try:
            conversation_id = str(uuid.UUID(conversation_id))
        except ValueError:
            abort(400)
    signature = customers_service.inbox_signature(
        context().settings, g.tenant_id, conversation_id
    )
    response = jsonify(signature=signature)
    response.headers["Cache-Control"] = "no-store"
    return response


@tenant_dashboard_bp.post("/t/<uuid:tenant_id>/inbox/<uuid:conversation_id>/mode")
@member_required
def inbox_set_mode(tenant_id, conversation_id):
    new_mode = (request.form.get("mode") or "").strip()
    # Resuming AI must be explicitly confirmed: the AI will answer this customer's
    # next message on its own.
    if new_mode == "ai_active" and request.form.get("confirm") != "yes":
        raise ServiceError("Confirm that the AI should resume for this customer.", 400)
    result = conversation_control.set_mode(
        context().settings,
        actor_user_id=g.tenant_user_id,
        tenant_id=g.tenant_id,
        conversation_id=str(conversation_id),
        new_mode=new_mode,
    )
    flash("Conversation mode updated." if result == "changed" else "Conversation mode unchanged.")
    return redirect(
        url_for("tenant_dashboard.inbox_conversation", tenant_id=g.tenant_id, conversation_id=conversation_id)
    )


@tenant_dashboard_bp.post("/t/<uuid:tenant_id>/inbox/<uuid:conversation_id>/attention")
@member_required
def inbox_set_attention(tenant_id, conversation_id):
    value = (request.form.get("value") or "").strip()
    if value not in ("0", "1"):
        raise ServiceError("Invalid attention flag.", 400)
    result = conversation_control.set_needs_attention(
        context().settings,
        actor_user_id=g.tenant_user_id,
        tenant_id=g.tenant_id,
        conversation_id=str(conversation_id),
        active=value == "1",
    )
    flash("Attention flag updated." if result == "changed" else "Attention flag unchanged.")
    return redirect(
        url_for("tenant_dashboard.inbox_conversation", tenant_id=g.tenant_id, conversation_id=conversation_id)
    )


@tenant_dashboard_bp.post("/t/<uuid:tenant_id>/inbox/<uuid:conversation_id>/reply")
@member_required
def inbox_reply(tenant_id, conversation_id):
    settings = context().settings
    # Sending is rejected BEFORE any write: no mode change, no audit event and no
    # message row are recorded while WhatsApp sending is disabled.
    if not settings.whatsapp_send_enabled:
        raise ServiceError("Sending is disabled in this environment.", 503)
    text = request.form.get("text") or ""
    conversation = customers_service.get_conversation(
        settings, g.tenant_id, str(conversation_id)
    )
    if conversation is None:
        abort(404)
    customer = customers_service.get_customer(
        settings, g.tenant_id, str(conversation["customer_id"])
    )
    channel = whatsapp_service.get_channel(
        settings, g.tenant_id, str(conversation["channel_id"])
    )
    if customer is None or channel is None:
        abort(404)
    recipient = str(customer.get("wa_id") or "")
    phone_number_id = str(channel.get("phone_number_id") or "")
    if not recipient or not phone_number_id:
        raise ServiceError(
            "This conversation is not connected to a WhatsApp channel.", 409
        )
    row = customers_service.add_staff_reply(
        settings,
        tenant_id=g.tenant_id,
        channel_id=str(conversation["channel_id"]),
        conversation_id=str(conversation_id),
        actor_user_id=g.tenant_user_id,
        body_text=text,
    )
    outcome = outbound_dispatch.dispatch_reply(
        settings,
        tenant_id=g.tenant_id,
        conversation_id=str(conversation_id),
        message_row_id=str(row["id"]),
        sender_type="staff",
        send=lambda: whatsapp_service.send_text(
            settings,
            phone_number_id=phone_number_id,
            tenant_id=g.tenant_id,
            credentials_ref=channel.get("credentials_ref"),
            recipient=recipient,
            text=text,
        ),
    )
    if outcome.kind == "sent":
        flash("Reply sent.")
    elif outcome.kind == "failed":
        flash("Reply failed to send.", "error")
    else:
        flash("Reply outcome unknown - pending reconciliation.", "error")
    return redirect(
        url_for("tenant_dashboard.inbox_conversation", tenant_id=g.tenant_id, conversation_id=conversation_id)
    )