"""Server-side authorization for the dashboard."""

from __future__ import annotations

import time
from functools import wraps

from flask import abort, current_app, g, redirect, session, url_for

from saas import platform as platform_db
from saas import tenant_membership

from .app import PlatformContext


def context() -> PlatformContext:
    return current_app.extensions["platform"]


def admin_required(view):
    """Allow only an active platform administrator, re-checked in the database each request."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id or session.get("exp", 0) < time.time():
            session.clear()
            return redirect(url_for("auth.login"))
        try:
            allowed = platform_db.is_platform_admin(
                user_id, url=context().settings.platform_database_url
            )
        except Exception:  # noqa: BLE001 - fail closed when the database is unreachable
            abort(503)
        if not allowed:
            session.clear()
            abort(403)
        g.admin_id = user_id
        return view(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# Tenant portal (namespaced ``t_*`` session keys)
# ---------------------------------------------------------------------------
#
# The admin console and the tenant portal share one signed cookie but use
# disjoint key namespaces: ``user_id``/``email``/``exp`` for platform admins and
# ``t_*`` for tenant users. Tenant code never calls ``session.clear()``: a
# tenant sign-in or sign-out touches only tenant keys, so a concurrent admin
# session survives. The admin flow keeps its historical semantics unchanged.

TENANT_SESSION_KEYS = ("t_user_id", "t_email", "t_exp", "t_tenant_id")


def clear_tenant_session() -> None:
    """Discard only the tenant-namespace keys; the admin namespace is untouched."""
    for key in TENANT_SESSION_KEYS:
        session.pop(key, None)


def tenant_session_user_id() -> str | None:
    """The verified tenant user id, or None when the session is absent or expired."""
    user_id = session.get("t_user_id")
    if not isinstance(user_id, str) or not user_id:
        return None
    exp = session.get("t_exp", 0)
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    return user_id


def _tenant_guard(view, *, owner_only: bool | None):
    """Per-request tenant authorization.

    ``owner_only=None``: any signed-in tenant session (no specific tenant).
    ``owner_only=False``: an active membership of the URL's tenant (any role).
    ``owner_only=True``: an active ``owner`` membership of the URL's tenant.

    The tenant id comes from the URL but is only ever trusted after a
    server-side membership re-check against the database; if that check fails
    the request is denied (fail closed).
    """

    @wraps(view)
    def wrapped(*args, **kwargs):
        user_id = tenant_session_user_id()
        if user_id is None:
            clear_tenant_session()
            return redirect(url_for("tenant_auth.login"))
        g.tenant_user_id = user_id
        g.tenant_email = session.get("t_email", "")
        if owner_only is not None:
            tenant_id = kwargs.get("tenant_id")
            if tenant_id is None:
                abort(404)
            tenant_id = str(tenant_id)
            try:
                membership = tenant_membership.membership_for(
                    user_id, tenant_id, url=context().settings.app_database_url
                )
            except Exception:  # noqa: BLE001 - fail closed when the database is unreachable
                abort(503)
            if membership is None:
                abort(403)
            if owner_only and membership["role"] != "owner":
                abort(403)
            g.tenant_id = tenant_id
            g.tenant_role = membership["role"]
        return view(*args, **kwargs)

    return wrapped


def tenant_session_required(view):
    """Allow only a signed-in tenant session (any membership; no tenant selected yet)."""
    return _tenant_guard(view, owner_only=None)


def member_required(view):
    """Allow only an active member of the URL's tenant (re-checked every request)."""
    return _tenant_guard(view, owner_only=False)


def owner_required(view):
    """Allow only an active owner of the URL's tenant (re-checked every request)."""
    return _tenant_guard(view, owner_only=True)
