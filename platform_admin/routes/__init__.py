"""HTTP routes. No SQL and no business rules here: routes call services."""

from __future__ import annotations

import logging
import io
import time
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
from ..services import handover as handover_service
from ..services import playground as playground_service
from ..services import quotations as quotation_service
from ..services import tenant_config
from ..services import tenants as tenant_service
from ..services import whatsapp as whatsapp_service
from ..services.validation import ServiceError
from saas.bot import BotResponseEngine, ConversationTurn, TenantBotContextLoader

auth_bp = Blueprint("auth", __name__)
admin_bp = Blueprint("admin", __name__)
whatsapp_bp = Blueprint("whatsapp", __name__)

GENERIC_LOGIN_ERROR = "Invalid email or password."
logger = logging.getLogger("platform_admin.playground")


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


@whatsapp_bp.post("/webhooks/whatsapp")
def whatsapp_webhook():
    settings = context().settings
    raw_body = request.get_data(cache=True)
    if not whatsapp_service.verify_signature(
        raw_body, request.headers.get("X-Hub-Signature-256"), settings.whatsapp_app_secret
    ):
        return jsonify(ok=False, error="Invalid webhook signature."), 403
    try:
        event = whatsapp_service.parse_event(request.get_json(silent=True))
    except (TypeError, ValueError):
        event = None
    if event is None:
        return jsonify(ok=True, ignored=True)
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
        )
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
            )
        except Exception as error:  # noqa: BLE001 - never silence the bot
            logger.error(
                "Handover handling failed tenant=%s error=%s", tenant_id, type(error).__name__
            )
            handover_reply = handover_service.safe_reply(response)
        reply_text = handover_reply or response.text
        outbound_id = whatsapp_service.send_text(
            settings,
            phone_number_id=event["phone_number_id"],
            tenant_id=tenant_id,
            credentials_ref=channel.get("credentials_ref"),
            recipient=customer_key,
            text=reply_text,
        )
        # Persist the outbound message only after a successful send.
        if conversation:
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
                customer_service.update_conversation_activity(
                    settings, tenant_id, str(conversation["id"])
                )
            except Exception as error:  # noqa: BLE001 - non-fatal
                logger.error(
                    "Outbound message persistence failed tenant=%s error=%s",
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
            settings, tenant_id, event["message_id"], "processed"
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
    quotation = quotation_service.get_quotation(
        context().settings, tenant["id"], quotation_id
    )
    return send_file(
        io.BytesIO(quotation_service.quotation_pdf(quotation)),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{quotation['quotation_number']}.pdf",
    )


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
@admin_bp.route("/tenants/<tenant_id>/members", methods=["GET", "POST"])
@admin_required
def tenant_members(tenant_id):
    tenant = _tenant(tenant_id)
    settings = context().settings
    if request.method == "POST":
        return _act("admin.tenant_members", tenant["id"], "Member added.",
                    lambda: tenant_config.add_member(settings, g.admin_id, tenant["id"], _form()))
    members = tenant_config.list_members(settings, tenant["id"])
    return render_template("tenant_members.html", tenant=tenant, members=members, tab="members")


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


def register_routes(app: Flask) -> None:
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(whatsapp_bp)
