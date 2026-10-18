"""End-to-end webhook outbox behaviour against a migrated PostgreSQL database.

The Flask route runs as the real ``app_backend`` role, so RLS and grants are exercised;
only the outbound Meta call and the AI provider are stubbed (network).

Skipped unless ``SAAS_TEST_DATABASE_URL`` names a database that has migration 0035 and
the ``db/local`` app_backend login. A skip is visible, never a silent pass.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from platform_admin.app import create_app
from platform_admin.config import Settings
from platform_admin.services import whatsapp as whatsapp_service

try:
    from .saas_test_support import (
        _connect,
        app_backend_test_url,
        test_database_url as _test_database_url,
    )
except ImportError:  # discovery inside the tests directory
    from saas_test_support import (  # type: ignore[no-redef]
        _connect,
        app_backend_test_url,
        test_database_url as _test_database_url,
    )

SECRET = "app-secret"


class _FakeProvider:
    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return "Thanks for your message."


def _inbound_payload(message_id, sender, body, phone_number_id):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": phone_number_id},
            "messages": [{
                "id": message_id,
                "from": sender,
                "type": "text",
                "text": {"body": body},
            }],
        }}]}],
    }


def _status_payload(wamid, status, phone_number_id, errors=None):
    entry = {"id": wamid, "status": status, "timestamp": "1792210000",
             "recipient_id": "254700000001"}
    if errors is not None:
        entry["errors"] = errors
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": phone_number_id},
            "statuses": [entry],
        }}]}],
    }


@contextmanager
def _super_cursor():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _rows(sql, params=()):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [column.name for column in cur.description]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    finally:
        conn.close()


class WebhookOutboxIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = _test_database_url()
        backend = app_backend_test_url()
        if not base or not backend:
            raise unittest.SkipTest(
                "SAAS_TEST_DATABASE_URL is not configured; integration tests skipped."
            )
        try:
            conn = _connect()
        except Exception as error:  # noqa: BLE001 - skip rather than fail on infra
            raise unittest.SkipTest(f"test database unreachable: {type(error).__name__}")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select 1 from information_schema.columns where table_name = 'messages' "
                    "and column_name = 'reply_to_provider_message_id'"
                )
                if cur.fetchone() is None:
                    raise unittest.SkipTest("migration 0035 is not applied to this database.")
                cur.execute(
                    "select 1 from pg_roles where rolname = 'app_backend' and rolcanlogin"
                )
                if cur.fetchone() is None:
                    raise unittest.SkipTest(
                        "app_backend has no LOGIN here; run the runner with --include-local."
                    )
        finally:
            conn.close()
        cls.app_backend_url = backend

    def setUp(self):
        self.provider = _FakeProvider()
        self.app = create_app(
            Settings(
                secret_key="s" * 40,
                cookie_secure=False,
                app_database_url=self.app_backend_url,
                whatsapp_verify_token="verify-me",
                whatsapp_app_secret=SECRET,
                whatsapp_send_enabled=True,
            ),
            bot_provider=self.provider,
        )
        self.client = self.app.test_client()
        self.tenants = []
        self.tenant_id, self.channel_id, self.phone_number_id = self._make_tenant()
        # WhatsApp ids are digits only; CUSTOMER_KEY_RE rejects letters.
        self.sender = "2547" + str(uuid.uuid4().int)[:9]

    def tearDown(self):
        with _super_cursor() as cur:
            for tenant_id in self.tenants:
                cur.execute("delete from public.tenants where id = %s", (tenant_id,))

    # -- fixtures ------------------------------------------------------------
    def _make_tenant(self):
        tenant_id = str(uuid.uuid4())
        channel_id = str(uuid.uuid4())
        phone_number_id = "it-" + uuid.uuid4().hex[:12]
        with _super_cursor() as cur:
            cur.execute(
                "insert into public.tenants (id, name, slug, status) values (%s,%s,%s,'active')",
                (tenant_id, "Integration", "it-" + uuid.uuid4().hex[:10]),
            )
            cur.execute(
                "insert into public.tenant_settings (tenant_id) values (%s)", (tenant_id,)
            )
            cur.execute(
                "insert into public.tenant_channels "
                "(id, tenant_id, phone_number_id, status, credentials_ref) "
                "values (%s,%s,%s,'active',%s)",
                (channel_id, tenant_id, phone_number_id,
                 f"tenants/{tenant_id}/whatsapp"),
            )
        self.tenants.append(tenant_id)
        return tenant_id, channel_id, phone_number_id


    def _make_thread(self, tenant_id, channel_id):
        customer_id = str(uuid.uuid4())
        conversation_id = str(uuid.uuid4())
        with _super_cursor() as cur:
            cur.execute(
                "insert into public.customers (id, tenant_id, wa_id) values (%s,%s,%s)",
                (customer_id, tenant_id, "2547" + uuid.uuid4().hex[:9]),
            )
            cur.execute(
                "insert into public.conversations "
                "(id, tenant_id, customer_id, channel_id, status) values (%s,%s,%s,%s,'open')",
                (conversation_id, tenant_id, customer_id, channel_id),
            )
        return conversation_id

    def _insert_outbound(self, tenant_id, channel_id, conversation_id,
                         inbound_wamid, provider_wamid=None):
        with _super_cursor() as cur:
            cur.execute(
                "insert into public.messages "
                "(tenant_id, channel_id, conversation_id, direction, sender_type, "
                " provider_message_id, message_type, body_text, payload, delivery_status, "
                " reply_to_provider_message_id) "
                "values (%s,%s,%s,'outbound','assistant',%s,'text','existing',%s,%s,%s)",
                (tenant_id, channel_id, conversation_id, provider_wamid,
                 json.dumps({"text": "existing"}),
                 "sent" if provider_wamid else None, inbound_wamid),
            )

    # -- helpers -------------------------------------------------------------
    def _post(self, payload):
        body = json.dumps(payload).encode()
        signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/webhooks/whatsapp",
            data=body,
            headers={"Content-Type": "application/json",
                     "X-Hub-Signature-256": f"sha256={signature}"},
        )

    def _inbound(self, message_id, body="hello"):
        return _inbound_payload(message_id, self.sender, body, self.phone_number_id)

    def _post_status(self, wamid, status, errors=None):
        return self._post(_status_payload(wamid, status, self.phone_number_id, errors))

    def _outbound(self, tenant_id=None):
        return _rows(
            "select provider_message_id, delivery_status, error_code, error_message, "
            "body_text, reply_to_provider_message_id from public.messages "
            "where tenant_id = %s and direction = 'outbound' order by created_at",
            (tenant_id or self.tenant_id,),
        )

    def _event(self, message_id, tenant_id=None):
        rows = _rows(
            "select status, error_code, response_message_id "
            "from public.whatsapp_webhook_events where tenant_id = %s and message_id = %s",
            (tenant_id or self.tenant_id, message_id),
        )
        return rows[0] if rows else None


    # -- scenarios -----------------------------------------------------------
    @patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.OUT-1")
    def test_successful_inbound_records_outbox_and_wamid(self, send):
        response = self._post(self._inbound("wamid.IN-1"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "message_id": "wamid.OUT-1"})
        send.assert_called_once()
        rows = self._outbound()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider_message_id"], "wamid.OUT-1")
        self.assertEqual(rows[0]["delivery_status"], "sent")
        self.assertEqual(rows[0]["reply_to_provider_message_id"], "wamid.IN-1")
        self.assertEqual(rows[0]["body_text"], "Thanks for your message.")
        event = self._event("wamid.IN-1")
        self.assertEqual(event["status"], "processed")
        self.assertEqual(event["response_message_id"], "wamid.OUT-1")

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_a_failed_send_is_recorded_once_without_retry(self, send):
        send.side_effect = whatsapp_service.WhatsAppSendError(
            "WhatsApp message delivery failed.", 502,
            provider_code="131047", provider_message="Re-engagement message",
        )
        response = self._post(self._inbound("wamid.IN-F"))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(send.call_count, 1, "a failed send must not be retried here")
        rows = self._outbound()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["provider_message_id"])
        self.assertEqual(rows[0]["delivery_status"], "failed")
        self.assertEqual(rows[0]["error_code"], "131047")
        self.assertEqual(rows[0]["error_message"], "Re-engagement message")
        event = self._event("wamid.IN-F")
        self.assertEqual(event["status"], "failed")

    @patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.OUT-D")
    def test_duplicate_inbound_is_acknowledged_without_a_second_reply(self, send):
        payload = self._inbound("wamid.IN-D")
        self.assertEqual(self._post(payload).status_code, 200)
        again = self._post(payload)
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json["duplicate"])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(self._outbound()), 1)

    @patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.OUT-R")
    def test_receipts_are_monotonic(self, send):
        self._post(self._inbound("wamid.IN-R"))
        self.assertEqual(self._post_status("wamid.OUT-R", "delivered").json["statuses"], 1)
        self.assertEqual(self._outbound()[0]["delivery_status"], "delivered")

        # A late "sent" must not move the row backwards.
        self._post_status("wamid.OUT-R", "sent")
        self.assertEqual(self._outbound()[0]["delivery_status"], "delivered")

        self._post_status("wamid.OUT-R", "read")
        self.assertEqual(self._outbound()[0]["delivery_status"], "read")

        # A failure after a read is ignored.
        self._post_status("wamid.OUT-R", "failed",
                          errors=[{"code": 131047, "message": "late"}])
        self.assertEqual(self._outbound()[0]["delivery_status"], "read")

    @patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.OUT-A")
    def test_an_ambiguous_outcome_is_not_resent(self, send):
        conversation_id = self._make_thread(self.tenant_id, self.channel_id)
        self._insert_outbound(self.tenant_id, self.channel_id, conversation_id,
                              "wamid.IN-A")

        response = self._post(self._inbound("wamid.IN-A"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ambiguous"])
        send.assert_not_called()
        rows = self._outbound()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["provider_message_id"])
        event = self._event("wamid.IN-A")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_code"], "ambiguous_send")

    @patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.OUT-X")
    def test_a_receipt_never_touches_another_tenant(self, send):
        other_id, other_channel, _ = self._make_tenant()
        other_conversation = self._make_thread(other_id, other_channel)
        self._insert_outbound(other_id, other_channel, other_conversation,
                              "wamid.IN-OTHER", provider_wamid="wamid.OUT-OTHER")

        response = self._post_status("wamid.OUT-OTHER", "read")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self._outbound(other_id)[0]["delivery_status"], "sent",
            "a receipt resolved through tenant A's channel must not update tenant B",
        )


if __name__ == "__main__":
    unittest.main()

