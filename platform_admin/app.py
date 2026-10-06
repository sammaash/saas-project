"""Application factory for the private platform admin dashboard."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from flask import Flask, render_template
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from . import csrf
from .auth import AuthError, Authenticator, Identity, LoginRateLimiter, SupabaseAuthenticator
from .config import Settings
from .services.validation import ServiceError, translate_db_error

logger = logging.getLogger("platform_admin")


class _UnconfiguredAuthenticator(Authenticator):
    def login(self, email: str, password: str) -> Identity:
        raise AuthError("Authentication is not configured.")


@dataclass
class PlatformContext:
    settings: Settings
    authenticator: Authenticator
    limiter: LoginRateLimiter


def create_app(
    settings: Settings | None = None,
    *,
    authenticator: Authenticator | None = None,
) -> Flask:
    settings = settings or Settings.from_env()
    if authenticator is None:
        if settings.supabase_url and settings.supabase_anon_key:
            authenticator = SupabaseAuthenticator(
                settings.supabase_url, settings.supabase_anon_key, settings.supabase_jwt_secret
            )
        else:
            authenticator = _UnconfiguredAuthenticator()

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=settings.cookie_secure,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=settings.session_minutes),
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    if settings.trusted_proxies > 0:
        # Only the configured number of proxies may set X-Forwarded-For; otherwise it is ignored.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=settings.trusted_proxies)
    app.extensions["platform"] = PlatformContext(
        settings=settings, authenticator=authenticator, limiter=LoginRateLimiter()
    )

    app.before_request(csrf.protect)
    app.jinja_env.globals["csrf_token"] = csrf.token

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.errorhandler(ServiceError)
    def service_error(error: ServiceError):
        return render_template("error.html", message=error.message), error.status

    @app.errorhandler(Exception)
    def unexpected_error(error: Exception):
        if isinstance(error, HTTPException):
            message = error.description or "Error"
            return render_template("error.html", message=message), error.code
        friendly = translate_db_error(error)
        if friendly is not None:
            return render_template("error.html", message=friendly.message), friendly.status
        # Log the exception type and SQLSTATE only: tracebacks and driver messages can echo
        # SQL parameter values (tenant data).
        logger.error(
            "unhandled %s (sqlstate=%s)", type(error).__name__, getattr(error, "sqlstate", None)
        )
        return render_template("error.html", message="Something went wrong."), 500

    from .routes import register_routes  # noqa: PLC0415  (avoids an import cycle)

    register_routes(app)
    return app
