"""Contracts for outbound reply correlation, delivery receipts and typed inbound events.

These tests are database-free: every persistence call is patched, because the point is
the *sequence* the route follows (record intent -> send -> record outcome) and the
guarantees it must not break. The DB-backed half lives in test_whatsapp_inbound_flow.py.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import unittest
import urllib.error
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from platform_admin.app import create_app
from platform_admin.config import Settings
from platform_admin.services import customers as customer_service
from platform_admin.services import whatsapp as whatsapp_service

PHONE_ID = "phone-test-1"
SECRET = "app-secret"


def _settings(**overrides):
    values = {
        "secret_key": "s" * 40,
        "cookie_secure": False,
        "whatsapp_verify_token": "verify-me",
        "whatsapp_app_secret": SECRET,
    }
    values.update(overrides)
    return Settings(**values)


def _text_payload(message_id="wamid.in-1", sender="254700000001", body="Hello"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PHONE_ID},
            "messages": [{
                "id": message_id,
                "from": sender,
                "type": "text",
                "text": {"body": body},
            }],
        }}]}],
    }


def _interactive_payload(kind="button_reply", action_id="v1:menu", title="Menu"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PHONE_ID},
            "messages": [{
                "id": "wamid.in-tap-1",
                "from": "254700000001",
                "type": "interactive",
                "interactive": {
                    "type": kind,
                    kind: {"id": action_id, "title": title},
                },
            }],
        }}]}],
    }


def _status_payload(status="delivered", message_id="wamid.out-1", callback=None,
                    errors=None, phone_number_id=PHONE_ID):
    entry = {
        "id": message_id,
        "status": status,
        "timestamp": "1792210000",
        "recipient_id": "254700000001",
    }
    if callback is not None:
        entry["biz_opaque_callback_data"] = callback
    if errors is not None:
        entry["errors"] = errors
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": phone_number_id},
            "statuses": [entry],
        }}]}],
    }


class TypedInboundParsingTests(unittest.TestCase):
    def test_text_event_is_typed_and_keeps_its_existing_keys(self):
        event = whatsapp_service.parse_event(_text_payload())
        self.assertEqual(event["kind"], "text")
        self.assertEqual(event["text"], "Hello")
        self.assertEqual(event["message_id"], "wamid.in-1")
        self.assertEqual(event["customer_key"], "254700000001")
        self.assertEqual(event["phone_number_id"], PHONE_ID)

    def test_button_reply_is_typed(self):
        event = whatsapp_service.parse_event(_interactive_payload())
        self.assertEqual(event["kind"], "interactive")
        self.assertEqual(event["action_type"], "button_reply")
        self.assertEqual(event["action_id"], "v1:menu")
        self.assertEqual(event["action_title"], "Menu")

    def test_list_reply_is_typed(self):
        event = whatsapp_service.parse_event(
            _interactive_payload(kind="list_reply", action_id="v1:cat:cakes", title="Cakes")
        )
        self.assertEqual(event["action_type"], "list_reply")
        self.assertEqual(event["action_id"], "v1:cat:cakes")

    def test_unsupported_message_types_are_still_ignored(self):
        payload = _text_payload()
        payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
            "id": "wamid.img-1",
            "from": "254700000001",
            "type": "image",
            "image": {"id": "media-1"},
        }
        self.assertIsNone(whatsapp_service.parse_event(payload))

    def test_absent_or_oversized_action_id_is_ignored(self):
        self.assertIsNone(whatsapp_service.parse_event(_interactive_payload(action_id="x" * 300)))

    def test_first_text_message_wins_in_a_batch(self):
        payload = _text_payload(message_id="wamid.first", body="one")
        payload["entry"][0]["changes"][0]["value"]["messages"].append({
            "id": "wamid.second",
            "from": "254700000001",
            "type": "text",
            "text": {"body": "two"},
        })
        event = whatsapp_service.parse_event(payload)
        self.assertEqual(event["message_id"], "wamid.first")
        self.assertEqual(event["text"], "one")

    def test_text_after_a_tap_in_the_same_batch_is_not_lost(self):
        payload = _interactive_payload()
        payload["entry"][0]["changes"][0]["value"]["messages"].append({
            "id": "wamid.in-after-tap",
            "from": "254700000001",
            "type": "text",
            "text": {"body": "actually, a question"},
        })
        event = whatsapp_service.parse_event(payload)
        self.assertEqual(event["kind"], "text")
        self.assertEqual(event["text"], "actually, a question")

    def test_text_before_a_tap_still_wins(self):
        payload = _text_payload(body="first")
        payload["entry"][0]["changes"][0]["value"]["messages"].append({
            "id": "wamid.in-tap-2",
            "from": "254700000001",
            "type": "interactive",
            "interactive": {"type": "button_reply",
                            "button_reply": {"id": "v1:menu", "title": "Menu"}},
        })
        self.assertEqual(whatsapp_service.parse_event(payload)["kind"], "text")

    def test_a_tap_in_one_entry_does_not_hide_text_in_another(self):
        payload = _interactive_payload()
        payload["entry"].append({"changes": [{"value": {
            "metadata": {"phone_number_id": PHONE_ID},
            "messages": [{
                "id": "wamid.in-later-entry",
                "from": "254700000001",
                "type": "text",
                "text": {"body": "later entry"},
            }],
        }}]})
        event = whatsapp_service.parse_event(payload)
        self.assertEqual(event["kind"], "text")
        self.assertEqual(event["text"], "later entry")


class StatusParsingTests(unittest.TestCase):
    def test_status_event_carries_code_and_correlation(self):
        events = whatsapp_service.parse_status_events(_status_payload(
            status="failed",
            callback="11111111-1111-1111-1111-111111111111",
            errors=[{"code": 131047, "message": "Re-engagement message"}],
        ))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["provider_message_id"], "wamid.out-1")
        self.assertEqual(events[0]["error_code"], "131047")
        self.assertEqual(events[0]["error_message"], "Re-engagement message")
        self.assertEqual(
            events[0]["biz_opaque_callback_data"], "11111111-1111-1111-1111-111111111111"
        )

    def test_every_status_in_a_batch_is_returned(self):
        payload = _status_payload()
        payload["entry"][0]["changes"][0]["value"]["statuses"].append({
            "id": "wamid.out-2", "status": "read", "timestamp": "1792210001",
        })
        self.assertEqual(
            [event["status"] for event in whatsapp_service.parse_status_events(payload)],
            ["delivered", "read"],
        )

    def test_unknown_status_is_ignored(self):
        self.assertEqual(
            whatsapp_service.parse_status_events(_status_payload(status="deleted")), []
        )

    def test_a_message_webhook_yields_no_status_events(self):
        self.assertEqual(whatsapp_service.parse_status_events(_text_payload()), [])

    def test_non_whatsapp_object_yields_nothing(self):
        self.assertEqual(
            whatsapp_service.parse_status_events({"object": "page", "entry": []}), []
        )


class WebhookOutboxTests(unittest.TestCase):
    """The route must record intent, send once, then record the outcome."""

    def setUp(self):
        self.app = create_app(_settings(), bot_provider=object())
        self.client = self.app.test_client()
        self.settings = self.app.extensions["platform"].settings
        self.stack = stack = ExitStack()
        self.addCleanup(stack.close)
        self.resolve = stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.resolve_channel",
            return_value={
                "tenant_id": "tenant-1",
                "channel_id": "channel-1",
                "credentials_ref": "tenants/tenant-1/whatsapp",
            },
        ))
        self.claim = stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.claim_event", return_value=True))
        stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.conversation_history", return_value=[]))
        self.send = stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.send_text", return_value="wamid.reply-1"))
        self.update_event = stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.update_event"))
        stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.save_conversation"))
        self.engine = stack.enter_context(patch("platform_admin.routes.BotResponseEngine"))
        self.engine.return_value.respond.return_value.text = "Welcome"
        stack.enter_context(patch(
            "platform_admin.routes.handover_service.handle_turn", return_value=None))
        stack.enter_context(patch(
            "platform_admin.routes.customer_service.get_or_create_customer",
            return_value={"id": "customer-1"}))
        stack.enter_context(patch(
            "platform_admin.routes.customer_service.get_or_create_conversation",
            return_value={"id": "conversation-1"}))
        self.save_message = stack.enter_context(patch(
            "platform_admin.routes.customer_service.save_message"))
        self.find_reply = stack.enter_context(patch(
            "platform_admin.routes.customer_service.find_outbound_reply"))
        self.mark_sent = stack.enter_context(patch(
            "platform_admin.routes.customer_service.mark_message_sent"))
        self.mark_failed = stack.enter_context(patch(
            "platform_admin.routes.customer_service.mark_message_failed"))
        stack.enter_context(patch(
            "platform_admin.routes.customer_service.update_conversation_activity"))
        self.update_status = stack.enter_context(patch(
            "platform_admin.routes.customer_service.update_delivery_status", return_value=1))
        self.save_message.side_effect = self._outbox_ok()

    @staticmethod
    def _outbox_ok(row_id="outbox-1"):
        def side_effect(settings, tenant_id, channel_id, conversation_id, *, direction, **kwargs):
            return {"id": "inbound-1" if direction == "inbound" else row_id}
        return side_effect

    def _post(self, payload):
        body = json.dumps(payload).encode()
        signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/webhooks/whatsapp",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )

    def test_outbound_intent_is_recorded_then_the_wamid_is_attached(self):
        response = self._post(_text_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "message_id": "wamid.reply-1"})

        outbound_call = self.save_message.call_args_list[-1]
        self.assertEqual(outbound_call.kwargs["direction"], "outbound")
        self.assertEqual(
            outbound_call.kwargs["reply_to_provider_message_id"], "wamid.in-1"
        )
        self.assertNotIn(
            "provider_message_id",
            outbound_call.kwargs,
            "the intent row must not claim a provider id before the send",
        )
        self.mark_sent.assert_called_once_with(
            self.settings, "tenant-1", "outbox-1", "wamid.reply-1"
        )
        self.update_event.assert_called_once_with(
            self.settings,
            "tenant-1",
            "wamid.in-1",
            "processed",
            response_message_id="wamid.reply-1",
        )

    def test_text_send_arguments_are_unchanged(self):
        self._post(_text_payload())
        self.send.assert_called_once_with(
            self.settings,
            phone_number_id=PHONE_ID,
            tenant_id="tenant-1",
            credentials_ref="tenants/tenant-1/whatsapp",
            recipient="254700000001",
            text="Welcome",
        )

    def test_a_reply_that_was_already_sent_is_not_sent_again(self):
        def duplicate(settings, tenant_id, channel_id, conversation_id, *, direction, **kwargs):
            if direction == "inbound":
                return {"id": "inbound-1"}
            raise customer_service.DuplicateReplyError()

        self.save_message.side_effect = duplicate
        self.find_reply.return_value = {
            "id": "outbox-1",
            "provider_message_id": "wamid.previous",
        }
        response = self._post(_text_payload())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["duplicate_reply"])
        self.assertEqual(response.json["message_id"], "wamid.previous")
        self.send.assert_not_called()
        self.update_event.assert_called_once_with(
            self.settings,
            "tenant-1",
            "wamid.in-1",
            "processed",
            response_message_id="wamid.previous",
        )

    def test_an_ambiguous_reply_is_recorded_and_never_automatically_resent(self):
        def duplicate(settings, tenant_id, channel_id, conversation_id, *, direction, **kwargs):
            if direction == "inbound":
                return {"id": "inbound-1"}
            raise customer_service.DuplicateReplyError()

        self.save_message.side_effect = duplicate
        self.find_reply.return_value = {"id": "outbox-1", "provider_message_id": None}
        response = self._post(_text_payload())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ambiguous"])
        self.send.assert_not_called()
        self.update_event.assert_called_once_with(
            self.settings, "tenant-1", "wamid.in-1", "failed", "ambiguous_send"
        )

    def test_send_failure_is_recorded_against_the_outbound_row(self):
        self.send.side_effect = whatsapp_service.WhatsAppSendError(
            "WhatsApp message delivery failed.",
            502,
            provider_code="131047",
            provider_message="Re-engagement message",
        )
        response = self._post(_text_payload())
        self.assertEqual(response.status_code, 502)
        self.mark_failed.assert_called_once_with(
            self.settings,
            "tenant-1",
            "outbox-1",
            error_code="131047",
            error_message="Re-engagement message",
        )
        self.mark_sent.assert_not_called()
        self.update_event.assert_called_once_with(
            self.settings, "tenant-1", "wamid.in-1", "failed", "WhatsAppSendError"
        )

    def test_outbox_persistence_failure_does_not_silence_the_bot(self):
        def broken(settings, tenant_id, channel_id, conversation_id, *, direction, **kwargs):
            if direction == "inbound":
                return {"id": "inbound-1"}
            raise RuntimeError("db down")

        self.save_message.side_effect = broken
        response = self._post(_text_payload())
        self.assertEqual(response.status_code, 200)
        self.send.assert_called_once()
        self.assertEqual(response.json["message_id"], "wamid.reply-1")

    def test_an_unmigrated_database_falls_back_to_post_send_persistence(self):
        """Deploying this code before 0035 must not lose outbound message history."""
        calls = []

        def legacy(settings, tenant_id, channel_id, conversation_id, *, direction, **kwargs):
            calls.append(kwargs)
            if direction == "inbound":
                return {"id": "inbound-1"}
            if "reply_to_provider_message_id" in kwargs:
                raise customer_service.SchemaOutOfDateError()
            return {"id": "legacy-out"}

        self.save_message.side_effect = legacy
        response = self._post(_text_payload())
        self.assertEqual(response.status_code, 200)
        self.send.assert_called_once()
        self.assertEqual(calls[-1]["provider_message_id"], "wamid.reply-1")
        self.assertEqual(calls[-1]["delivery_status"], "sent")
        self.mark_sent.assert_not_called()

    def test_interactive_inbound_is_acknowledged_without_processing(self):
        response = self._post(_interactive_payload())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ignored"])
        self.assertEqual(response.json["kind"], "interactive")
        self.claim.assert_not_called()
        self.send.assert_not_called()


class DeliveryStatusWebhookTests(unittest.TestCase):
    """Receipts update an existing row; they never create one and never fail the webhook."""

    def setUp(self):
        self.app = create_app(_settings(), bot_provider=object())
        self.client = self.app.test_client()
        self.settings = self.app.extensions["platform"].settings
        self.stack = stack = ExitStack()
        self.addCleanup(stack.close)
        self.resolve = stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.resolve_channel",
            return_value={"tenant_id": "tenant-1", "channel_id": "channel-1"},
        ))
        self.claim = stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.claim_event", return_value=True))
        self.send = stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.send_text", return_value="wamid.x"))
        self.update_status = stack.enter_context(patch(
            "platform_admin.routes.customer_service.update_delivery_status", return_value=1))

    def _post(self, payload):
        body = json.dumps(payload).encode()
        signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/webhooks/whatsapp",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )

    def test_receipt_is_applied_without_claiming_an_event(self):
        response = self._post(_status_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "statuses": 1})
        self.claim.assert_not_called()
        self.send.assert_not_called()
        self.update_status.assert_called_once_with(
            self.settings,
            "tenant-1",
            status="delivered",
            provider_message_id="wamid.out-1",
            row_id=None,
            error_code=None,
            error_message=None,
        )

    def test_receipt_includes_provider_error_details(self):
        self._post(_status_payload(
            status="failed",
            errors=[{"code": 131026, "message": "Message undeliverable"}],
        ))
        self.assertEqual(
            self.update_status.call_args.kwargs["error_code"], "131026"
        )
        self.assertEqual(
            self.update_status.call_args.kwargs["error_message"], "Message undeliverable"
        )

    def test_receipt_uses_the_outbox_row_id_when_supplied(self):
        self._post(_status_payload(callback="11111111-1111-1111-1111-111111111111"))
        self.assertEqual(
            self.update_status.call_args.kwargs["row_id"],
            "11111111-1111-1111-1111-111111111111",
        )

    def test_a_non_uuid_callback_is_ignored_rather_than_rejected(self):
        response = self._post(_status_payload(callback="not-a-uuid"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.update_status.call_args.kwargs["row_id"])

    def test_unknown_channel_is_acked_and_ignored(self):
        self.resolve.return_value = None
        response = self._post(_status_payload())
        self.assertEqual(response.status_code, 200)
        self.update_status.assert_not_called()

    def test_a_database_failure_does_not_fail_the_webhook(self):
        self.update_status.side_effect = RuntimeError("db down")
        response = self._post(_status_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["statuses"], 1)


class SenderPayloadTests(unittest.TestCase):
    """The plain-text request body must not change. This is the production-safety test."""

    @patch("platform_admin.services.whatsapp.access_token", return_value="test-token")
    @patch("platform_admin.services.whatsapp.urllib.request.urlopen")
    def test_plain_text_payload_is_byte_identical(self, urlopen, token):
        response = MagicMock()
        response.read.return_value = b'{"messages":[{"id":"wamid.out"}]}'
        urlopen.return_value.__enter__.return_value = response
        settings = Settings(secret_key="s" * 40, whatsapp_send_enabled=True)

        whatsapp_service.send_text(
            settings,
            phone_number_id=PHONE_ID,
            tenant_id="tenant-1",
            credentials_ref="tenants/tenant-1/whatsapp",
            recipient="254700000001",
            text="Hello",
        )

        request = urlopen.call_args[0][0]
        self.assertEqual(
            request.data,
            b'{"messaging_product": "whatsapp", "to": "254700000001", '
            b'"type": "text", "text": {"body": "Hello"}}',
        )

    @patch("platform_admin.services.whatsapp.access_token", return_value="test-token")
    @patch("platform_admin.services.whatsapp.urllib.request.urlopen")
    def test_correlation_data_is_added_only_when_requested(self, urlopen, token):
        response = MagicMock()
        response.read.return_value = b'{"messages":[{"id":"wamid.out"}]}'
        urlopen.return_value.__enter__.return_value = response
        settings = Settings(secret_key="s" * 40, whatsapp_send_enabled=True)

        whatsapp_service.send_text(
            settings,
            phone_number_id=PHONE_ID,
            tenant_id="tenant-1",
            credentials_ref="tenants/tenant-1/whatsapp",
            recipient="254700000001",
            text="Hello",
            reply_to_row_id="11111111-1111-1111-1111-111111111111",
        )

        body = json.loads(urlopen.call_args[0][0].data.decode())
        self.assertEqual(
            body["biz_opaque_callback_data"], "11111111-1111-1111-1111-111111111111"
        )

    @patch("platform_admin.services.whatsapp.access_token", return_value="test-token")
    @patch("platform_admin.services.whatsapp.urllib.request.urlopen")
    def test_provider_error_is_structured_but_the_message_is_unchanged(self, urlopen, token):
        urlopen.side_effect = urllib.error.HTTPError(
            "https://graph.facebook.com/v21.0/phone-test-1/messages",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":{"code":131047,"message":"Re-engagement message"}}'),
        )
        settings = Settings(secret_key="s" * 40, whatsapp_send_enabled=True)

        with self.assertRaises(whatsapp_service.WhatsAppSendError) as caught:
            whatsapp_service.send_text(
                settings,
                phone_number_id=PHONE_ID,
                tenant_id="tenant-1",
                credentials_ref="tenants/tenant-1/whatsapp",
                recipient="254700000001",
                text="Hello",
            )

        self.assertEqual(caught.exception.message, "WhatsApp message delivery failed.")
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(caught.exception.provider_code, "131047")
        self.assertEqual(caught.exception.provider_message, "Re-engagement message")


class MigrationContractTests(unittest.TestCase):
    """The index name is part of the code/migration contract; a rename must break a test."""

    @staticmethod
    def _sql() -> str:
        root = Path(__file__).resolve().parent.parent
        return (
            root / "db" / "migrations" / "0035_menu_state_and_reply_correlation.sql"
        ).read_text(encoding="utf-8")

    def test_the_migration_defines_the_columns_the_code_uses(self):
        sql = self._sql()
        for column in (
            "menu_state",
            "menu_state_expires_at",
            "menu_state_version",
            "reply_to_provider_message_id",
        ):
            self.assertIn(column, sql)
        self.assertIn("messages_outbound_reply_unique", sql)

    def test_the_index_name_matches_the_constant_the_code_matches_on(self):
        root = Path(__file__).resolve().parent.parent
        source = (root / "platform_admin" / "services" / "customers.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("messages_outbound_reply_unique", source)

    def test_the_index_is_partial_and_does_not_block_the_intent_row(self):
        sql = self._sql()
        self.assertIn("where direction = 'outbound'", sql)
        self.assertIn("reply_to_provider_message_id is not null", sql)
        # The intent row is inserted with a NULL provider_message_id, so the new index
        # must not key on it.
        self.assertNotIn("(tenant_id, channel_id, provider_message_id)", sql)

    def test_the_migration_is_additive_only(self):
        sql = self._sql().lower()
        for destructive in ("drop table", "drop column", "delete from", "truncate"):
            self.assertNotIn(destructive, sql)
        self.assertNotIn("update public.", sql)


class SchemaDetectionTests(unittest.TestCase):
    """F5: only the outbox column may be read as "0035 is missing"."""

    @staticmethod
    def _error(column, message=None):
        error = RuntimeError(message or f'column "{column}" does not exist')
        error.diag = SimpleNamespace(column_name=column)
        return error

    def test_the_outbox_column_is_recognised_by_its_diagnostic(self):
        self.assertTrue(
            customer_service.missing_outbox_column(
                self._error("reply_to_provider_message_id")
            )
        )

    def test_the_outbox_column_is_recognised_from_the_message(self):
        error = RuntimeError(
            'column "reply_to_provider_message_id" of relation "messages" does not exist'
        )
        self.assertTrue(customer_service.missing_outbox_column(error))

    def test_an_unrelated_missing_column_is_not_treated_as_0035(self):
        self.assertFalse(
            customer_service.missing_outbox_column(self._error("payload"))
        )

    def test_a_missing_table_is_not_treated_as_0035(self):
        error = RuntimeError('relation "public.messages" does not exist')
        self.assertFalse(customer_service.missing_outbox_column(error))


if __name__ == "__main__":
    unittest.main()
