"""Server-side authorization for the dashboard."""

from __future__ import annotations

import time
from functools import wraps

from flask import abort, current_app, g, redirect, session, url_for

from saas import platform as platform_db

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
