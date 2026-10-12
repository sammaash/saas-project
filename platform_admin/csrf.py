"""CSRF protection: a per-session token required on every state-changing request."""

from __future__ import annotations

import hmac
import secrets

from flask import abort, request, session

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SESSION_KEY = "csrf_token"


def token() -> str:
    if SESSION_KEY not in session:
        session[SESSION_KEY] = secrets.token_urlsafe(32)
    return session[SESSION_KEY]


def rotate() -> None:
    session[SESSION_KEY] = secrets.token_urlsafe(32)


def protect() -> None:
    """Abort with 400 unless the request carries the session's CSRF token."""
    if request.path == "/webhooks/whatsapp":
        return
    if request.method in SAFE_METHODS:
        return
    expected = session.get(SESSION_KEY)
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    if not expected or not hmac.compare_digest(expected, supplied):
        abort(400, description="Invalid CSRF token.")
