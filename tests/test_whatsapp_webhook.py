"""Database-free contracts for verified WhatsApp webhook handling."""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest.mock import MagicMock, patch

from platform_admin.app import create_app
from platform_admin.config import Settings
from platform_admin.services import whatsapp
from platform_admin.services.validation import ServiceError


def _settings(**overrides):
    values = {
        "secret_key": "s" * 40,
        "cookie_secure": False,
        "whatsapp_verify_token": "verify-me",
        "whatsapp_app_secret": "app-secret",
    }
    values.update(overrides)
    return Settings(**values)


def _payload():
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "phone-test-1"},
                    "messages": [{
                        "id": "wamid.test-1",
                        "from": "+254700000001",
                        "text": {"body": "Hello"},
                    }],
                }
            }]
        }],
    }


class WhatsAppWebhookTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_settings(), bot_provider=object())
        self.client = self.app.test_client()

    def _signed_post(self, payload=None, secret="app-secret"):
        body = json.dumps(payload or _payload()).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/webhooks/whatsapp",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )

    def test_verification_requires_token_and_returns_challenge(self):
        response = self.client.get(
            "/webhooks/whatsapp",
            query_string={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "123"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"123")
        self.assertEqual(
            self.client.get(
                "/webhooks/whatsapp",
                query_string={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "123"},
            ).status_code,
            403,
        )

    def test_signature_and_malformed_payload_are_rejected_or_ignored(self):
        body = json.dumps(_payload()).encode()
        response = self.client.post(
            "/webhooks/whatsapp",
            data=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bad"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self._signed_post({"object": "whatsapp_business_account", "entry": []}).status_code,
            200,
        )

    @patch("platform_admin.routes.whatsapp_service.resolve_channel")
    def test_unknown_channel_is_not_processed(self, resolve):
        resolve.return_value = None
        response = self._signed_post()
        self.assertEqual(response.status_code, 404)
        resolve.assert_called_once_with(self.app.extensions["platform"].settings, "phone-test-1")

    @patch("platform_admin.routes.whatsapp_service.claim_event", return_value=False)
    @patch("platform_admin.routes.whatsapp_service.resolve_channel")
    def test_duplicate_message_is_acknowledged_without_processing(self, resolve, claim):
        resolve.return_value = {"tenant_id": "tenant-1", "channel_id": "channel-1"}
        response = self._signed_post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "duplicate": True})

    @patch("platform_admin.routes.whatsapp_service.update_event")
    @patch("platform_admin.routes.whatsapp_service.save_conversation")
    @patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.reply-1")
    @patch("platform_admin.routes.whatsapp_service.conversation_history", return_value=[])
    @patch("platform_admin.routes.whatsapp_service.claim_event", return_value=True)
    @patch("platform_admin.routes.whatsapp_service.resolve_channel")
    @patch("platform_admin.routes.BotResponseEngine")
    def test_valid_message_is_routed_once_and_sent(
        self, engine, resolve, claim, history, send, save, update
    ):
        resolve.return_value = {
            "tenant_id": "tenant-1",
            "channel_id": "channel-1",
            "credentials_ref": "tenants/tenant-1/whatsapp",
        }
        engine.return_value.respond.return_value.text = "Welcome"
        response = self._signed_post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "message_id": "wamid.reply-1"})
        send.assert_called_once()
        save.assert_called_once()
        update.assert_called_once_with(
            self.app.extensions["platform"].settings,
            "tenant-1",
            "wamid.test-1",
            "processed",
            response_message_id="wamid.reply-1",
        )

    # A genuine Meta message id, captured from a live send on 2026-10-17. It ends in base64
    # padding. An earlier MESSAGE_ID_RE excluded "+", "/" and "=", so every real inbound
    # message failed validation and was silently ignored (200 {"ignored": true}, nothing
    # written) while the simplified "wamid.test-1" fixture kept this suite green.
    REAL_WAMID = "wamid.HBgMMjU0NzIzNTk1MzM0FQIAERgSRjk2NDBCNUIwQzFGQkI5RkQyAA=="

    def _payload_with_real_id(self):
        payload = _payload()
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = self.REAL_WAMID
        return payload

    def test_a_real_meta_message_id_parses(self):
        from platform_admin.services import whatsapp as whatsapp_service

        event = whatsapp_service.parse_event(self._payload_with_real_id())
        self.assertIsNotNone(event, "a genuine Meta wamid must not be dropped")
        self.assertEqual(event["message_id"], self.REAL_WAMID)
        self.assertEqual(event["customer_key"], "+254700000001")

    @patch("platform_admin.routes.whatsapp_service.update_event")
    @patch("platform_admin.routes.whatsapp_service.save_conversation")
    @patch("platform_admin.routes.whatsapp_service.send_text", return_value="wamid.reply-1")
    @patch("platform_admin.routes.whatsapp_service.conversation_history", return_value=[])
    @patch("platform_admin.routes.whatsapp_service.claim_event", return_value=True)
    @patch("platform_admin.routes.whatsapp_service.resolve_channel")
    @patch("platform_admin.routes.BotResponseEngine")
    def test_a_real_meta_message_id_is_replied_to_not_ignored(
        self, engine, resolve, claim, history, send, save, update
    ):
        resolve.return_value = {
            "tenant_id": "tenant-1",
            "channel_id": "channel-1",
            "credentials_ref": "tenants/tenant-1/whatsapp",
        }
        engine.return_value.respond.return_value.text = "Welcome"
        response = self._signed_post(self._payload_with_real_id())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("ignored", response.json, "a real message must not be silently ignored")
        send.assert_called_once()

    def test_webhook_post_does_not_require_dashboard_csrf(self):
        response = self._signed_post({"object": "not-whatsapp"})
        self.assertEqual(response.status_code, 200)

    @patch("platform_admin.routes.whatsapp_service.update_event")
    @patch("platform_admin.routes.whatsapp_service.conversation_history", return_value=[])
    @patch("platform_admin.routes.whatsapp_service.claim_event", return_value=True)
    @patch("platform_admin.routes.whatsapp_service.resolve_channel")
    @patch("platform_admin.routes.BotResponseEngine")
    def test_provider_failure_is_sanitized_and_marks_event_failed(
        self, engine, resolve, claim, history, update
    ):
        resolve.return_value = {
            "tenant_id": "tenant-1",
            "channel_id": "channel-1",
            "credentials_ref": "tenants/tenant-1/whatsapp",
        }
        engine.return_value.respond.side_effect = RuntimeError(
            "private customer text and provider credential"
        )
        with self.assertLogs("platform_admin.playground", level="ERROR") as captured:
            response = self._signed_post()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json["error"], "WhatsApp message processing failed.")
        diagnostic = "\n".join(captured.output)
        self.assertIn("RuntimeError", diagnostic)
        self.assertNotIn("private customer text", diagnostic)
        self.assertNotIn("provider credential", diagnostic)
        update.assert_called_once_with(
            self.app.extensions["platform"].settings,
            "tenant-1",
            "wamid.test-1",
            "failed",
            "processing_error",
        )


class WhatsAppSenderTests(unittest.TestCase):
    def test_sending_is_disabled_by_default_without_reading_credentials(self):
        settings = Settings(secret_key="s" * 40)
        with patch("platform_admin.services.whatsapp.access_token") as token, patch(
            "platform_admin.services.whatsapp.urllib.request.urlopen"
        ) as urlopen:
            with self.assertRaisesRegex(ServiceError, "disabled"):
                whatsapp.send_text(
                    settings,
                    phone_number_id="phone-test-1",
                    tenant_id="tenant-1",
                    credentials_ref="tenants/tenant-1/whatsapp",
                    recipient="+254700000001",
                    text="Hello",
                )
        token.assert_not_called()
        urlopen.assert_not_called()

    @patch("platform_admin.services.whatsapp.access_token", return_value="test-token")
    @patch("platform_admin.services.whatsapp.urllib.request.urlopen")
    def test_enabled_sender_uses_resolved_token_without_exposing_it(self, urlopen, token):
        response = MagicMock()
        response.read.return_value = b'{"messages":[{"id":"wamid.reply-1"}]}'
        urlopen.return_value.__enter__.return_value = response
        settings = Settings(secret_key="s" * 40, whatsapp_send_enabled=True)

        message_id = whatsapp.send_text(
            settings,
            phone_number_id="phone-test-1",
            tenant_id="tenant-1",
            credentials_ref="tenants/tenant-1/whatsapp",
            recipient="+254700000001",
            text="Hello",
        )

        self.assertEqual(message_id, "wamid.reply-1")
        outbound = urlopen.call_args.args[0]
        self.assertEqual(outbound.get_header("Authorization"), "Bearer test-token")
        self.assertNotIn("test-token", outbound.full_url)


if __name__ == "__main__":
    unittest.main()
