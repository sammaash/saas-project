"""Proactive human handover: an unlisted product is recorded and staff are actually told.

These tests drive the real webhook route against a real database with genuinely signed
payloads; only the two external calls (the AI provider and the Meta send) are stubbed,
because those are outbound network calls.

Tenant 002 ("Test Bakery") is used deliberately: it is the generic test tenant, so no Cindy
Bakes configuration or data is touched, and every row this suite writes is deleted again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from platform_admin.app import create_app
from platform_admin.auth import AuthError, Authenticator, Identity
from platform_admin.config import Settings
from platform_admin.services import handover as handover_service
from platform_admin.services.validation import ServiceError
from saas import handover as handover_store
from saas.bot import BotResponseRequest

try:
    from .saas_test_support import (
        PLATFORM_ADMIN_USER_ID,
        TENANT_ONE_ID,
        TENANT_TWO_ID,
        SaasDatabaseTestCase,
        _connect,
        app_backend_test_url,
        platform_admin_test_url,
    )
except ImportError:  # discovery inside the tests directory
    from saas_test_support import (  # type: ignore[no-redef]
        PLATFORM_ADMIN_USER_ID,
        TENANT_ONE_ID,
        TENANT_TWO_ID,
        SaasDatabaseTestCase,
        _connect,
        app_backend_test_url,
        platform_admin_test_url,
    )

SECRET = "app-secret"
CHANNEL_PHONE_ID = "dev-phone-id-testbakery-002"   # the seeded channel for tenant 002
CUSTOMER = "254711000111"
STAFF = "+254700000999"
STAFF_SPACED = "+254 700 000 999"
PASSWORD = "correct horse"

TOOL_CALL = (
    'TOOL_CALL: {"name": "request_staff_handover", "arguments": {"product": '
    '"pina colada cake", "reason": "unlisted_product", "size": "1kg", "quantity": "1"}}'
)

#: The reply that caused the reported problem, verbatim in shape: the bot told the customer
#: to contact staff themselves instead of recording anything.
DEFERRAL_REPLY = (
    "Pina colada cake isn't listed in our catalogue. Please contact our staff on WhatsApp at "
    f"{STAFF_SPACED} to check if it can be arranged."
)


class _ScriptedProvider:
    """Stands in for the AI provider and replays a fixed script."""

    def __init__(self, *responses: str):
        self._responses = list(responses)
        self.requests: list[BotResponseRequest] = []

    def generate(self, request: BotResponseRequest) -> str:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return "Thanks for your message."


def _settings() -> Settings:
    return Settings(
        secret_key="s" * 40,
        cookie_secure=False,
        app_database_url=app_backend_test_url(),
        platform_database_url=platform_admin_test_url(),
        whatsapp_verify_token="verify-me",
        whatsapp_app_secret=SECRET,
        whatsapp_send_enabled=True,      # so the send path is genuinely exercised
    )


def _payload(message_id: str, text: str, sender: str = CUSTOMER) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": CHANNEL_PHONE_ID},
            "contacts": [{"profile": {"name": "Test Customer"}, "wa_id": sender}],
            "messages": [{
                "id": message_id,
                "from": sender,
                "timestamp": "1792210000",
                "type": "text",
                "text": {"body": text},
            }],
        }}]}],
    }


class _HandoverTestCase(SaasDatabaseTestCase):
    """Isolated handover fixtures on tenant 002, restored after every test."""

    def setUp(self):
        self._original_settings = None
        self._cleanup()
        self.addCleanup(self._cleanup)

    # -- fixtures ------------------------------------------------------------
    def _cleanup(self):
        conn = _connect()
        with conn, conn.cursor() as cursor:
            for table in (
                "handover_requests", "messages", "conversations", "customers",
                "whatsapp_webhook_events", "whatsapp_conversations", "audit_log",
            ):
                cursor.execute(
                    f"delete from public.{table} where tenant_id = %s", (TENANT_TWO_ID,)
                )
            original = getattr(self, "_original_settings", None)
            if original is not None:
                cursor.execute(
                    "update public.tenant_settings set business_rules = %s::jsonb, "
                    "configuration = %s::jsonb where tenant_id = %s",
                    (*original, TENANT_TWO_ID),
                )
        conn.close()

    def install_settings(self, staff: str | None = STAFF) -> None:
        """Give the test tenant a staff contact (or remove it), remembering the original."""
        conn = _connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "select business_rules, configuration from public.tenant_settings "
                "where tenant_id = %s",
                (TENANT_TWO_ID,),
            )
            rules, configuration = cursor.fetchone()
            self._original_settings = (json.dumps(rules), json.dumps(configuration))
            rules = dict(rules or {})
            configuration = dict(configuration or {})
            config = dict(configuration.get("config") or {})
            config["provider"] = "meta_cloud_api"
            if staff is None:
                config.pop("human_handover_number", None)
                rules.pop("escalation", None)
            else:
                config["human_handover_number"] = staff
                rules["escalation"] = {
                    "after_tries": 2, "transfer_to": "Owner", "whatsapp": staff,
                }
            configuration["config"] = config
            configuration.setdefault("identity", {"whatsapp_display_name": "Test Bakery"})
            cursor.execute(
                "update public.tenant_settings set business_rules = %s::jsonb, "
                "configuration = %s::jsonb where tenant_id = %s",
                (json.dumps(rules), json.dumps(configuration), TENANT_TWO_ID),
            )
        conn.close()

    # -- harness -------------------------------------------------------------
    def client_for(self, *responses: str):
        self.provider = _ScriptedProvider(*responses)
        self.app = create_app(_settings(), bot_provider=self.provider)
        self.client = self.app.test_client()
        return self.client

    def post(self, client, payload: dict, secret: str = SECRET):
        body = json.dumps(payload).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return client.post(
            "/webhooks/whatsapp",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )

    def handovers(self) -> list[dict]:
        conn = _connect()
        with conn.cursor() as cursor:
            cursor.execute(
                "select id, requested_product, reason, status, wa_id, customer_name, "
                "notification_target, notification_error, details, context "
                "from public.handover_requests where tenant_id = %s order by created_at",
                (TENANT_TWO_ID,),
            )
            rows = cursor.fetchall()
        conn.close()
        keys = (
            "id", "requested_product", "reason", "status", "wa_id", "customer_name",
            "notification_target", "notification_error", "details", "context",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    @staticmethod
    def customer_text(send) -> str:
        """The message the customer received (the last send of the turn)."""
        return send.call_args_list[-1].kwargs["text"]

    @staticmethod
    def staff_calls(send) -> list:
        return [call for call in send.call_args_list if call.kwargs["recipient"] != CUSTOMER]



class UnlistedProductHandoverTests(_HandoverTestCase):
    """Requirements 1-3: record, notify, then answer -- in that order."""

    def setUp(self):
        super().setUp()
        self.install_settings()

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_an_unlisted_product_creates_a_handover_and_confirms_to_the_customer(self, send):
        send.side_effect = ["wamid.staff-1", "wamid.customer-1"]
        client = self.client_for(TOOL_CALL, "Let me check that for you.")

        response = self.post(client, _payload("wamid.handover-1", "I want a pina colada cake"))

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        rows = self.handovers()
        self.assertEqual(len(rows), 1, rows)
        record = rows[0]
        self.assertEqual(record["requested_product"], "pina colada cake")
        self.assertEqual(record["reason"], "unlisted_product")
        self.assertEqual(record["status"], "notified")
        self.assertEqual(record["wa_id"], CUSTOMER)
        self.assertEqual(record["notification_target"], STAFF)
        self.assertEqual(record["details"], {"size": "1kg", "quantity": "1"})

        self.assertEqual(send.call_count, 2, "one staff notification and one customer reply")
        customer = self.customer_text(send)
        self.assertIn("pina colada cake", customer)
        self.assertIn("I've shared your request", customer)
        self.assertIn("actual price", customer)
        self.assertNotIn(STAFF, customer, "the customer is never handed the staff number")

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_the_confirmation_is_only_sent_after_the_request_is_stored(self, send):
        seen: dict[str, int] = {}

        def record_then_send(settings, *, recipient, text, **kwargs):
            if recipient != CUSTOMER:
                seen["rows_when_staff_notified"] = len(self.handovers())
            else:
                seen["rows_when_customer_answered"] = len(self.handovers())
            return "wamid.x"

        send.side_effect = record_then_send
        client = self.client_for(TOOL_CALL, "Let me check that for you.")

        self.assertEqual(
            self.post(client, _payload("wamid.order-1", "pina colada cake please")).status_code,
            200,
        )
        self.assertEqual(seen["rows_when_staff_notified"], 1)
        self.assertEqual(seen["rows_when_customer_answered"], 1)

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_staff_receive_the_details_they_need_to_follow_up(self, send):
        send.side_effect = ["wamid.staff-2", "wamid.customer-2"]
        client = self.client_for(TOOL_CALL, "Let me check that for you.")
        self.post(client, _payload("wamid.details-1", "I want a 1kg pina colada cake"))

        staff = self.staff_calls(send)
        self.assertEqual(len(staff), 1)
        call = staff[0]
        self.assertEqual(call.kwargs["recipient"], STAFF)
        text = call.kwargs["text"]
        self.assertIn("Test Bakery", text)
        self.assertIn("pina colada cake", text)
        self.assertIn("size: 1kg", text)
        self.assertIn(CUSTOMER, text)
        self.assertIn("confirm whether you can make this", text)
        self.assertIn("actual price", text)
        self.assertIn("Recent conversation", text)
        self.assertIn("I want a 1kg pina colada cake", text)

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_a_notification_failure_never_becomes_a_success_claim(self, send):
        send.side_effect = [
            ServiceError("WhatsApp message delivery failed.", 502),
            "wamid.customer-3",
        ]
        client = self.client_for(TOOL_CALL, "Let me check that for you.")

        response = self.post(client, _payload("wamid.notifyfail-1", "pina colada cake please"))

        self.assertEqual(response.status_code, 200, "a staff problem must not fail the customer")
        record = self.handovers()[0]
        self.assertEqual(record["status"], "failed")
        self.assertIn("delivery failed", record["notification_error"].lower())

        customer = self.customer_text(send)
        self.assertNotIn("shared your request", customer)
        self.assertIn("logged your request", customer)
        self.assertIn("actual price", customer)

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_a_redelivered_event_does_not_create_a_second_request_or_notification(self, send):
        send.side_effect = ["wamid.staff-4", "wamid.customer-4"]
        client = self.client_for(TOOL_CALL, "Let me check that for you.")
        payload = _payload("wamid.dup-1", "pina colada cake please")

        self.assertEqual(self.post(client, payload).status_code, 200)
        after_first = self.handovers()
        self.assertEqual(len(after_first), 1)

        again = self.post(client, payload)
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json.get("duplicate"))
        self.assertEqual(self.handovers(), after_first)
        self.assertEqual(send.call_count, 2, "no second notification and no second reply")



class SafetyNetTests(_HandoverTestCase):
    """The reported bug: the bot sent the customer to staff instead of recording anything."""

    def setUp(self):
        super().setUp()
        self.install_settings()

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_a_reply_that_hands_over_the_staff_number_is_replaced_and_recorded(self, send):
        send.side_effect = ["wamid.staff-5", "wamid.customer-5"]
        client = self.client_for(DEFERRAL_REPLY)

        response = self.post(client, _payload("wamid.regression-1", "Do you have pina colada cake?"))

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        rows = self.handovers()
        self.assertEqual(len(rows), 1, "the deferral itself becomes a recorded request")
        self.assertEqual(rows[0]["reason"], "customer_request")
        self.assertEqual(rows[0]["status"], "notified")

        customer = self.customer_text(send)
        self.assertNotIn(STAFF_SPACED, customer)
        self.assertNotIn("contact our staff", customer.lower())
        self.assertIn("I've shared your request", customer)
        self.assertIn("actual price", customer)

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_asking_again_about_the_same_thing_does_not_spam_staff(self, send):
        send.side_effect = ["wamid.staff-6", "wamid.customer-6", "wamid.customer-7"]
        client = self.client_for(DEFERRAL_REPLY, DEFERRAL_REPLY)

        self.post(client, _payload("wamid.repeat-1", "Do you have pina colada cake?"))
        self.post(client, _payload("wamid.repeat-2", "Hello? Do you have pina colada cake?"))

        self.assertEqual(len(self.handovers()), 1, "one unresolved request per customer")
        self.assertEqual(len(self.staff_calls(send)), 1, "staff are told once")

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_a_normal_reply_is_not_treated_as_a_handover(self, send):
        send.side_effect = ["wamid.customer-8"]
        client = self.client_for("Our vanilla cake is KES 2500 and is available today.")

        self.post(client, _payload("wamid.normal-1", "How much is the vanilla cake?"))

        self.assertEqual(self.handovers(), [])
        self.assertEqual(send.call_count, 1)
        self.assertIn("vanilla cake", self.customer_text(send))


class CatalogueProductTests(_HandoverTestCase):
    """Requirement 4/6: a listed product keeps the normal bot workflow, price included."""

    def setUp(self):
        super().setUp()
        self.install_settings()

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_a_listed_product_is_answered_normally_with_no_handover(self, send):
        send.side_effect = ["wamid.customer-9"]
        reply = "Our 1kg vanilla cake is KES 2500. Shall I start an order?"
        client = self.client_for(reply)

        self.post(client, _payload("wamid.listed-1", "How much is the 1kg vanilla cake?"))

        self.assertEqual(self.handovers(), [])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.customer_text(send), reply, "the model's answer is not rewritten")
        self.assertEqual(send.call_args.kwargs["recipient"], CUSTOMER)

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_an_invented_price_never_reaches_the_customer(self, send):
        send.side_effect = ["wamid.staff-7", "wamid.customer-10"]
        invented = (
            "A pina colada cake would be KES 4500. Please contact our staff on WhatsApp at "
            f"{STAFF_SPACED} to confirm."
        )
        client = self.client_for(invented)

        self.post(client, _payload("wamid.price-1", "How much is a pina colada cake?"))

        customer = self.customer_text(send)
        self.assertNotIn("4500", customer)
        self.assertNotIn("KES", customer)
        self.assertIn("actual price", customer)
        self.assertEqual(len(self.handovers()), 1)



class MissingStaffContactTests(_HandoverTestCase):
    """Requirement 9: no staff contact configured is handled safely, not silently."""

    def setUp(self):
        super().setUp()
        self.install_settings(staff=None)

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_the_request_is_kept_and_the_failure_is_visible(self, send):
        send.side_effect = ["wamid.customer-11"]
        client = self.client_for(TOOL_CALL, "Let me check that for you.")

        response = self.post(client, _payload("wamid.nostaff-1", "pina colada cake please"))

        self.assertEqual(response.status_code, 200)
        rows = self.handovers()
        self.assertEqual(len(rows), 1, "the request is still recorded")
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("No staff contact is configured", rows[0]["notification_error"])

        self.assertEqual(len(self.staff_calls(send)), 0, "nothing is sent to a missing number")
        customer = self.customer_text(send)
        self.assertIn("logged your request", customer)
        self.assertNotIn("shared your request", customer)

    @patch("platform_admin.routes.whatsapp_service.send_text")
    def test_the_customer_still_gets_an_answer(self, send):
        send.side_effect = ["wamid.customer-12"]
        client = self.client_for(DEFERRAL_REPLY)
        response = self.post(client, _payload("wamid.nostaff-2", "Do you have pina colada cake?"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.customer_text(send))


class RecordFailureTests(_HandoverTestCase):
    """Requirement 2: if the request cannot be recorded, never claim it was shared."""

    def setUp(self):
        super().setUp()
        self.install_settings()

    @patch("platform_admin.routes.whatsapp_service.send_text")
    @patch("saas.handover.create_request", side_effect=RuntimeError("database unavailable"))
    def test_a_record_failure_produces_an_apology_not_a_false_claim(self, _create, send):
        send.side_effect = ["wamid.customer-13"]
        client = self.client_for(TOOL_CALL, "I've shared your request with the team!")

        response = self.post(client, _payload("wamid.recordfail-1", "pina colada cake please"))

        self.assertEqual(response.status_code, 200, "the customer still gets an answer")
        self.assertEqual(self.handovers(), [])
        self.assertEqual(len(self.staff_calls(send)), 0)
        customer = self.customer_text(send)
        self.assertIn("couldn't record your request", customer)
        self.assertNotIn("shared your request", customer)



class RecordStoreTests(_HandoverTestCase):
    """Requirements 3 and 5: durable, idempotent, and strictly tenant-scoped."""

    URL = staticmethod(app_backend_test_url)

    def record(self, **overrides):
        arguments = {
            "wa_id": CUSTOMER,
            "requested_product": "pina colada cake",
            "dedupe_key": f"wamid.{uuid.uuid4()}",
            "database_url": self.URL(),
        }
        arguments.update(overrides)
        return handover_store.create_request(TENANT_TWO_ID, **arguments)

    def test_the_same_event_is_recorded_once(self):
        first, created_first = self.record(dedupe_key="wamid.same")
        second, created_second = self.record(dedupe_key="wamid.same")

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.handovers()), 1)

    def test_the_same_product_in_a_later_message_reuses_the_open_request(self):
        first, _ = self.record(requested_product="Pina Colada Cake", dedupe_key="wamid.a")
        second, created = self.record(requested_product="pina colada cake", dedupe_key="wamid.b")

        self.assertFalse(created)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.handovers()), 1)

    def test_resolving_a_request_allows_a_new_one(self):
        first, _ = self.record(dedupe_key="wamid.resolve-a")
        self.assertTrue(
            handover_store.set_status(
                TENANT_TWO_ID, str(first["id"]), "resolved", self.URL()
            )
        )
        second, created = self.record(dedupe_key="wamid.resolve-b")

        self.assertTrue(created)
        self.assertNotEqual(first["id"], second["id"])

    def test_another_tenant_cannot_read_or_change_a_request(self):
        record, _ = self.record()

        self.assertIsNone(handover_store.get_request(TENANT_ONE_ID, str(record["id"]), self.URL()))
        self.assertEqual(
            handover_store.list_requests(TENANT_ONE_ID, database_url=self.URL()), []
        )
        self.assertFalse(
            handover_store.set_status(
                TENANT_ONE_ID, str(record["id"]), "resolved", self.URL()
            )
        )
        self.assertEqual(
            handover_store.get_request(TENANT_TWO_ID, str(record["id"]), self.URL())["status"],
            "pending",
        )


class _FakeAuthenticator(Authenticator):
    def __init__(self, users: dict[str, tuple[str, str]]):
        self._users = users

    def login(self, email, password):
        entry = self._users.get(email)
        if not entry or entry[0] != password:
            raise AuthError("Invalid email or password.")
        return Identity(entry[1], email)



class HandoverDashboardTests(_HandoverTestCase):
    """Requirement 5: an authorised operator sees and works one tenant's requests only."""

    def setUp(self):
        super().setUp()
        self.install_settings()
        self.record, _ = handover_store.create_request(
            TENANT_TWO_ID,
            wa_id=CUSTOMER,
            requested_product="pina colada cake",
            dedupe_key="wamid.dashboard-1",
            details={"size": "1kg"},
            database_url=app_backend_test_url(),
        )
        conn = _connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "insert into public.platform_admins (user_id) values (%s) on conflict do nothing",
                (PLATFORM_ADMIN_USER_ID,),
            )
        conn.close()
        self.app = create_app(
            _settings(),
            authenticator=_FakeAuthenticator({
                "admin@example.com": (PASSWORD, PLATFORM_ADMIN_USER_ID),
            }),
        )
        self.client = self.app.test_client()
        self.login()

    def login(self, email="admin@example.com", password=PASSWORD):
        self.client.get("/login")
        with self.client.session_transaction() as session:
            token = session["csrf_token"]
        return self.client.post(
            "/login", data={"csrf_token": token, "email": email, "password": password}
        )

    def post(self, path, data=None):
        with self.client.session_transaction() as session:
            token = session["csrf_token"]
        payload = dict(data or {})
        payload["csrf_token"] = token
        return self.client.post(path, data=payload)

    def test_the_dashboard_lists_the_tenants_pending_requests(self):
        response = self.client.get(f"/tenants/{TENANT_TWO_ID}/handovers")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("pina colada cake", page)
        self.assertIn(CUSTOMER, page)
        self.assertIn("Handovers", page)

    def test_another_tenants_requests_are_never_shown(self):
        response = self.client.get(f"/tenants/{TENANT_ONE_ID}/handovers")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("pina colada cake", response.get_data(as_text=True))

    def test_staff_can_mark_a_request_resolved(self):
        response = self.post(
            f"/tenants/{TENANT_TWO_ID}/handovers/{self.record['id']}/status",
            {"status": "resolved"},
        )
        self.assertEqual(response.status_code, 302)
        stored = handover_store.get_request(
            TENANT_TWO_ID, str(self.record["id"]), app_backend_test_url()
        )
        self.assertEqual(stored["status"], "resolved")

    def test_an_invalid_or_cross_tenant_status_change_is_refused(self):
        self.assertEqual(
            self.post(
                f"/tenants/{TENANT_TWO_ID}/handovers/{self.record['id']}/status",
                {"status": "deleted"},
            ).status_code,
            302,
        )
        self.assertEqual(
            self.post(
                f"/tenants/{TENANT_ONE_ID}/handovers/{self.record['id']}/status",
                {"status": "resolved"},
            ).status_code,
            302,
        )
        stored = handover_store.get_request(
            TENANT_TWO_ID, str(self.record["id"]), app_backend_test_url()
        )
        self.assertEqual(stored["status"], "pending", "nothing changed")

    @patch("platform_admin.services.handover.whatsapp_service.send_text")
    def test_retry_delivers_and_marks_notified(self, send):
        send.return_value = "wamid.staff-8"
        response = self.post(f"/tenants/{TENANT_TWO_ID}/handovers/{self.record['id']}/notify")
        self.assertEqual(response.status_code, 302)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["recipient"], STAFF)
        stored = handover_store.get_request(
            TENANT_TWO_ID, str(self.record["id"]), app_backend_test_url()
        )
        self.assertEqual(stored["status"], "notified")

    @patch("platform_admin.services.handover.whatsapp_service.send_text")
    def test_a_failed_retry_stays_failed(self, send):
        send.side_effect = ServiceError("WhatsApp message delivery failed.", 502)
        response = self.post(f"/tenants/{TENANT_TWO_ID}/handovers/{self.record['id']}/notify")
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("could not be delivered", page)
        stored = handover_store.get_request(
            TENANT_TWO_ID, str(self.record["id"]), app_backend_test_url()
        )
        self.assertEqual(stored["status"], "failed")

    def test_a_signed_out_visitor_cannot_reach_the_handover_view(self):
        anonymous = self.app.test_client()
        self.assertEqual(
            anonymous.get(f"/tenants/{TENANT_TWO_ID}/handovers").status_code, 302
        )



class HandoverTextTests(unittest.TestCase):
    """Pure functions: target resolution, detection and wording. No database needed."""

    DOCUMENT = {
        "configuration": {"config": {"human_handover_number": STAFF}},
        "business_rules": {"escalation": {"whatsapp": "+254711111111"}},
        "timezone": "Africa/Nairobi",
    }

    def test_the_explicit_handover_number_wins_over_the_escalation_number(self):
        self.assertEqual(handover_service.staff_target(self.DOCUMENT), STAFF)
        self.assertEqual(
            handover_service.staff_numbers(self.DOCUMENT), [STAFF, "+254711111111"]
        )

    def test_the_escalation_number_is_the_fallback(self):
        document = {
            "configuration": {"config": {}},
            "business_rules": {"escalation": {"whatsapp": "+254722222222"}},
        }
        self.assertEqual(handover_service.staff_target(document), "+254722222222")

    def test_no_staff_contact_is_reported_as_missing(self):
        for document in ({}, {"configuration": {"config": {}}, "business_rules": {}}):
            self.assertIsNone(handover_service.staff_target(document))

    def test_the_reported_deferral_reply_is_detected(self):
        self.assertTrue(handover_service.reply_hands_off_to_staff(DEFERRAL_REPLY, self.DOCUMENT))

    def test_every_format_of_the_staff_number_is_detected(self):
        for text in (
            f"Call us on {STAFF}",
            "Call 0700 000 999",
            "WhatsApp 254700000999",
            "Reach the team at (+254) 700-000-999",
        ):
            with self.subTest(text=text):
                self.assertTrue(handover_service.reply_hands_off_to_staff(text, self.DOCUMENT))

    def test_an_invented_price_cannot_hide_the_staff_number(self):
        text = f"A pina colada cake is KES 4500. Contact our staff at {STAFF_SPACED}."
        self.assertTrue(handover_service.reply_hands_off_to_staff(text, self.DOCUMENT))

    def test_a_normal_reply_is_not_a_handover(self):
        for text in (
            "Our 1kg vanilla cake is KES 2500.",
            "Thanks! Your order is confirmed.",
            "Call us if you need anything else.",          # no number at all
            "Delivery is KES 320 per km.",
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(handover_service.reply_hands_off_to_staff(text, self.DOCUMENT))

    def test_the_confirmation_never_claims_sharing_when_the_team_was_not_told(self):
        shared = handover_service.confirmation_text(
            "Test Bakery", "pina colada cake", notified=True
        )
        logged = handover_service.confirmation_text(
            "Test Bakery", "pina colada cake", notified=False
        )

        self.assertIn("I've shared your request", shared)
        self.assertIn("actual price", shared)
        self.assertNotIn("shared your request", logged)
        self.assertIn("logged your request", logged)
        self.assertIn("actual price", logged)
        for text in (shared, logged):
            self.assertNotIn(STAFF, text)

    def test_the_notification_has_everything_staff_need(self):
        record = {
            "requested_product": "pina colada cake",
            "details": {"size": "1kg", "needed_by": "Saturday"},
            "wa_id": CUSTOMER,
            "customer_name": "Amina",
            "reason": "unlisted_product",
            "created_at": datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc),
            "context": [{"role": "user", "text": "I want a pina colada cake"}],
        }
        text = handover_service.notification_text(
            "Test Bakery", record, timezone_name="Africa/Nairobi", reference="abc12345"
        )

        for expected in (
            "Test Bakery", "pina colada cake", "size: 1kg", "needed_by: Saturday",
            CUSTOMER, "Amina", "2026-10-17 15:00", "unlisted product",
            "confirm whether you can make this", "actual price", "abc12345",
            "I want a pina colada cake",
        ):
            self.assertIn(expected, text)
        self.assertLessEqual(len(text), 4096)

    def test_the_notification_survives_missing_optional_details(self):
        record = {
            "requested_product": "cake",
            "details": {},
            "wa_id": CUSTOMER,
            "customer_name": None,
            "reason": "customer_request",
            "created_at": None,
            "context": [],
        }
        text = handover_service.notification_text("Test Bakery", record)
        self.assertIn("cake", text)
        self.assertNotIn("Customer name:", text)
        self.assertNotIn("Recent conversation", text)


if __name__ == "__main__":
    unittest.main()

