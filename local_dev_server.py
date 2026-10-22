"""Local-only dashboard runner with an injected development authenticator.

This is deliberately separate from the production entry point. It must never be
imported by ``platform_admin.railway_app`` or selected by the ``Procfile``.

The fixture replaces only the external Supabase identity provider. The normal
``platform_admin.routes.login`` authorization check still calls
``saas.platform.is_platform_admin``, so the configured development identity must
correspond to an active row in ``public.platform_admins``.

Required local environment (in addition to the normal application settings):

    PLATFORM_DEV_MODE=1
    PLATFORM_INSECURE_COOKIES=1
    LOCAL_DEV_EMAIL=<local-only email>
    LOCAL_DEV_PASSWORD=<local-only password>
    LOCAL_DEV_ADMIN_USER_ID=<active local platform admin UUID>
    LOCAL_DEV_DATABASE_NAME=<exact disposable local database name>

The server always binds to 127.0.0.1:5055. It never prints the password or any
authentication token.
"""

from __future__ import annotations

import hmac
import os
import sys
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from platform_admin.app import create_app
from platform_admin.auth import AuthError, Authenticator, Identity
from platform_admin.config import (
    ConfigError,
    Settings,
    verify_database_roles,
    verify_database_schema,
)
from saas import platform as platform_db

LOCAL_DEV_HOST = "127.0.0.1"
LOCAL_DEV_PORT = 5055
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SHARED_DATABASES = frozenset({"postgres", "template0", "template1"})
REDIRECTING_QUERY_KEYS = frozenset(
    {"host", "hostaddr", "port", "dbname", "service", "options", "user"}
)


class LocalDevConfigError(RuntimeError):
    """The local development fixture is not safe to enable."""


@dataclass(frozen=True)
class LocalDevConfig:
    email: str
    password: str
    admin_user_id: str
    database_name: str


class LocalDevAuthenticator(Authenticator):
    """Accept exactly one explicitly configured local development identity."""

    def __init__(self, email: str, password: str, admin_user_id: str):
        self._email = email.strip().lower()
        self._password = password
        self._admin_user_id = admin_user_id

    def login(self, email: str, password: str) -> Identity:
        supplied_email = (email or "").strip().lower()
        supplied_password = password or ""
        email_ok = hmac.compare_digest(
            supplied_email.encode("utf-8"), self._email.encode("utf-8")
        )
        password_ok = hmac.compare_digest(
            supplied_password.encode("utf-8"), self._password.encode("utf-8")
        )
        if not email_ok or not password_ok:
            raise AuthError("Invalid email or password.")
        return Identity(self._admin_user_id, self._email)


def _required(environ: dict[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value.strip():
        raise LocalDevConfigError(f"{name} must be set for the local development server.")
    return value


def _validate_database_url(url: str, database_name: str) -> None:
    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise LocalDevConfigError("The local database URL is malformed.") from error

    if parts.scheme not in ("postgres", "postgresql"):
        raise LocalDevConfigError(
            "SAAS_DATABASE_URL and SAAS_PLATFORM_DATABASE_URL must be PostgreSQL URLs."
        )
    host = (parts.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise LocalDevConfigError(
            "The local development database must use a loopback host "
            "(127.0.0.1, localhost or ::1)."
        )

    query = parse_qs(parts.query, keep_blank_values=True)
    redirecting = REDIRECTING_QUERY_KEYS.intersection(key.lower() for key in query)
    if redirecting:
        raise LocalDevConfigError(
            "The local development database URL contains a redirecting query parameter."
        )

    actual_database = (parts.path or "").lstrip("/")
    if "/" in actual_database:
        raise LocalDevConfigError("The local development database URL has an invalid path.")
    if actual_database.lower() in SHARED_DATABASES:
        raise LocalDevConfigError(
            "The local development server must not target a shared PostgreSQL database."
        )
    if actual_database != database_name:
        raise LocalDevConfigError(
            "The configured local database name does not match the database URL."
        )


def validate_local_dev_database_urls(
    app_url: str, platform_url: str, database_name: str
) -> None:
    """Fail closed unless both application DSNs target the same disposable loopback DB."""
    if not database_name.strip():
        raise LocalDevConfigError("LOCAL_DEV_DATABASE_NAME must be set.")
    _validate_database_url(app_url, database_name)
    _validate_database_url(platform_url, database_name)


def load_local_dev_config(environ: dict[str, str] | None = None) -> LocalDevConfig:
    """Read and validate all local-only settings without opening a connection."""
    values = dict(os.environ if environ is None else environ)

    if values.get("PLATFORM_DEV_MODE") != "1":
        raise LocalDevConfigError("PLATFORM_DEV_MODE=1 is required for the local dev server.")
    if values.get("PLATFORM_INSECURE_COOKIES") != "1":
        raise LocalDevConfigError(
            "PLATFORM_INSECURE_COOKIES=1 is required for the loopback HTTP server."
        )

    email = _required(values, "LOCAL_DEV_EMAIL").strip().lower()
    password = _required(values, "LOCAL_DEV_PASSWORD")
    admin_user_id = _required(values, "LOCAL_DEV_ADMIN_USER_ID").strip()
    database_name = _required(values, "LOCAL_DEV_DATABASE_NAME").strip()

    try:
        admin_user_id = str(uuid.UUID(admin_user_id))
    except ValueError as error:
        raise LocalDevConfigError("LOCAL_DEV_ADMIN_USER_ID must be a UUID.") from error

    if "@" not in email:
        raise LocalDevConfigError("LOCAL_DEV_EMAIL must look like an email address.")
    if len(password) < 8:
        raise LocalDevConfigError("LOCAL_DEV_PASSWORD must be at least 8 characters.")

    validate_local_dev_database_urls(
        _required(values, "SAAS_DATABASE_URL"),
        _required(values, "SAAS_PLATFORM_DATABASE_URL"),
        database_name,
    )
    return LocalDevConfig(
        email=email,
        password=password,
        admin_user_id=admin_user_id,
        database_name=database_name,
    )


def require_active_admin(
    settings: Settings,
    admin_user_id: str,
    checker=platform_db.is_platform_admin,
) -> None:
    """Require the configured identity to already be an active platform admin."""
    if not checker(admin_user_id, url=settings.platform_database_url):
        raise LocalDevConfigError(
            "LOCAL_DEV_ADMIN_USER_ID is not an active row in public.platform_admins."
        )


def build_local_dev_app(environ: dict[str, str] | None = None):
    """Build the Flask app with the local authenticator after every guard passes."""
    config = load_local_dev_config(environ)
    settings = Settings.from_env(environ)
    validate_local_dev_database_urls(
        settings.app_database_url or "",
        settings.platform_database_url or "",
        config.database_name,
    )
    verify_database_roles(settings)
    verify_database_schema(settings)
    require_active_admin(settings, config.admin_user_id)
    return create_app(
        settings,
        authenticator=LocalDevAuthenticator(
            config.email, config.password, config.admin_user_id
        ),
    )


def main(environ: dict[str, str] | None = None) -> int:
    try:
        app = build_local_dev_app(environ)
    except LocalDevConfigError as error:
        print(f"LOCAL DEV SERVER REFUSED: {error}", file=sys.stderr)
        return 2
    except ConfigError as error:
        print(f"LOCAL DEV SERVER REFUSED: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - never print credentials or a DSN
        print(
            "LOCAL DEV SERVER REFUSED: startup database/schema check failed "
            f"({type(error).__name__}).",
            file=sys.stderr,
        )
        return 2

    print(
        f"Local development dashboard: http://{LOCAL_DEV_HOST}:{LOCAL_DEV_PORT}",
        flush=True,
    )
    app.run(
        host=LOCAL_DEV_HOST,
        port=LOCAL_DEV_PORT,
        debug=False,
        use_reloader=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
