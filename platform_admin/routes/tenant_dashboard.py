"""Tenant portal dashboard: read-only views of one tenant's operational data.

Every route runs behind ``member_required``/``owner_required``, which re-check
active membership of the URL's tenant against the database on *every* request;
the services themselves stay inside the per-tenant RLS model
(``tenant_id = app.current_tenant_id()``). Views are read-only: this milestone
adds no writes, no invitations, no member management and no messaging.
"""

from __future__ import annotations

from flask import Blueprint, abort, g, redirect, render_template, request, url_for

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
    customers as customers_service,
    handover as handover_service,
    quotations as quotations_service,
    tenants as tenants_service,
)

tenant_dashboard_bp = Blueprint("tenant_dashboard", __name__)


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
    messages, _ = customers_service.list_messages(
        context().settings, g.tenant_id, conversation_id
    )
    return render_template(
        "tenant/conversation_detail.html", conversation=conversation, messages=messages
    )


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/quotations")
@member_required
def quotations(tenant_id):
    rows = quotations_service.list_quotations(context().settings, g.tenant_id)
    return render_template("tenant/quotations.html", rows=rows)


@tenant_dashboard_bp.get("/t/<uuid:tenant_id>/quotations/<uuid:quotation_id>")
@member_required
def quotation_detail(tenant_id, quotation_id):
    quotation = quotations_service.get_quotation(
        context().settings, g.tenant_id, str(quotation_id)
    )
    return render_template("tenant/quotation_detail.html", quotation=quotation)


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