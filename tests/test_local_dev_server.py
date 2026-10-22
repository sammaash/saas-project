"""Database-free tests for the local-only development authentication fixture."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from platform_admin.auth import AuthError
from local_dev_server import (
    LOCAL_DEV_HOST,
    LOCAL_DEV_PORT,
    LocalDevAuthenticator,
    LocalDevConfigError,
    load_local_dev_config,
    require_active_admin,
)

BASE_ENV = {
    "PLATFORM_DEV_MODE": "1",
    "PLATFORM_INSECURE_COOKIES": "1",
    "SAAS_DATABASE_URL": (
        "postgresql://app_backend:local-password@127.0.0.1:5432/saas_cindy_menu_local"
    ),
    "SAAS_PLATFORM_DATABASE_URL": (
        "postgresql://saas_platform_admin:local-password@127.0.0.1:5432/"
        "saas_cindy_menu_local"
    ),
    "LOCAL_DEV_EMAIL": "local-admin@example.invalid",
    "LOCAL_DEV_PASSWORD": "local-only-password",
    "LOCAL_DEV_ADMIN_USER_ID": "f0000000-0000-4000-8000-000000000001",
    "LOCAL_DEV_DATABASE_NAME": "saas_cindy_menu_local",
}


def _env(**overrides: str) -> dict[str, str]:
    values = dict(BASE_ENV)
    values.update(overrides)
    return values


class LocalDevAuthenticatorTests(unittest.TestCase):
    def setUp(self):
        self.authenticator = LocalDevAuthenticator(
            "Local-Admin@Example.invalid",
            "local-only-password",
            "f0000000-0000-4000-8000-000000000001",
        )

    def test_correct_credentials_return_only_the_configured_admin(self):
        identity = self.authenticator.login(
            " local-admin@example.invalid ", "local-only-password"
        )
        self.assertEqual(identity.user_id, "f0000000-0000-4000-8000-000000000001")
        self.assertEqual(identity.email, "local-admin@example.invalid")

    def test_wrong_email_or_password_is_rejected(self):
        for email, password in (
            ("someone-else@example.invalid", "local-only-password"),
            ("local-admin@example.invalid", "wrong-password"),
            ("local-admin@example.invalid", ""),
        ):
            with self.subTest(email=email, password=password):
                with self.assertRaisesRegex(AuthError, "Invalid email or password"):
                    self.authenticator.login(email, password)

    def test_arbitrary_identity_is_not_accepted(self):
        with self.assertRaises(AuthError):
            self.authenticator.login(
                "local-admin@example.invalid", "local-only-password-extra"
            )


class LocalDevGuardTests(unittest.TestCase):
    def test_valid_configuration_is_loaded(self):
        config = load_local_dev_config(_env())
        self.assertEqual(config.email, "local-admin@example.invalid")
        self.assertEqual(config.admin_user_id, "f0000000-0000-4000-8000-000000000001")
        self.assertEqual(config.database_name, "saas_cindy_menu_local")

    def test_missing_or_disabled_development_mode_is_rejected(self):
        missing = _env()
        del missing["PLATFORM_DEV_MODE"]
        for values in (
            missing,
            _env(PLATFORM_DEV_MODE="0"),
            _env(PLATFORM_DEV_MODE="true"),
        ):
            with self.subTest(PLATFORM_DEV_MODE=values.get("PLATFORM_DEV_MODE")):
                with self.assertRaisesRegex(LocalDevConfigError, "PLATFORM_DEV_MODE=1"):
                    load_local_dev_config(values)

    def test_insecure_cookie_requirement_is_enforced(self):
        with self.assertRaisesRegex(LocalDevConfigError, "PLATFORM_INSECURE_COOKIES=1"):
            load_local_dev_config(_env(PLATFORM_INSECURE_COOKIES="0"))

    def test_non_loopback_database_is_rejected(self):
        with self.assertRaisesRegex(LocalDevConfigError, "loopback host"):
            load_local_dev_config(
                _env(
                    SAAS_DATABASE_URL=(
                        "postgresql://app_backend:pw@10.0.0.5:5432/"
                        "saas_cindy_menu_local"
                    )
                )
            )

    def test_non_disposable_database_is_rejected(self):
        with self.assertRaisesRegex(LocalDevConfigError, "does not match"):
            load_local_dev_config(
                _env(
                    SAAS_DATABASE_URL=(
                        "postgresql://app_backend:pw@127.0.0.1:5432/"
                        "saas_phase1_test"
                    )
                )
            )

    def test_shared_database_name_is_rejected(self):
        with self.assertRaisesRegex(LocalDevConfigError, "shared PostgreSQL"):
            load_local_dev_config(
                _env(
                    SAAS_DATABASE_URL="postgresql://app_backend:pw@127.0.0.1:5432/postgres",
                    SAAS_PLATFORM_DATABASE_URL=(
                        "postgresql://saas_platform_admin:pw@127.0.0.1:5432/postgres"
                    ),
                    LOCAL_DEV_DATABASE_NAME="postgres",
                )
            )

    def test_redirecting_query_parameters_are_rejected(self):
        with self.assertRaisesRegex(LocalDevConfigError, "redirecting"):
            load_local_dev_config(
                _env(
                    SAAS_DATABASE_URL=(
                        "postgresql://app_backend:pw@127.0.0.1:5432/"
                        "saas_cindy_menu_local?host=prod.example.com"
                    )
                )
            )

    def test_configured_admin_must_be_active(self):
        settings = SimpleNamespace(platform_database_url="postgresql://local/test")
        with self.assertRaisesRegex(LocalDevConfigError, "not an active row"):
            require_active_admin(
                settings,
                "f0000000-0000-4000-8000-000000000001",
                checker=lambda user_id, url=None: False,
            )

    def test_fixed_loopback_binding(self):
        self.assertEqual(LOCAL_DEV_HOST, "127.0.0.1")
        self.assertEqual(LOCAL_DEV_PORT, 5055)

    def test_fixture_is_not_wired_into_production_entry_points(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ("platform_admin/railway_app.py", "Procfile"):
            text = (root / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertNotIn("local_dev_server", text)


if __name__ == "__main__":
    unittest.main()
