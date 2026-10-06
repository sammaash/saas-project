"""HTTP routes. No SQL and no business rules here: routes call services."""

from __future__ import annotations

import time

from flask import (
    Blueprint,
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .. import csrf
from ..auth import AuthError
from ..authz import admin_required, context
from ..services import catalogue as catalogue_service
from ..services import channels as channel_service
from ..services import tenant_config
from ..services import tenants as tenant_service
from ..services.validation import ServiceError

auth_bp = Blueprint("auth", __name__)
admin_bp = Blueprint("admin", __name__)

GENERIC_LOGIN_ERROR = "Invalid email or password."


# --------------------------------------------------------------------- auth
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    ctx = context()
    email = (request.form.get("email") or "").strip().lower()
    key = f"{request.remote_addr}|{email}"
    if not ctx.limiter.allow(key):
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
        ctx.limiter.record_failure(key)
        flash(GENERIC_LOGIN_ERROR, "error")
        return render_template("login.html"), 401

    ctx.limiter.reset(key)
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


@admin_bp.route("/tenants/<tenant_id>/rules", methods=["GET", "POST"])
@admin_required
def tenant_rules(tenant_id):
    import json  # noqa: PLC0415

    tenant = _tenant(tenant_id)
    settings = context().settings
    if request.method == "POST":
        return _act("admin.tenant_rules", tenant["id"], "Rules and AI configuration saved.",
                    lambda: tenant_service.update_rules(settings, g.admin_id, tenant["id"], _form()))
    profile = tenant_service.get_profile(settings, tenant["id"])
    documents = {
        column: json.dumps(profile["settings"][column], indent=2, sort_keys=True)
        for column, _label in tenant_service.RULE_DOCUMENTS
    }
    return render_template(
        "tenant_rules.html", tenant=tenant, documents=documents,
        labels=tenant_service.RULE_DOCUMENTS, tab="rules",
    )


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


# ----------------------------------------------------------------- whatsapp
def _render_whatsapp(tenant: dict, **extra):
    channels = channel_service.list_channels(context().settings, tenant["id"])
    return render_template("tenant_whatsapp.html", tenant=tenant, channels=channels, tab="whatsapp", **extra)


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


@admin_bp.post("/tenants/<tenant_id>/whatsapp/<channel_id>/status")
@admin_required
def channel_status(tenant_id, channel_id):
    tenant = _tenant(tenant_id)
    return _act("admin.tenant_whatsapp", tenant["id"], "Channel status updated.",
                lambda: channel_service.set_channel_status(
                    context().settings, g.admin_id, tenant["id"], channel_id,
                    request.form.get("status", "")))


@admin_bp.post("/tenants/<tenant_id>/whatsapp/meta/<operation>")
@admin_required
def channel_meta(tenant_id, operation):
    tenant = _tenant(tenant_id)
    arguments = {k: v for k, v in request.form.items() if k != "csrf_token" and v}
    try:
        result = channel_service.run_meta_operation(
            context().settings, context().meta_provider, g.admin_id, tenant["id"], operation, **arguments
        )
    except ServiceError as error:
        flash(error.message, "error")
        return _render_whatsapp(tenant), error.status
    return _render_whatsapp(tenant, meta_result=result, meta_operation=operation)


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


def register_routes(app: Flask) -> None:
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
