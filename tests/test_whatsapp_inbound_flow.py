"""Reproduction tests for the inbound WhatsApp flow, against an isolated test database.

These exist because a reported symptom claimed that a *new* number's first inbound message
produces no bot response, while a number the business had contacted first works. These tests
pin the actual behaviour of the real route with real database writes.

Every scenario uses the real webhook endpoint with a genuinely signed payload; only the
external provider (AI + Meta send) is stubbed, because those are outbound network calls.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from platform_admin.app import create_app
from platform_admin.config import Settings
from saas.bot import BotResponseRequest

try:
    from .saas_test_support import (
        TENANT_ONE_ID,
        SaasDatabaseTestCase,
        _connect,
        app_backend_test_url,
    )
except ImportError:  # discovery inside the tests directory
    from saas_test_support import (  # type: ignore[no-redef]
        TENANT_ONE_ID,
        SaasDatabaseTestCase,
        _connect,
        app_backend_test_url,
    )

SECRET = "app-secret"
CHANNEL_PHONE_ID = "dev-phone-id-cindy-001"   # the seeded channel for tenant 001
CREDENTIALS_REF = f"tenants/{TENANT_ONE_ID}/whatsapp"
# A number that has never been seen, so "first ever inbound message" is genuinely tested.
NEW_NUMBER = "+254700999888"


class _FakeProvider:
    """Stands in for the AI provider; records calls so we can prove the bot was invoked."""

    def __init__(self, failure: Exception | None = None):
        self.requests: list[BotResponseRequest] = []
        self.failure = failure

    def generate(self, request: BotResponseRequest) -> str:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return "Bot reply for: " + request.customer_message


def _settings() -> Settings:
    return Settings(
        secret_key="s" * 40,
        cookie_secure=False,
        app_database_url=app_backend_test_url(),
        whatsapp_verify_token="verify-me",
        whatsapp_app_secret=SECRET,
        whatsapp_send_enabled=True,          # so the send path is genuinely exercised
    )


def _payload(message_id: str, sender: str, text: str, phone_number_id: str = CHANNEL_PHONE_ID) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": phone_number_id},
            "contacts": [{"profile": {"name": "Test"}, "wa_id": sender.lstrip("+")}],
            "messages": [{
                "id": message_id,
                "from": sender.lstrip("+"),
                "timestamp": "1792210000",
                "type": "text",
                "text": {"body": text},
            }],
        }}]}],
    }


class _InboundFlowTestCase(SaasDatabaseTestCase):
    def setUp(self):
        self._cleanup()
        self.provider = _FakeProvider()
        self.app = create_app(_settings(), bot_provider=self.provider)
        self.client = self.app.test_client()

    def _cleanup(self):
        conn = _connect()
        with conn, conn.cursor() as cursor:
            for table in ("messages", "conversations", "customers",
                          "whatsapp_webhook_events", "whatsapp_conversations"):
                cursor.execute(f"delete from public.{table} where tenant_id = %s", (TENANT_ONE_ID,))

    def tearDown(self):
        self._cleanup()

    def _post(self, payload: dict, secret: str = SECRET):
        body = json.dumps(payload).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/webhooks/whatsapp",
            data=body,
            headers={"Content-Type": "application/json",
                     "X-Hub-Signature-256": f"sha256={signature}"},
        )

    def _counts(self) -> dict[str, int]:
        conn = _connect()
        with conn.cursor() as cursor:
            cursor.execute("select set_config('app.tenant_id', %s, true)", (TENANT_ONE_ID,))
            out = {}
            for table in ("customers", "conversations", "messages", "whatsapp_webhook_events"):
                cursor.execute(f"select count(*) from public.{table}")
                out[table] = cursor.fetchone()[0]
        conn.close()
        return out

    # ------------------------------------------------------------------ scenarios
    @patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.reply-1")
    def test_first_message_from_an_unknown_number_is_answered(self, send):
        """The reported bug: a brand-new number sends its very first message."""
        before = self._counts()
        response = self._post(_payload("wamid.first-1", NEW_NUMBER, "hello"))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json, {"ok": True, "message_id": "wamid.reply-1"})
        self.assertNotIn("ignored", response.json, "a first inbound message must not be ignored")

        after = self._counts()
        self.assertEqual(after["customers"], before["customers"] + 1, "customer must be created")
        self.assertEqual(after["conversations"], before["conversations"] + 1, "conversation must be created")
        self.assertEqual(after["messages"], before["messages"] + 2, "inbound + outbound must be stored")
        self.assertEqual(after["whatsapp_webhook_events"], before["whatsapp_webhook_events"] + 1)
        self.assertEqual(len(self.provider.requests), 1, "the bot must be invoked")
        send.assert_called_once()

    @patch("platform_admin.routes.whatsapp_service.send_text",
           side_effect=["wamid.reply-2a", "wamid.reply-2b"])
    def test_second_message_reuses_customer_and_conversation(self, send):
        self._post(_payload("wamid.second-1", NEW_NUMBER, "first"))
        after_first = self._counts()
        response = self._post(_payload("wamid.second-2", NEW_NUMBER, "second"))
        self.assertEqual(response.status_code, 200)
        after_second = self._counts()
        self.assertEqual(after_second["customers"], after_first["customers"], "no duplicate customer")
        self.assertEqual(after_second["conversations"], after_first["conversations"], "thread reused")
        self.assertEqual(after_second["messages"], after_first["messages"] + 2)

    @patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.reply-3")
    def test_duplicate_delivery_is_not_processed_twice(self, send):
        payload = _payload("wamid.dupe-1", NEW_NUMBER, "only once")
        self.assertEqual(self._post(payload).status_code, 200)
        counts_after_first = self._counts()
        again = self._post(payload)
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json.get("duplicate"), "a redelivery must be reported as duplicate")
        self.assertEqual(self._counts(), counts_after_first, "no extra rows on redelivery")
        self.assertEqual(send.call_count, 1, "no second reply")

    def test_unknown_channel_is_rejected(self):
        response = self._post(_payload("wamid.unknown-1", NEW_NUMBER, "hi", phone_number_id="no-such-id"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._counts()["customers"], 0)

    def test_bad_signature_is_rejected(self):
        response = self._post(_payload("wamid.badsig-1", NEW_NUMBER, "hi"), secret="wrong-secret")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._counts()["whatsapp_webhook_events"], 0)

    @patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.reply-4")
    def test_ai_provider_failure_marks_the_event_failed(self, send):
        self.provider.failure = RuntimeError("provider exploded")
        response = self._post(_payload("wamid.aifail-1", NEW_NUMBER, "hello"))
        self.assertEqual(response.status_code, 502)
        send.assert_not_called()
        conn = _connect()
        with conn.cursor() as cursor:
            cursor.execute("select set_config('app.tenant_id', %s, true)", (TENANT_ONE_ID,))
            cursor.execute("select status from public.whatsapp_webhook_events")
            self.assertEqual(cursor.fetchone()[0], "failed")
        conn.close()

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_outbound_api_failure_marks_the_event_failed(self, send):
        from platform_admin.services.validation import ServiceError

        send.side_effect = ServiceError("WhatsApp message delivery failed.", 502)
        response = self._post(_payload("wamid.sendfail-1", NEW_NUMBER, "hello"))
        self.assertEqual(response.status_code, 502)
        conn = _connect()
        with conn.cursor() as cursor:
            cursor.execute("select set_config('app.tenant_id', %s, true)", (TENANT_ONE_ID,))
            cursor.execute("select status from public.whatsapp_webhook_events")
            self.assertEqual(cursor.fetchone()[0], "failed")
        conn.close()

    @patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.reply-5")
    @patch("platform_admin.routes.customer_service.get_or_create_customer")
    def test_persistence_failure_does_not_prevent_the_reply(self, get_customer, send):
        """Persistence is deliberately non-fatal, so a DB problem must not silence the bot."""
        get_customer.side_effect = RuntimeError("db down")
        response = self._post(_payload("wamid.persistfail-1", NEW_NUMBER, "hello"))
        self.assertEqual(response.status_code, 200)
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
