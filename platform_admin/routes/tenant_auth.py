"""Tenant portal sign-in, sign-out and tenant selection.

The portal shares the Flask app and signed session cookie with the admin console
but uses the disjoint ``t_*`` session namespace (see ``platform_admin.authz``).
Nothing here calls ``session.clear``: tenant sign-in and sign-out touch only
tenant keys, so a concurrent platform-admin session survives. Every failure mode
returns the same generic message, so a caller can never learn whether an email,
account or membership exists. No Supabase token is ever kept: the server verifies
the credentials and stores only the verified identity in the signed cookie.
"""

from __future__ import annotations

import time
import uuid

from flask import (
    Blueprint,
    abort,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from saas import tenant_membership

from .. import csrf
from ..auth import AuthError, LoginRateLimiter
from ..authz import (
    clear_tenant_session,
    context,
    tenant_session_required,
    tenant_session_user_id,
)

tenant_auth_bp = Blueprint("tenant_auth", __name__)

#: One generic error for every failed sign-in: wrong password, unknown account,
#: no tenant membership, malformed input. Never hints at which one occurred.
GENERIC_LOGIN_ERROR = "Invalid email or password."
THROTTLE_ERROR = "Too many sign-in attempts. Please try again later."

#: A separate limiter instance from the admin console's, so tenant sign-in
#: attempts cannot throttle admin sign-in (and vice versa).
_TENANT_LIMITER = LoginRateLimiter()


@tenant_auth_bp.get("/t/login")
def login():
    if tenant_session_user_id() is not None:
        return redirect(url_for("tenant_dashboard.home"))
    return render_template("tenant/login.html", error=None)


@tenant_auth_bp.post("/t/login")
def login_submit():
    if tenant_session_user_id() is not None:
        return redirect(url_for("tenant_dashboard.home"))
    settings = context().settings
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    ip = request.remote_addr or "unknown"
    if not _TENANT_LIMITER.allow(ip, email):
        return render_template("tenant/login.html", error=THROTTLE_ERROR), 429
    if not email or not password:
        _TENANT_LIMITER.record_failure(ip, email)
        return render_template("tenant/login.html", error=GENERIC_LOGIN_ERROR), 401
    try:
        identity = context().authenticator.login(email, password)
    except AuthError:
        _TENANT_LIMITER.record_failure(ip, email)
        return render_template("tenant/login.html", error=GENERIC_LOGIN_ERROR), 401
    # Membership listing uses the server-verified identity only. A database
    # failure is a 503 (fail closed); zero memberships get exactly the same
    # generic error as a wrong password.
    try:
        memberships = tenant_membership.list_my_active_tenants(
            identity.user_id, url=settings.app_database_url
        )
    except Exception:  # noqa: BLE001 - fail closed when the database is unreachable
        abort(503)
    if not memberships:
        _TENANT_LIMITER.record_failure(ip, email)
        return render_template("tenant/login.html", error=GENERIC_LOGIN_ERROR), 401
    # Discard any pre-login tenant state before writing verified values
    # (session-fixation hygiene), keep the admin namespace, rotate the CSRF token.
    clear_tenant_session()
    session["t_user_id"] = identity.user_id
    session["t_email"] = identity.email or ""
    session["t_exp"] = int(time.time() + settings.session_minutes * 60)
    csrf.rotate()
    _TENANT_LIMITER.reset(ip, email)
    if len(memberships) == 1:
        session["t_tenant_id"] = memberships[0]["tenant_id"]
        return redirect(
            url_for("tenant_dashboard.tenant_home", tenant_id=memberships[0]["tenant_id"])
        )
    return redirect(url_for("tenant_dashboard.home"))


@tenant_auth_bp.post("/t/logout")
def logout():
    clear_tenant_session()
    csrf.rotate()
    return redirect(url_for("tenant_auth.login"))


@tenant_auth_bp.get("/t/select")
@tenant_session_required
def select_tenant():
    memberships = tenant_membership.list_my_active_tenants(
        g.tenant_user_id, url=context().settings.app_database_url
    )
    if not memberships:
        # Membership revoked mid-session: drop the tenant session entirely.
        clear_tenant_session()
        return redirect(url_for("tenant_auth.login"))
    return render_template("tenant/select.html", memberships=memberships)


@tenant_auth_bp.post("/t/select")
@tenant_session_required
def select_tenant_submit():
    raw = (request.form.get("tenant_id") or "").strip()
    try:
        uuid.UUID(raw)
    except ValueError:
        abort(400, description="Invalid tenant selection.")
    # The form's tenant id is only trusted after a server-side active-membership
    # check; a non-member (or revoked member) can never select that tenant.
    membership = tenant_membership.membership_for(
        g.tenant_user_id, raw, url=context().settings.app_database_url
    )
    if membership is None:
        abort(403)
    session["t_tenant_id"] = raw
    return redirect(url_for("tenant_dashboard.tenant_home", tenant_id=raw))