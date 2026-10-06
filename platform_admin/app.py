"""Application factory for the private platform admin dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from flask import Flask, render_template

from . import csrf
from .auth import AuthError, Authenticator, Identity, LoginRateLimiter, SupabaseAuthenticator
from .config import Settings
from .meta.provider import MetaProvider, UnavailableMetaProvider
from .services.validation import ServiceError


class _UnconfiguredAuthenticator(Authenticator):
    def login(self, email: str, password: str) -> Identity:
        raise AuthError("Authentication is not configured.")


@dataclass
class PlatformContext:
    settings: Settings
    authenticator: Authenticator
    meta_provider: MetaProvider
    limiter: LoginRateLimiter


def create_app(
    settings: Settings | None = None,
    *,
    authenticator: Authenticator | None = None,
    meta_provider: MetaProvider | None = None,
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
    app.extensions["platform"] = PlatformContext(
        settings=settings,
        authenticator=authenticator,
        meta_provider=meta_provider or UnavailableMetaProvider(),
        limiter=LoginRateLimiter(),
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

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def http_error(error):
        message = getattr(error, "description", "Error")
        return render_template("error.html", message=message), error.code

    from .routes import register_routes  # noqa: PLC0415  (avoids an import cycle)

    register_routes(app)
    return app
