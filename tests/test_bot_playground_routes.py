"""Database-free route tests for Bot Playground security and session isolation."""

from __future__ import annotations

import time
import unittest
import uuid
from contextlib import ExitStack
from unittest.mock import Mock, patch

from platform_admin.app import create_app
from platform_admin.config import Settings
from platform_admin.services.validation import ServiceError

ADMIN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
PRODUCT_A = "aaaaaaaa-1111-4111-8111-111111111111"
VARIANT_A = "aaaaaaaa-2222-4222-8222-222222222222"
INACTIVE_VARIANT = "aaaaaaaa-3333-4333-8333-333333333333"
PRODUCT_B = "bbbbbbbb-1111-4111-8111-111111111111"
VARIANT_B = "bbbbbbbb-2222-4222-8222-222222222222"


def _tenant(tenant_id: str, slug: str) -> dict[str, str]:
    return {"id": tenant_id, "name": slug, "slug": slug, "status": "active"}


def _context(tenant: dict[str, str]) -> dict:
    is_a = tenant["id"] == TENANT_A
    return {
        "tenant": tenant,
        "settings": {
            "currency": "KES",
            "timezone": "Africa/Nairobi",
            "business_rules": {},
            "bot_persona": {},
            "business_hours": {},
            "configuration": {},
        },
        "catalogue": [
            {
                "id": PRODUCT_A if is_a else PRODUCT_B,
                "name": "Lemon cake" if is_a else "Tenant B cookies",
                "description": "Freshly prepared",
                "category": "bakes",
                "attributes": {},
                "image_url": None,
                "variants": [
                    {
                        "id": VARIANT_A if is_a else VARIANT_B,
                        "code": "large" if is_a else "box",
                        "name": "Large" if is_a else "Box",
                        "price": 500.0 if is_a else 300.0,
                        "attributes": {},
                    }
                ],
            }
        ],
    }


class BotPlaygroundRouteTests(unittest.TestCase):
    def setUp(self):
        settings = Settings(
            secret_key="k" * 40,
            cookie_secure=False,
            app_database_url="postgresql://app_backend@db.invalid/postgres",
            platform_database_url="postgresql://saas_platform_admin@db.invalid/postgres",
        )
        self.provider = Mock()
        self.app = create_app(settings, bot_provider=self.provider)
        self.client = self.app.test_client()
        self.tenants = [_tenant(TENANT_A, "saas-test"), _tenant(TENANT_B, "second-tenant")]
        self.contexts = {tenant["id"]: _context(tenant) for tenant in self.tenants}

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("saas.platform.is_platform_admin", return_value=True))
        self.list_tenants = self.stack.enter_context(
            patch("platform_admin.routes.tenant_service.list_tenants", return_value=self.tenants)
        )
        self.get_tenant = self.stack.enter_context(
            patch(
                "platform_admin.routes.tenant_service.get_tenant",
                side_effect=lambda _settings, _admin, tenant_id: next(
                    (tenant for tenant in self.tenants if tenant["id"] == tenant_id), None
                ),
            )
        )
        self.load_context = self.stack.enter_context(
            patch(
                "platform_admin.routes.playground_service.load_tenant_context",
                side_effect=lambda _settings, tenant_id: self.contexts[tenant_id],
            )
        )
        self.respond = self.stack.enter_context(
            patch(
                "platform_admin.routes.playground_service.respond_to_customer",
                side_effect=lambda _settings, _provider, tenant_id, message, _history, _context: {
                    "mode": "safe-test",
                    "tenant": next(tenant for tenant in self.tenants if tenant["id"] == tenant_id),
                    "response": f"AI:{message}",
                    "customer_message": message,
                },
            )
        )
        self.channel_mutators = [
            self.stack.enter_context(
                patch(f"platform_admin.routes.channel_service.{name}")
            )
            for name in ("connect_channel", "update_channel", "set_channel_status")
        ]

    def login_session(self):
        with self.client.session_transaction() as session:
            session["user_id"] = ADMIN_ID
            session["email"] = "admin@example.com"
            session["exp"] = time.time() + 600
            session["csrf_token"] = "test-csrf"

    def post(self, data: dict):
        return self.client.post("/playground", data={"csrf_token": "test-csrf", **data})

    def api_post(self, data: dict, client=None, csrf_token="test-csrf"):
        return (client or self.client).post(
            "/playground/api",
            data={"csrf_token": csrf_token, **data},
            headers={"Accept": "application/json"},
        )

    def test_anonymous_access_redirects_to_login(self):
        response = self.client.get("/playground")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_json_endpoint_requires_an_authenticated_platform_admin(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "anonymous-csrf"
        response = self.client.post(
            "/playground/api",
            data={
                "csrf_token": "anonymous-csrf",
                "tenant_id": TENANT_A,
                "action": "browse",
            },
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_default_tenant_is_saas_test_and_is_loaded_server_side(self):
        self.login_session()
        response = self.client.get("/playground")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"saas-test", response.data)
        self.assertIn(f'<option value="{TENANT_A}" selected>'.encode(), response.data)
        self.assertIn(b"WhatsApp Conversation Simulator", response.data)
        self.assertIn(b'data-api-url="/playground/api"', response.data)
        self.assertIn(b'src="/static/playground.js"', response.data)
        self.load_context.assert_called_with(unittest.mock.ANY, TENANT_A)
        self.assertEqual(self.list_tenants.call_count, 1)

    def test_tenant_picker_selects_default_and_explicit_tenants_across_uuid_types(self):
        self.contexts[TENANT_A]["tenant"] = _tenant(TENANT_A, "saas-test")
        self.contexts[TENANT_B]["tenant"] = _tenant(TENANT_B, "second-tenant")
        self.tenants[0]["id"] = uuid.UUID(TENANT_A)
        self.tenants[1]["id"] = uuid.UUID(TENANT_B)
        self.load_context.side_effect = (
            lambda _settings, tenant_id: self.contexts[str(tenant_id)]
        )
        self.login_session()

        default_page = self.client.get("/playground").get_data(as_text=True)
        self.assertIn(f'<option value="{TENANT_A}" selected>', default_page)
        self.assertIn(f'<option value="{TENANT_B}" >', default_page)

        explicit_page = self.client.get(
            f"/playground?tenant_id={TENANT_B}"
        ).get_data(as_text=True)
        self.assertIn(f'<option value="{TENANT_B}" selected>', explicit_page)
        self.assertIn(f'<option value="{TENANT_A}" >', explicit_page)
        self.assertIn("second-tenant", explicit_page)
        self.load_context.assert_called_with(unittest.mock.ANY, uuid.UUID(TENANT_B))

    def test_catalogue_shows_active_products_variants_and_prices(self):
        self.login_session()
        response = self.post(
            {"tenant_id": TENANT_A, "action": "browse"}
        )
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Lemon cake", page)
        self.assertIn("Large", page)
        self.assertIn("KES 500.00", page)

    def test_unknown_tenant_is_rejected_before_response_generation(self):
        self.login_session()
        response = self.post(
            {
                "tenant_id": "33333333-3333-4333-8333-333333333333",
                "customer_message": "hello",
            }
        )
        self.assertEqual(response.status_code, 404)
        self.respond.assert_not_called()

    def test_histories_are_isolated_and_new_conversation_is_tenant_scoped(self):
        self.login_session()
        self.assertEqual(
            self.post({"tenant_id": TENANT_A, "customer_message": "only-a"}).status_code, 200
        )
        self.assertEqual(
            self.post({"tenant_id": TENANT_B, "customer_message": "only-b"}).status_code, 200
        )
        page_a = self.client.get(f"/playground?tenant_id={TENANT_A}").get_data(as_text=True)
        page_b = self.client.get(f"/playground?tenant_id={TENANT_B}").get_data(as_text=True)
        self.assertIn("only-a", page_a)
        self.assertNotIn("only-b", page_a)
        self.assertIn("only-b", page_b)
        self.assertNotIn("only-a", page_b)

        self.assertEqual(self.post({"tenant_id": TENANT_A, "start_new": "1"}).status_code, 302)
        page_a = self.client.get(f"/playground?tenant_id={TENANT_A}").get_data(as_text=True)
        page_b = self.client.get(f"/playground?tenant_id={TENANT_B}").get_data(as_text=True)
        self.assertNotIn("only-a", page_a)
        self.assertIn("only-b", page_b)

    def test_provider_error_is_displayed_safely(self):
        self.login_session()
        self.respond.side_effect = ServiceError("The AI provider could not generate a response.", 502)
        response = self.post({"tenant_id": TENANT_A, "customer_message": "hello"})
        self.assertEqual(response.status_code, 502)
        self.assertIn(b"AI provider could not generate", response.data)

    def test_json_message_submission_returns_new_messages_and_fragments(self):
        self.login_session()
        response = self.api_post(
            {"tenant_id": TENANT_A, "action": "message", "customer_message": "hello"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_json)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(
            [(item["role"], item["text"]) for item in payload["messages"]],
            [("user", "hello"), ("assistant", "AI:hello")],
        )
        self.assertIn("AI:hello", payload["chat_html"])
        self.assertIn("playground", response.request.path)
        self.assertEqual(self.load_context.call_count, 1)

    def test_json_catalogue_cart_quantity_and_checkout_stay_server_controlled(self):
        self.login_session()
        catalogue = self.api_post({"tenant_id": TENANT_A, "action": "browse"}).get_json()
        self.assertEqual(catalogue["panel"], "catalogue")
        self.assertEqual(catalogue["catalogue"][0]["name"], "Lemon cake")
        self.assertIn("KES 500.00", catalogue["panel_html"])

        added = self.api_post(
            {
                "tenant_id": TENANT_A,
                "action": "add_cart",
                "product_id": PRODUCT_A,
                "variant_id": VARIANT_A,
                "quantity": "2",
                "price": "0.01",
            }
        ).get_json()
        self.assertEqual(added["cart"]["total"], "1000.00")
        self.assertEqual(added["cart"]["item_count"], 2)
        self.assertNotIn("0.01", added["panel_html"])

        updated = self.api_post(
            {
                "tenant_id": TENANT_A,
                "action": "update_cart",
                "product_id": PRODUCT_A,
                "variant_id": VARIANT_A,
                "quantity": "3",
            }
        ).get_json()
        self.assertEqual(updated["cart"]["total"], "1500.00")
        self.assertIn("KES 1500.00", updated["panel_html"])

        checkout = self.api_post(
            {"tenant_id": TENANT_A, "action": "checkout"}
        ).get_json()
        self.assertEqual(checkout["panel"], "checkout")
        self.assertIn("NOT A REAL ORDER", checkout["panel_html"])

        confirmed = self.api_post(
            {
                "tenant_id": TENANT_A,
                "action": "confirm_checkout",
                "customer_name": "Demo Customer",
            }
        ).get_json()
        self.assertEqual(confirmed["panel"], "confirmation")
        self.assertEqual(confirmed["cart"]["item_count"], 0)
        self.assertEqual(confirmed["confirmation"]["cart"]["total"], "1500.00")
        for mutator in self.channel_mutators:
            mutator.assert_not_called()

    def test_json_endpoint_rejects_csrf_unknown_tenant_and_malformed_actions(self):
        self.login_session()
        missing_csrf = self.client.post(
            "/playground/api",
            data={"tenant_id": TENANT_A, "action": "browse"},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(missing_csrf.status_code, 400)

        unknown_tenant = self.api_post(
            {
                "tenant_id": "33333333-3333-4333-8333-333333333333",
                "action": "browse",
            }
        )
        self.assertEqual(unknown_tenant.status_code, 404)
        self.assertEqual(unknown_tenant.get_json()["error"], "Tenant not found.")

        self.get_tenant.side_effect = ServiceError("Tenant must be a valid UUID.", 400)
        malformed_tenant = self.api_post(
            {"tenant_id": "not-a-uuid", "action": "browse"}
        )
        self.assertEqual(malformed_tenant.status_code, 400)
        self.assertEqual(
            malformed_tenant.get_json()["error"], "Tenant must be a valid UUID."
        )
        self.get_tenant.side_effect = None
        self.get_tenant.return_value = self.tenants[0]

        unknown_action = self.api_post(
            {"tenant_id": TENANT_A, "action": "not-an-action"}
        )
        self.assertEqual(unknown_action.status_code, 400)
        self.assertIn("Unknown", unknown_action.get_json()["error"])

        self.respond.side_effect = ServiceError("Customer message is required.", 400)
        missing_message = self.api_post(
            {"tenant_id": TENANT_A, "action": "message", "customer_message": ""}
        )
        self.assertEqual(missing_message.status_code, 400)
        self.assertEqual(
            missing_message.get_json()["error"], "Customer message is required."
        )

    def test_json_provider_error_remains_generic_and_recoverable(self):
        self.login_session()
        self.respond.side_effect = ServiceError(
            "The AI provider could not generate a response. Try again.", 502
        )
        response = self.api_post(
            {"tenant_id": TENANT_A, "action": "message", "customer_message": "hello"}
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json()["error"],
            "The AI provider could not generate a response. Try again.",
        )

    def test_json_cart_and_history_are_tenant_and_session_scoped(self):
        self.login_session()
        self.api_post(
            {
                "tenant_id": TENANT_A,
                "action": "add_cart",
                "product_id": PRODUCT_A,
                "variant_id": VARIANT_A,
                "quantity": "1",
            }
        )
        self.api_post(
            {"tenant_id": TENANT_A, "action": "message", "customer_message": "only-a"}
        )
        tenant_b = self.api_post(
            {"tenant_id": TENANT_B, "action": "view_cart"}
        ).get_json()
        self.assertEqual(tenant_b["cart"]["item_count"], 0)
        self.assertNotIn("only-a", tenant_b["chat_html"])

        other_client = self.app.test_client()
        with other_client.session_transaction() as other_session:
            other_session["user_id"] = ADMIN_ID
            other_session["email"] = "other-admin@example.com"
            other_session["exp"] = time.time() + 600
            other_session["csrf_token"] = "other-csrf"
        other = self.api_post(
            {"tenant_id": TENANT_A, "action": "view_cart"},
            client=other_client,
            csrf_token="other-csrf",
        ).get_json()
        self.assertEqual(other["cart"]["item_count"], 0)
        self.assertNotIn("only-a", other["chat_html"])

    def test_json_reset_clears_only_selected_tenant_chat_and_cart(self):
        self.login_session()
        self.api_post(
            {"tenant_id": TENANT_A, "action": "message", "customer_message": "clear-a"}
        )
        self.api_post(
            {
                "tenant_id": TENANT_A,
                "action": "add_cart",
                "product_id": PRODUCT_A,
                "variant_id": VARIANT_A,
                "quantity": "1",
            }
        )
        self.api_post(
            {"tenant_id": TENANT_B, "action": "message", "customer_message": "keep-b"}
        )

        reset = self.api_post({"tenant_id": TENANT_A, "start_new": "1"})
        self.assertEqual(reset.status_code, 200)
        payload = reset.get_json()
        self.assertTrue(payload["reset"])
        self.assertEqual(payload["cart"]["item_count"], 0)
        self.assertNotIn("clear-a", payload["chat_html"])
        tenant_b = self.api_post(
            {"tenant_id": TENANT_B, "action": "talk"}
        ).get_json()
        self.assertIn("keep-b", tenant_b["chat_html"])

    def test_cart_prices_and_totals_are_calculated_from_server_catalogue(self):
        self.login_session()
        response = self.post(
            {
                "tenant_id": TENANT_A,
                "action": "add_cart",
                "product_id": PRODUCT_A,
                "variant_id": VARIANT_A,
                "quantity": "2",
                "price": "0.01",
            }
        )
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("KES 500.00", page)
        self.assertIn("KES 1000.00", page)
        self.assertNotIn("KES 0.01", page)
        self.respond.assert_not_called()

    def test_unknown_product_and_inactive_variant_are_rejected(self):
        self.login_session()
        unknown = self.post(
            {
                "tenant_id": TENANT_A,
                "action": "add_cart",
                "product_id": "cccccccc-1111-4111-8111-111111111111",
                "variant_id": VARIANT_A,
                "quantity": "1",
            }
        )
        inactive = self.post(
            {
                "tenant_id": TENANT_A,
                "action": "add_cart",
                "product_id": PRODUCT_A,
                "variant_id": INACTIVE_VARIANT,
                "quantity": "1",
            }
        )
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(inactive.status_code, 404)
        self.assertIn(b"not available", unknown.data)
        self.assertIn(b"not available", inactive.data)

    def test_cart_quantity_remove_and_reset(self):
        self.login_session()
        add = {
            "tenant_id": TENANT_A,
            "action": "add_cart",
            "product_id": PRODUCT_A,
            "variant_id": VARIANT_A,
            "quantity": "1",
        }
        self.assertEqual(self.post(add).status_code, 200)
        update = {**add, "action": "update_cart", "quantity": "3"}
        page = self.post(update).get_data(as_text=True)
        self.assertIn("KES 1500.00", page)
        remove = {**add, "action": "remove_cart"}
        page = self.post(remove).get_data(as_text=True)
        self.assertIn("demo cart is empty", page)

        self.post(add)
        reset = self.post({"tenant_id": TENANT_A, "start_new": "1"})
        self.assertEqual(reset.status_code, 302)
        page = self.client.get(f"/playground?tenant_id={TENANT_A}").get_data(as_text=True)
        self.assertIn("View cart", page)
        self.assertNotIn("View cart (1)", page)

    def test_checkout_is_demo_only_and_missing_configuration_is_not_invented(self):
        self.login_session()
        self.post(
            {
                "tenant_id": TENANT_A,
                "action": "add_cart",
                "product_id": PRODUCT_A,
                "variant_id": VARIANT_A,
                "quantity": "1",
            }
        )
        checkout = self.post({"tenant_id": TENANT_A, "action": "checkout"})
        page = checkout.get_data(as_text=True)
        self.assertIn("Delivery:</strong>\n        Not configured", page)
        self.assertIn("Payment:</strong>\n        Not configured", page)
        self.assertNotIn("M-Pesa", page)
        self.assertNotIn("Cash on delivery", page)
        self.assertNotIn('name="delivery_details"', page)

        confirmation = self.post(
            {
                "tenant_id": TENANT_A,
                "action": "confirm_checkout",
                "customer_name": "Demo Customer",
            }
        )
        page = confirmation.get_data(as_text=True)
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn("DEMO", page)
        self.assertIn("NOT A REAL ORDER", page)
        self.assertIn("No order, inventory reservation, payment or WhatsApp message was created", page)
        self.respond.assert_not_called()
        for mutator in self.channel_mutators:
            mutator.assert_not_called()

    def test_explicit_delivery_and_payment_configuration_is_shown(self):
        self.login_session()
        self.contexts[TENANT_A]["settings"]["configuration"] = {
            "delivery_methods": ["Courier"],
            "payment_methods": ["Invoice"],
        }
        self.post(
            {
                "tenant_id": TENANT_A,
                "action": "add_cart",
                "product_id": PRODUCT_A,
                "variant_id": VARIANT_A,
                "quantity": "1",
            }
        )
        page = self.post({"tenant_id": TENANT_A, "action": "checkout"}).get_data(as_text=True)
        self.assertIn("Configured: Courier", page)
        self.assertIn("Configured: Invoice", page)
        self.assertIn('name="delivery_details"', page)

    def test_cart_is_scoped_by_tenant_and_authenticated_session(self):
        self.login_session()
        self.post(
            {
                "tenant_id": TENANT_A,
                "action": "add_cart",
                "product_id": PRODUCT_A,
                "variant_id": VARIANT_A,
                "quantity": "1",
            }
        )
        tenant_b = self.post({"tenant_id": TENANT_B, "action": "view_cart"}).get_data(as_text=True)
        self.assertIn("demo cart is empty", tenant_b)

        other_client = self.app.test_client()
        with other_client.session_transaction() as other_session:
            other_session["user_id"] = ADMIN_ID
            other_session["email"] = "other-admin@example.com"
            other_session["exp"] = time.time() + 600
            other_session["csrf_token"] = "other-csrf"
        other_page = other_client.post(
            "/playground",
            data={
                "csrf_token": "other-csrf",
                "tenant_id": TENANT_A,
                "action": "view_cart",
            },
        ).get_data(as_text=True)
        self.assertIn("demo cart is empty", other_page)

    def test_state_changing_actions_require_csrf(self):
        self.login_session()
        response = self.client.post(
            "/playground", data={"tenant_id": TENANT_A, "action": "clear_cart"}
        )
        self.assertEqual(response.status_code, 400)

    def test_playground_never_calls_live_channel_mutators(self):
        self.login_session()
        private_message = "send-this-private-message-live"
        with self.assertLogs("platform_admin.playground", level="INFO") as captured:
            response = self.post({"tenant_id": TENANT_A, "customer_message": private_message})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.load_context.call_count, 1)
        diagnostic = "\n".join(captured.output)
        self.assertIn("Playground request timing total_ms=", diagnostic)
        self.assertIn("outcome=success", diagnostic)
        self.assertNotIn(private_message, diagnostic)
        for mutator in self.channel_mutators:
            mutator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
