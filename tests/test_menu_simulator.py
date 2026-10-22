"""Route and adapter tests for the local WhatsApp menu simulator.

The actual menu flows are covered by ``test_whatsapp_menu`` and ``test_menu_db``. These
tests prove that the simulator route delegates to that service, renders its real list and
button replies, keeps its own session state, and never calls the WhatsApp sender.
"""

from __future__ import annotations

import time
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from platform_admin.app import create_app
from platform_admin.config import ConfigError, Settings
from platform_admin.services import menu as menu_service
from platform_admin.services import menu_simulator as simulator_service
from platform_admin.services.validation import ServiceError

ADMIN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"

REMOTE_ENV = {
    "PLATFORM_SECRET_KEY": "k" * 40,
    "SUPABASE_URL": "https://project.supabase.co",
    "SUPABASE_ANON_KEY": "anon-key",
    "SAAS_DATABASE_URL": "postgresql://app_backend:pw@db.invalid:5432/app",
    "SAAS_PLATFORM_DATABASE_URL": "postgresql://saas_platform_admin:pw@db.invalid:5432/app",
}


def _env(**overrides: str) -> dict[str, str]:
    values = dict(REMOTE_ENV)
    values.update(overrides)
    return values


def _tenant(tenant_id: str, slug: str) -> dict[str, str]:
    return {"id": tenant_id, "name": slug, "slug": slug, "status": "active"}


def _main_reply() -> menu_service.MenuReply:
    rows = [
        {"id": menu_service.make_action(step), "title": title, "description": "test"}
        for step, title in (
            ("browse", "Browse Products"),
            ("order", "Place an Order"),
            ("my_order", "My Order"),
            ("delivery", "Delivery & Collection"),
            ("faqs", "FAQs"),
            ("staff", "Talk to Staff"),
        )
    ]
    return menu_service.MenuReply(
        text="Main menu options",
        kind="list",
        header="Main menu",
        sections=({"title": "Menu", "rows": rows},),
        footer="Reply MENU for this menu",
    )


class MenuSimulatorRouteTests(unittest.TestCase):
    def setUp(self):
        self.tenants = [_tenant(TENANT_A, "cindy-bakes"), _tenant(TENANT_B, "other")]
        self.tenant = self.tenants[0]
        self.enabled = True
        self.main_reply = _main_reply()
        self.action_reply = menu_service.MenuReply(
            text="Selected menu option", kind="buttons", header="Result"
        )
        self.settings = Settings(
            secret_key="k" * 40,
            cookie_secure=False,
            app_database_url="postgresql://app_backend@127.0.0.1:5432/test",
            platform_database_url="postgresql://saas_platform_admin@127.0.0.1:5432/test",
        )
        self.app = create_app(self.settings)
        self.client = self.app.test_client()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch("saas.platform.is_platform_admin", return_value=True)
        )
        self.stack.enter_context(
            patch(
                "platform_admin.routes.tenant_service.list_tenants",
                return_value=self.tenants,
            )
        )
        self.stack.enter_context(
            patch(
                "platform_admin.routes.tenant_service.get_tenant",
                return_value=self.tenant,
            )
        )
        self.stack.enter_context(
            patch(
                "platform_admin.services.menu_simulator.status",
                side_effect=lambda _settings, _tenant_id: (
                    self.enabled,
                    {"configuration": {"menu": {"enabled": self.enabled}}},
                ),
            )
        )
        self.stack.enter_context(
            patch(
                "platform_admin.services.menu_simulator.make_context",
                return_value=Mock(),
            )
        )
        self.main = self.stack.enter_context(
            patch(
                "platform_admin.routes.menu_service.main_menu",
                return_value=self.main_reply,
            )
        )
        self.handle_text = self.stack.enter_context(
            patch(
                "platform_admin.routes.menu_service.handle_text",
                return_value=self.main_reply,
            )
        )
        self.handle_action = self.stack.enter_context(
            patch(
                "platform_admin.routes.menu_service.handle_action",
                return_value=self.action_reply,
            )
        )
        self.send_text = self.stack.enter_context(
            patch("platform_admin.routes.whatsapp_service.send_text")
        )
        self.send_list = self.stack.enter_context(
            patch("platform_admin.routes.whatsapp_service.send_list")
        )
        self.send_buttons = self.stack.enter_context(
            patch("platform_admin.routes.whatsapp_service.send_buttons")
        )

    def login(self):
        with self.client.session_transaction() as session:
            session["user_id"] = ADMIN_ID
            session["email"] = "admin@example.com"
            session["exp"] = time.time() + 600
            session["csrf_token"] = "test-csrf"

    def post(self, data: dict):
        return self.client.post(
            "/playground/menu", data={"csrf_token": "test-csrf", **data}
        )

    def test_anonymous_access_redirects_to_login(self):
        response = self.client.get("/playground/menu")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_enabled_main_menu_is_rendered_with_real_reply_options(self):
        self.login()
        response = self.client.get(f"/playground/menu?tenant_id={TENANT_A}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for title in (
            "WhatsApp Menu Simulator",
            "Browse Products",
            "Place an Order",
            "My Order",
            "Delivery &amp; Collection",
            "FAQs",
            "Talk to Staff",
        ):
            self.assertIn(title, html)
        self.assertIn("No WhatsApp send", html)
        self.assertIn(f'href="/tenants/{TENANT_A}/rules#config-menu"', html)
        self.assertEqual(self.main.call_count, 1)

    def test_disabled_menu_shows_actual_state_and_configuration_link(self):
        self.enabled = False
        self.login()
        response = self.client.get(f"/playground/menu?tenant_id={TENANT_A}")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("WhatsApp menu is disabled", html)
        self.assertIn(f'href="/tenants/{TENANT_A}/rules#config-menu"', html)
        self.assertNotIn("Browse Products", html)
        self.main.assert_not_called()

    def test_text_command_uses_the_real_menu_text_router(self):
        self.login()
        response = self.post(
            {"tenant_id": TENANT_A, "action": "text", "customer_message": "menu"}
        )
        self.assertEqual(response.status_code, 200)
        self.handle_text.assert_called_once()
        self.assertEqual(self.handle_text.call_args.args[1], "menu")
        self.assertIn("Main menu options", response.get_data(as_text=True))

    def test_clickable_option_uses_the_real_action_router(self):
        self.login()
        action_id = self.main_reply.sections[0]["rows"][0]["id"]
        response = self.post(
            {
                "tenant_id": TENANT_A,
                "action": "action",
                "action_id": action_id,
                "action_title": "Browse Products",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.handle_action.assert_called_once()
        self.assertEqual(self.handle_action.call_args.args[1], action_id)
        self.assertIn("Selected menu option", response.get_data(as_text=True))

    def test_unknown_or_expired_action_gets_an_honest_retry_message(self):
        self.handle_action.return_value = None
        self.login()
        response = self.post(
            {
                "tenant_id": TENANT_A,
                "action": "action",
                "action_id": "m1.nope.-.9999999999",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("no longer available", response.get_data(as_text=True))

    def test_reset_rebuilds_the_simulator_and_clears_old_messages(self):
        self.login()
        self.post({"tenant_id": TENANT_A, "action": "text", "customer_message": "menu"})
        with self.client.session_transaction() as session:
            self.assertIn("menu_simulator_states", session)
        response = self.post({"tenant_id": TENANT_A, "action": "reset"})
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            states = session["menu_simulator_states"]
            key = f"{ADMIN_ID}:{TENANT_A}"
            self.assertIn(key, states)
            self.assertEqual(len(states[key]["messages"]), 1)
        self.assertIn("Main menu options", response.get_data(as_text=True))

    def test_simulator_state_is_separate_for_each_tenant(self):
        self.login()
        self.post({"tenant_id": TENANT_A, "action": "text", "customer_message": "menu"})
        self.client.get(f"/playground/menu?tenant_id={TENANT_B}")
        with self.client.session_transaction() as session:
            states = session["menu_simulator_states"]
            self.assertIn(f"{ADMIN_ID}:{TENANT_A}", states)
            self.assertIn(f"{ADMIN_ID}:{TENANT_B}", states)
            self.assertNotIn("playground_histories", session)

    def test_no_whatsapp_provider_is_ever_called(self):
        self.login()
        self.post({"tenant_id": TENANT_A, "action": "text", "customer_message": "menu"})
        self.post(
            {
                "tenant_id": TENANT_A,
                "action": "action",
                "action_id": self.main_reply.sections[0]["rows"][0]["id"],
            }
        )
        self.send_text.assert_not_called()
        self.send_list.assert_not_called()
        self.send_buttons.assert_not_called()


class MenuSimulatorAdapterTests(unittest.TestCase):
    def test_non_loopback_database_is_rejected(self):
        settings = Settings(
            secret_key="k" * 40,
            app_database_url="postgresql://app_backend@db.invalid/app",
        )
        with self.assertRaisesRegex(ServiceError, "loopback"):
            simulator_service.require_local_database(settings)

    def test_remote_database_is_allowed_only_with_explicit_true(self):
        rejected = Settings.from_env(_env())
        self.assertFalse(simulator_service.remote_database_allowed(rejected))
        with self.assertRaisesRegex(ServiceError, "loopback"):
            simulator_service.require_local_database(rejected)

        allowed = Settings.from_env(
            _env(MENU_SIMULATOR_ALLOW_REMOTE_DATABASE="true")
        )
        self.assertTrue(simulator_service.remote_database_allowed(allowed))
        simulator_service.require_local_database(allowed)

    def test_loopback_database_continues_to_work_without_the_flag(self):
        settings = Settings.from_env(
            _env(SAAS_DATABASE_URL="postgresql://app_backend:pw@127.0.0.1:5432/app")
        )
        self.assertFalse(settings.menu_simulator_allow_remote_database)
        simulator_service.require_local_database(settings)

    def test_false_flag_values_keep_remote_databases_rejected(self):
        for value in ("false", "0", "no", "off"):
            with self.subTest(value=value):
                settings = Settings.from_env(
                    _env(MENU_SIMULATOR_ALLOW_REMOTE_DATABASE=value)
                )
                self.assertFalse(settings.menu_simulator_allow_remote_database)
                with self.assertRaisesRegex(ServiceError, "loopback"):
                    simulator_service.require_local_database(settings)

    def test_invalid_flag_values_fail_closed(self):
        for value in ("1", "yes", "on", "maybe", "2", "", "true false", "  "):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Settings.from_env(
                    _env(MENU_SIMULATOR_ALLOW_REMOTE_DATABASE=value)
                )

    def test_remote_flag_must_be_exactly_true_and_a_host_must_exist(self):
        truthy_string = Settings(
            secret_key="k" * 40,
            app_database_url="postgresql://app_backend@db.invalid/app",
            menu_simulator_allow_remote_database="true",
        )
        with self.assertRaisesRegex(ServiceError, "loopback"):
            simulator_service.require_local_database(truthy_string)

        missing_host = Settings(
            secret_key="k" * 40,
            app_database_url=None,
            menu_simulator_allow_remote_database=True,
        )
        with self.assertRaisesRegex(ServiceError, "loopback"):
            simulator_service.require_local_database(missing_host)

    def test_remote_mode_uses_the_existing_url_and_synthetic_identity(self):
        settings = Settings.from_env(
            _env(
                MENU_SIMULATOR_ALLOW_REMOTE_DATABASE="true",
                WHATSAPP_SEND_ENABLED="1",
            )
        )
        with patch(
            "platform_admin.services.menu_simulator.channel_service.list_channels",
            return_value=[],
        ):
            context = simulator_service.make_context(
                settings,
                TENANT_A,
                ADMIN_ID,
                {"configuration": {"menu": {"enabled": True}}},
                message_id="sim-test",
            )
        self.assertEqual(context.tenant_id, TENANT_A)
        self.assertEqual(context.wa_id, f"{simulator_service.SIMULATOR_WA_PREFIX}{ADMIN_ID}")
        self.assertEqual(context.settings.app_database_url, settings.app_database_url)
        self.assertFalse(context.settings.whatsapp_send_enabled)

    def test_remote_status_uses_the_supplied_configured_settings(self):
        settings = Settings.from_env(
            _env(MENU_SIMULATOR_ALLOW_REMOTE_DATABASE="true")
        )
        with patch(
            "platform_admin.services.menu_simulator.menu.load_document",
            return_value={"configuration": {"menu": {"enabled": True}}},
        ) as load:
            enabled, document = simulator_service.status(settings, TENANT_A)
        self.assertTrue(enabled)
        self.assertEqual(document["configuration"]["menu"]["enabled"], True)
        load.assert_called_once_with(settings, TENANT_A)

    def test_reply_view_preserves_list_ids_and_button_ids(self):
        list_reply = _main_reply()
        view = simulator_service.reply_view(list_reply)
        self.assertEqual(view["kind"], "list")
        self.assertEqual(len(view["sections"][0]["rows"]), 6)
        button_reply = menu_service.MenuReply(
            text="buttons",
            kind="buttons",
            buttons=({"id": "m1.main.-.9999999999", "title": "Main menu"},),
        )
        button_view = simulator_service.reply_view(button_reply)
        self.assertEqual(button_view["kind"], "buttons")
        self.assertEqual(button_view["buttons"][0]["id"], "m1.main.-.9999999999")


if __name__ == "__main__":
    unittest.main()
