"""Database-backed privacy and staff-assistance tests for the menu.

These tests only run against an explicitly configured disposable test database. Without
``SAAS_TEST_DATABASE_URL`` they skip; they never fall back to an application DSN.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
import uuid
from unittest.mock import MagicMock, patch

from platform_admin.app import create_app
from platform_admin.config import Settings
from platform_admin.services import handover, menu
from saas.tenant_admin import TenantAdmin

try:
    from .saas_test_support import (
        TENANT_ONE_ID,
        TENANT_TWO_ID,
        SaasDatabaseTestCase,
        _connect,
        app_backend_test_url,
        as_backend,
        as_superuser,
    )
except ImportError:  # discovery inside the tests directory
    from saas_test_support import (  # type: ignore[no-redef]
        TENANT_ONE_ID,
        TENANT_TWO_ID,
        SaasDatabaseTestCase,
        _connect,
        app_backend_test_url,
        as_backend,
        as_superuser,
    )


def _run_committed(sql: str, params: tuple) -> None:
    conn = _connect()
    try:
        with conn, conn.cursor() as cursor:
            cursor.execute(sql, params)
    finally:
        conn.close()


class OrderPrivacyDbTests(SaasDatabaseTestCase):
    def test_lookup_is_scoped_to_the_sender_and_tenant(self):
        suffix = uuid.uuid4().hex[:10]
        own = f"MENU-{suffix}-A"
        another = f"MENU-{suffix}-B"
        without_sender = f"MENU-{suffix}-C"
        wa_a = f"2547{suffix}"
        wa_b = f"2548{suffix}"
        numbers = (own, another, without_sender)
        try:
            _run_committed(
                "insert into public.orders "
                "(tenant_id, order_number, customer_name, whatsapp_wa_id, status, currency) "
                "values (%s, %s, 'A', %s, 'draft', 'KES'), "
                "(%s, %s, 'B', %s, 'draft', 'KES'), "
                "(%s, %s, 'No sender', null, 'draft', 'KES')",
                (
                    TENANT_ONE_ID, own, wa_a,
                    TENANT_ONE_ID, another, wa_b,
                    TENANT_ONE_ID, without_sender,
                ),
            )
            with as_backend(TENANT_ONE_ID) as cursor:
                admin = TenantAdmin(cursor)
                self.assertIsNotNone(
                    admin.get_order_by_number_for_whatsapp(own, wa_a)
                )
                self.assertIsNone(
                    admin.get_order_by_number_for_whatsapp(another, wa_a)
                )
                self.assertIsNone(
                    admin.get_order_by_number_for_whatsapp("MISSING", wa_a)
                )
                self.assertIsNone(
                    admin.get_order_by_number_for_whatsapp(without_sender, wa_b)
                )
                self.assertIsNone(admin.get_order_by_number_for_whatsapp(own, " "))
                self.assertIsNone(admin.get_order_by_number_for_whatsapp(own, ""))

            # A different tenant cannot see the order even with the same sender identity.
            with as_backend(TENANT_TWO_ID) as cursor:
                self.assertIsNone(
                    TenantAdmin(cursor).get_order_by_number_for_whatsapp(own, wa_a)
                )
        finally:
            _run_committed(
                "delete from public.orders where order_number in (%s, %s, %s)",
                numbers,
            )


class MenuPersistenceDbTests(SaasDatabaseTestCase):
    """The real menu webhook persists an outbound row and a receipt can find it."""

    SECRET = "app-secret"
    PHONE_NUMBER_ID = "dev-phone-id-cindy-001"

    def setUp(self):
        self.url = app_backend_test_url()
        self.assertTrue(self.url, "the test database must be configured")
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "select configuration from public.tenant_settings "
                    "where tenant_id = %s",
                    (TENANT_ONE_ID,),
                )
                row = cursor.fetchone()
        if row is None:
            self.skipTest("seed tenant settings are not present")
        self.original_configuration = row[0] or {}
        self.settings = Settings(
            secret_key="s" * 40,
            cookie_secure=False,
            app_database_url=self.url,
            whatsapp_verify_token="verify-me",
            whatsapp_app_secret=self.SECRET,
            whatsapp_send_enabled=True,
        )
        menu.set_enabled(self.settings, TENANT_ONE_ID, True)
        self.app = create_app(self.settings, bot_provider=MagicMock())
        self.client = self.app.test_client()
        self.sender = "2547" + str(uuid.uuid4().int)[:9]
        self.inbound_id = f"wamid.menu-db-in-{uuid.uuid4().hex[:12]}"
        self.outbound_id = f"wamid.menu-db-out-{uuid.uuid4().hex[:12]}"

    def tearDown(self):
        _run_committed(
            "delete from public.messages where tenant_id = %s "
            "and (provider_message_id in (%s, %s) or reply_to_provider_message_id = %s)",
            (TENANT_ONE_ID, self.inbound_id, self.outbound_id, self.inbound_id),
        )
        _run_committed(
            "delete from public.conversations where tenant_id = %s and customer_id in "
            "(select id from public.customers where tenant_id = %s and wa_id = %s)",
            (TENANT_ONE_ID, TENANT_ONE_ID, self.sender),
        )
        _run_committed(
            "delete from public.customers where tenant_id = %s and wa_id = %s",
            (TENANT_ONE_ID, self.sender),
        )
        _run_committed(
            "delete from public.whatsapp_webhook_events "
            "where tenant_id = %s and message_id = %s",
            (TENANT_ONE_ID, self.inbound_id),
        )
        _run_committed(
            "update public.tenant_settings set configuration = %s::jsonb "
            "where tenant_id = %s",
            (json.dumps(self.original_configuration), TENANT_ONE_ID),
        )

    def _post(self, payload: dict):
        body = json.dumps(payload).encode()
        signature = hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/webhooks/whatsapp",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )


    def _inbound(self) -> dict:
        return {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": self.PHONE_NUMBER_ID},
                "contacts": [{"profile": {"name": "Menu Test"}, "wa_id": self.sender}],
                "messages": [{
                    "id": self.inbound_id,
                    "from": self.sender,
                    "timestamp": "1792210000",
                    "type": "text",
                    "text": {"body": "menu"},
                }],
            }}]}],
        }

    def _status(self) -> dict:
        return {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": self.PHONE_NUMBER_ID},
                "statuses": [{
                    "id": self.outbound_id,
                    "status": "delivered",
                    "timestamp": "1792210001",
                    "recipient_id": self.sender,
                }],
            }}]}],
        }

    def test_menu_reply_is_persisted_and_a_receipt_correlates(self):
        with patch(
            "platform_admin.routes.whatsapp_service.send_list",
            return_value=self.outbound_id,
        ) as send_list, patch(
            "platform_admin.routes.whatsapp_service.send_text"
        ) as send_text:
            response = self._post(self._inbound())
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.json.get("menu"))
        send_list.assert_called_once()
        send_text.assert_not_called()

        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "select direction, message_type, provider_message_id, "
                    "reply_to_provider_message_id, delivery_status, body_text "
                    "from public.messages where tenant_id = %s "
                    "and (provider_message_id in (%s, %s) "
                    "or reply_to_provider_message_id = %s)",
                    (TENANT_ONE_ID, self.inbound_id, self.outbound_id, self.inbound_id),
                )
                rows = {row[0]: row for row in cursor.fetchall()}
        self.assertEqual(set(rows), {"inbound", "outbound"})
        inbound, outbound = rows["inbound"], rows["outbound"]
        self.assertEqual(inbound[2], self.inbound_id)
        self.assertEqual(outbound[1], "interactive")
        self.assertEqual(outbound[2], self.outbound_id)
        self.assertEqual(outbound[3], self.inbound_id)
        self.assertEqual(outbound[4], "sent")
        self.assertIn("Browse Products", outbound[5])

        receipt = self._post(self._status())
        self.assertEqual(receipt.status_code, 200)
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "select delivery_status from public.messages "
                    "where tenant_id = %s and provider_message_id = %s",
                    (TENANT_ONE_ID, self.outbound_id),
                )
                self.assertEqual(cursor.fetchone()[0], "delivered")


class StaffAssistanceDbTests(SaasDatabaseTestCase):
    def setUp(self):
        self.url = app_backend_test_url()
        self.assertTrue(self.url, "the test database must be configured")
        self.settings = Settings(secret_key="s" * 40, app_database_url=self.url)
        with as_superuser() as cursor:
            cursor.execute("select to_regclass('public.handover_requests')")
            if cursor.fetchone()[0] is None:
                self.skipTest("handover_requests is not present in this test database")

    def _request(self, *, wa_id: str, message_id: str, notified: bool):
        with patch.object(handover, "deliver", return_value=notified) as deliver:
            result = handover.request_staff_assistance(
                self.settings,
                tenant_id=TENANT_ONE_ID,
                channel={},
                phone_number_id="dev-phone-id-cindy-001",
                customer_key=wa_id,
                customer=None,
                conversation=None,
                message_id=message_id,
                settings_document={},
                business_name="Cindy Bakes",
            )
        self.assertEqual(deliver.call_count, 1)
        return result

    def test_success_failure_and_duplicate_requests(self):
        suffix = uuid.uuid4().hex[:10]
        wa_ok = f"2547{suffix}"
        wa_fail = f"2548{suffix}"
        message_id = f"menu-test-{suffix}"
        try:
            notified, text = self._request(
                wa_id=wa_ok, message_id=message_id, notified=True
            )
            self.assertTrue(notified)
            self.assertIn("let the team know", text)

            # The same provider event is recorded once, even if the tap is retried.
            notified_again, _ = self._request(
                wa_id=wa_ok, message_id=message_id, notified=True
            )
            self.assertTrue(notified_again)
            with as_backend(TENANT_ONE_ID, url=self.url) as cursor:
                cursor.execute(
                    "select count(*) from public.handover_requests "
                    "where dedupe_key = %s",
                    (f"menu:{message_id}",),
                )
                self.assertEqual(cursor.fetchone()[0], 1)

            notified_failed, failed_text = self._request(
                wa_id=wa_fail, message_id=f"{message_id}-fail", notified=False
            )
            self.assertFalse(notified_failed)
            self.assertIn("couldn't reach", failed_text)
        finally:
            _run_committed(
                "delete from public.handover_requests where wa_id in (%s, %s)",
                (wa_ok, wa_fail),
            )


if __name__ == "__main__":
    unittest.main()
