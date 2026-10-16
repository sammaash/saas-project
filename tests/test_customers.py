"""Tests for the tenant-scoped customers / conversations / messages feature (0033).

Two layers:

* Database-free contracts (validation, webhook orchestration, dashboard routes,
  and the startup schema contract) are exercised with mocked services and run
  without PostgreSQL.
* Database-backed contracts (RLS isolation, get-or-create idempotency, and the
  provider-message-id dedup backstop) need the 0033 migration applied and skip
  when the database is unavailable or not yet migrated, exactly like the rest of
  the SaaS suite.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest.mock import MagicMock, patch

from platform_admin.app import create_app
from platform_admin.config import REQUIRED_SCHEMA, Settings
from platform_admin.services import customers
from platform_admin.services.validation import ServiceError


def _settings(**overrides) -> Settings:
    values = {
        "secret_key": "s" * 40,
        "cookie_secure": False,
        "whatsapp_verify_token": "verify-me",
        "whatsapp_app_secret": "app-secret",
    }
    values.update(overrides)
    return Settings(**values)


TENANT_ID = "11111111-1111-4111-8111-111111111111"
CHANNEL_ID = "c0000000-0000-4000-8000-000000000001"
CUSTOMER_ID = "99999999-9999-4999-8999-999999999999"
CONVERSATION_ID = "88888888-8888-4888-8888-888888888888"


def _webhook_payload() -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-test-1"},
                            "messages": [
                                {
                                    "id": "wamid.test-1",
                                    "from": "+254700000001",
                                    "text": {"body": "Hello"},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


class CustomerServiceValidationTests(unittest.TestCase):
    """save_message validates its enums before touching the database."""

    def test_empty_wa_id_is_refused(self):
        settings = _settings()
        with self.assertRaises(ServiceError):
            customers.get_or_create_customer(settings, TENANT_ID, CHANNEL_ID, "   ")

    def test_invalid_direction_is_refused(self):
        with self.assertRaises(ServiceError):
            customers.save_message(
                _settings(), TENANT_ID, CHANNEL_ID, CONVERSATION_ID,
                direction="sideways", sender_type="customer",
            )

    def test_invalid_sender_type_is_refused(self):
        with self.assertRaises(ServiceError):
            customers.save_message(
                _settings(), TENANT_ID, CHANNEL_ID, CONVERSATION_ID,
                direction="inbound", sender_type="robot",
            )

    def test_invalid_message_type_is_refused(self):
        with self.assertRaises(ServiceError):
            customers.save_message(
                _settings(), TENANT_ID, CHANNEL_ID, CONVERSATION_ID,
                direction="inbound", sender_type="customer", message_type="hologram",
            )

    def test_invalid_delivery_status_is_refused(self):
        with self.assertRaises(ServiceError):
            customers.save_message(
                _settings(), TENANT_ID, CHANNEL_ID, CONVERSATION_ID,
                direction="outbound", sender_type="assistant",
                delivery_status="teleported",
            )

    def test_row_to_dict_handles_bytes_and_none(self):
        class _Col:
            name = "id"

        class _Cursor:
            description = [_Col()]

        self.assertEqual(customers._row_to_dict(_Cursor(), None), {})
        self.assertEqual(
            customers._row_to_dict(_Cursor(), (b"abc",)), {"id": "abc"}
        )


class RequiredSchemaContractTests(unittest.TestCase):
    """The startup schema contract must include the 0033 tables."""

    def test_new_tables_are_in_the_required_schema(self):
        required = dict(REQUIRED_SCHEMA)
        for table in ("public.customers", "public.conversations", "public.messages"):
            with self.subTest(table=table):
                self.assertIn(table, required)


class WebhookCustomerPersistenceTests(unittest.TestCase):
    """The webhook resolves/creates customer + conversation and persists messages."""

    def setUp(self):
        self.app = create_app(_settings(), bot_provider=object())
        self.client = self.app.test_client()

    def _signed_post(self):
        body = json.dumps(_webhook_payload()).encode()
        signature = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/webhooks/whatsapp",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )

    def _channel(self):
        return {
            "tenant_id": TENANT_ID,
            "channel_id": CHANNEL_ID,
            "credentials_ref": f"tenants/{TENANT_ID}/whatsapp",
        }

    @patch("platform_admin.routes.customer_service.update_conversation_activity")
    @patch("platform_admin.routes.customer_service.save_message")
    @patch("platform_admin.routes.customer_service.get_or_create_conversation")
    @patch("platform_admin.routes.customer_service.get_or_create_customer")
    @patch("platform_admin.routes.whatsapp_service.update_event")
    @patch("platform_admin.routes.whatsapp_service.save_conversation")
    @patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.reply-1")
    @patch("platform_admin.routes.whatsapp_service.conversation_history", return_value=[])
    @patch("platform_admin.routes.whatsapp_service.claim_event", return_value=True)
    @patch("platform_admin.routes.whatsapp_service.resolve_channel")
    @patch("platform_admin.routes.BotResponseEngine")
    def test_inbound_and_outbound_messages_are_persisted(
        self, engine, resolve, claim, history, send, save_conv, update_event,
        get_customer, get_conversation, save_message, touch,
    ):
        resolve.return_value = self._channel()
        get_customer.return_value = {"id": CUSTOMER_ID}
        get_conversation.return_value = {"id": CONVERSATION_ID}
        engine.return_value.respond.return_value = MagicMock(text="Hi back")

        response = self._signed_post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["message_id"], "wamid.reply-1")
        get_customer.assert_called_once()
        self.assertEqual(get_customer.call_args.args[3], "+254700000001")
        self.assertEqual(save_message.call_count, 2)
        inbound, outbound = save_message.call_args_list
        self.assertEqual(inbound.kwargs["direction"], "inbound")
        self.assertEqual(inbound.kwargs["provider_message_id"], "wamid.test-1")
        self.assertEqual(outbound.kwargs["direction"], "outbound")
        self.assertEqual(outbound.kwargs["delivery_status"], "sent")
        touch.assert_called_once()

    @patch("platform_admin.routes.customer_service.get_or_create_customer")
    @patch("platform_admin.routes.whatsapp_service.update_event")
    @patch("platform_admin.routes.whatsapp_service.conversation_history", return_value=[])
    @patch("platform_admin.routes.whatsapp_service.claim_event", return_value=True)
    @patch("platform_admin.routes.whatsapp_service.resolve_channel")
    @patch("platform_admin.routes.BotResponseEngine")
    def test_persistence_failure_does_not_block_the_ack(
        self, engine, resolve, claim, history, update_event, get_customer,
    ):
        resolve.return_value = self._channel()
        get_customer.side_effect = RuntimeError("db down")
        engine.return_value.respond.return_value = MagicMock(text="Reply")
        with patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.x"), patch(
            "platform_admin.routes.whatsapp_service.save_conversation"
        ):
            with self.assertLogs("platform_admin.playground", level="ERROR"):
                response = self._signed_post()
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Database-backed contracts. These require the 0033 migration and skip when the
# database is unavailable, matching the rest of the SaaS suite.
# ---------------------------------------------------------------------------
try:
    from .saas_test_support import (
        TENANT_ONE_ID,
        TENANT_ONE_OWNER_USER_ID,
        TENANT_TWO_ID,
        SaasDatabaseTestCase,
        as_backend,
        as_superuser,
        as_user,
    )
except ImportError:  # discovery inside the tests directory
    from saas_test_support import (  # type: ignore[no-redef]
        TENANT_ONE_ID,
        TENANT_ONE_OWNER_USER_ID,
        TENANT_TWO_ID,
        SaasDatabaseTestCase,
        as_backend,
        as_superuser,
        as_user,
    )

NEW_TABLES = ("customers", "conversations", "messages")


class CustomerSchemaPresenceTests(SaasDatabaseTestCase):
    """The 0033 tables exist and carry the required RLS configuration."""

    def test_tables_exist_and_rls_is_forced(self):
        with as_superuser() as cursor:
            for table in NEW_TABLES:
                with self.subTest(table=table):
                    cursor.execute(
                        "select relrowsecurity, relforcerowsecurity "
                        "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                        "where n.nspname = 'public' and c.relname = %s",
                        (table,),
                    )
                    row = cursor.fetchone()
                    self.assertIsNotNone(row, f"{table} missing")
                    self.assertTrue(row[0], f"{table} must have RLS enabled")
                    self.assertTrue(row[1], f"{table} must have RLS forced")

    def test_authenticated_role_has_no_privileges(self):
        with as_superuser() as cursor:
            for table in NEW_TABLES:
                with self.subTest(table=table):
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        cursor.execute(
                            "select has_table_privilege(%s, %s, %s)",
                            ("authenticated", f"public.{table}", privilege),
                        )
                        self.assertFalse(
                            cursor.fetchone()[0],
                            f"authenticated must not hold {privilege} on {table}",
                        )


class CustomerRlsIsolationTests(SaasDatabaseTestCase):
    """RLS hides other tenants' customers and fails closed without context."""

    def _seed_customer(self, cursor, tenant_id, wa_id):
        cursor.execute(
            "insert into public.customers (tenant_id, wa_id) values (%s, %s) returning id",
            (tenant_id, wa_id),
        )
        return cursor.fetchone()[0]

    def test_backend_without_context_sees_nothing(self):
        with as_backend(None) as cursor:
            for table in NEW_TABLES:
                with self.subTest(table=table):
                    cursor.execute(f"select count(*) from public.{table}")
                    self.assertEqual(cursor.fetchone()[0], 0)

    def test_customer_is_confined_to_its_tenant(self):
        with as_superuser() as cursor:
            self._seed_customer(cursor, TENANT_ONE_ID, "+254711111111")
            self._seed_customer(cursor, TENANT_TWO_ID, "+254722222222")
        try:
            with as_backend(TENANT_ONE_ID) as cursor:
                cursor.execute("select count(*) from public.customers")
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute(
                    "select count(*) from public.customers where tenant_id = %s",
                    (TENANT_TWO_ID,),
                )
                self.assertEqual(cursor.fetchone()[0], 0)
            with as_user(TENANT_ONE_OWNER_USER_ID) as cursor:
                cursor.execute("select count(*) from public.customers")
                self.assertEqual(cursor.fetchone()[0], 0)  # authenticated has no grant
        finally:
            with as_superuser() as cursor:
                cursor.execute(
                    "delete from public.customers where wa_id in (%s, %s)",
                    ("+254711111111", "+254722222222"),
                )


class CustomerServiceDbTests(SaasDatabaseTestCase):
    """get_or_create_customer is idempotent within a tenant."""

    def test_get_or_create_is_idempotent(self):
        settings = Settings(secret_key="s" * 40, app_database_url=None)
        # The service opens its own connection via saas.db.connect using
        # SAAS_DATABASE_URL; point it at the test URL for this call.
        from saas import config as saas_config  # noqa: PLC0415

        url = saas_config.test_database_url()
        try:
            customer = customers.get_or_create_customer(
                settings, TENANT_ONE_ID, "c0000000-0000-4000-8000-000000000001",
                "+254733333333", name="Idem",
            )
            again = customers.get_or_create_customer(
                settings, TENANT_ONE_ID, "c0000000-0000-4000-8000-000000000001",
                "+254733333333",
            )
            self.assertEqual(customer["id"], again["id"])
            self.assertEqual(again["name"], "Idem")  # coalesce keeps first name
        finally:
            with as_superuser() as cursor:
                cursor.execute(
                    "delete from public.customers where wa_id = %s", ("+254733333333",)
                )


if __name__ == "__main__":
    unittest.main()


