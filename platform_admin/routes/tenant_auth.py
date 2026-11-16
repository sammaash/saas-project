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

import logging
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

from saas import password_reset, tenant_membership

from .. import csrf
from ..auth import AuthError, LoginRateLimiter
from ..auth_admin import AuthAdminError, auth_callback_url, check_password, normalize_email
from ..authz import (
    clear_tenant_session,
    context,
    tenant_session_required,
    tenant_session_user_id,
)

tenant_auth_bp = Blueprint("tenant_auth", __name__)
logger = logging.getLogger("platform_admin.tenant_auth")

#: One generic error for every failed sign-in: wrong password, unknown account,
#: no tenant membership, malformed input. Never hints at which one occurred.
GENERIC_LOGIN_ERROR = "Invalid email or password."
THROTTLE_ERROR = "Too many sign-in attempts. Please try again later."

#: A separate limiter instance from the admin console's, so tenant sign-in
#: attempts cannot throttle admin sign-in (and vice versa).
_TENANT_LIMITER = LoginRateLimiter()
#: Password-reset requests are throttled separately, per IP and email.
_RESET_LIMITER = LoginRateLimiter()

#: Session keys for the short window between opening an invitation (or reset) link and
#: choosing a password. They hold only the verified user id and email, never a token.
PASSWORD_SETUP_KEYS = ("t_pw_user_id", "t_pw_email", "t_pw_exp")
PASSWORD_SETUP_SECONDS = 15 * 60
LINK_ERROR = "This link is invalid or has expired. Ask for a new invitation or password reset."
#: Every reset request takes at least this long, so response time does not reveal
#: whether an email was actually sent.
RESET_RESPONSE_FLOOR_SECONDS = 1.0
RESET_SENT_MESSAGE = (
    "If an account exists for that email, we have sent a link to choose a new password."
)


def _clear_password_setup() -> None:
    for key in PASSWORD_SETUP_KEYS:
        session.pop(key, None)


def _password_setup_user() -> str | None:
    user_id = session.get("t_pw_user_id")
    exp = session.get("t_pw_exp", 0)
    if not isinstance(user_id, str) or not isinstance(exp, (int, float)) or exp < time.time():
        _clear_password_setup()
        return None
    return user_id


@tenant_auth_bp.get("/t/login")
def login():
    if tenant_session_user_id() is not None:
        return redirect(url_for("tenant_dashboard.home"))
    notice = (
        "Your password is set. Sign in with your email and new password."
        if request.args.get("password") == "set" else None
    )
    return render_template("tenant/login.html", error=None, notice=notice)


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

# ---------------------------------------------------------------------------
# Invitation acceptance, password setup and password reset
# ---------------------------------------------------------------------------
# Supabase sends invitation and reset links to /t/auth/callback with the access token in the
# URL fragment, which never reaches a server. A same-origin script (static/auth_callback.js)
# posts it here once, with the CSRF token, and removes it from the address bar. The server
# verifies it, keeps only the verified user id for 15 minutes, and discards the token.
# Opening a link grants no tenant access: the person must still choose a password and sign
# in, and sign-in still requires an active membership.

@tenant_auth_bp.get("/t/auth/callback")
def auth_callback():
    return render_template("tenant/auth_callback.html", error=None)


@tenant_auth_bp.post("/t/auth/callback")
def auth_callback_submit():
    token = (request.form.get("access_token") or "").strip()
    if not token or len(token) > 8192:
        return render_template("tenant/auth_callback.html", error=LINK_ERROR), 400
    try:
        # Only a fresh invitation/recovery/OTP token (amr) is accepted here; an ordinary
        # password-session token can never be used to set a new password.
        identity = context().authenticator.verify_link_token(token)
    except AuthError:
        return render_template("tenant/auth_callback.html", error=LINK_ERROR), 400
    _clear_password_setup()
    session["t_pw_user_id"] = identity.user_id
    session["t_pw_email"] = identity.email or ""
    session["t_pw_exp"] = int(time.time() + PASSWORD_SETUP_SECONDS)
    csrf.rotate()
    return redirect(url_for("tenant_auth.set_password"))


@tenant_auth_bp.route("/t/set-password", methods=["GET", "POST"])
def set_password():
    user_id = _password_setup_user()
    if user_id is None:
        return render_template("tenant/auth_callback.html", error=LINK_ERROR), 400
    email = session.get("t_pw_email", "")
    if request.method == "GET":
        return render_template("tenant/set_password.html", email=email, error=None)
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""
    try:
        check_password(password, confirm)
        context().auth_admin.set_password(user_id, password)
    except AuthAdminError as error:
        return render_template("tenant/set_password.html", email=email, error=str(error)), 400
    _clear_password_setup()
    csrf.rotate()
    return redirect(url_for("tenant_auth.login", password="set"))


@tenant_auth_bp.route("/t/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("tenant/forgot_password.html", notice=None, error=None)
    started = time.monotonic()
    settings = context().settings
    ip = request.remote_addr or "unknown"
    raw_email = (request.form.get("email") or "").strip().lower()
    if not _RESET_LIMITER.allow(ip, raw_email):
        return render_template(
            "tenant/forgot_password.html", notice=None, error=THROTTLE_ERROR
        ), 429
    _RESET_LIMITER.record_failure(ip, raw_email)
    try:
        email = normalize_email(raw_email)
    except AuthAdminError as error:
        return render_template("tenant/forgot_password.html", notice=None, error=str(error)), 400
    if settings.public_url:
        try:
            # Migration 0043: true only for an account with an active membership of a
            # draft/active tenant, under per-email and global hourly caps shared by every
            # application process. Nothing else reaches Supabase's email quota.
            if password_reset.claim(email, url=settings.app_database_url):
                context().auth_admin.send_password_reset(
                    email, auth_callback_url(settings.public_url)
                )
        except AuthAdminError:
            pass  # Same answer whether or not the account exists or Supabase refused.
        except Exception:  # noqa: BLE001 - the answer never reveals an internal failure
            logger.exception("password reset request could not be processed")
    # Same answer and (at least) the same duration whatever happened above.
    remaining = RESET_RESPONSE_FLOOR_SECONDS - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)
    return render_template("tenant/forgot_password.html", notice=RESET_SENT_MESSAGE, error=None)
