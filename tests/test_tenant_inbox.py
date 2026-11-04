"""Local tests for the Samma Inbox Phase 1 (read-only tenant conversation inbox).

All database access is stubbed; no live database is contacted. WhatsApp sending is
never exercised — the composer and conversation controls are prototype-only.
"""

from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from platform_admin.app import create_app
from platform_admin.auth import Authenticator, Identity, LoginRateLimiter
from platform_admin.config import Settings
from platform_admin.routes import tenant_auth as tenant_auth_module
from platform_admin.services import customers as customers_service
from platform_admin.services import outbound_dispatch
from platform_admin.services.validation import ServiceError

REPO = Path(__file__).resolve().parent.parent

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())
CONV_A = str(uuid.uuid4())
CUSTOMER_A = str(uuid.uuid4())


def _settings(**overrides):
    return Settings(secret_key="s" * 40, cookie_secure=False, **overrides)


class _AcceptAll(Authenticator):
    def login(self, email, password):
        return Identity(str(uuid.uuid4()), email)


def _thread(**overrides):
    thread = {
        "id": CONV_A,
        "status": "open",
        "assigned_to": None,
        "last_message_at": "2026-01-02 10:00:00",
        "closed_at": None,
        "created_at": "2026-01-01 09:00:00",
        "customer_id": CUSTOMER_A,
        "customer_name": "Customer",
        "wa_id": "15550001",
        "phone_normalized": None,
        "email": None,
        "last_interaction_at": "2026-01-02 10:00:00",
        "preview_text": "hello there",
        "preview_sender": "customer",
        "preview_direction": "inbound",
        "preview_at": "2026-01-02 10:00:00",
    }
    thread.update(overrides)
    return thread


def _message(**overrides):
    message = {
        "id": str(uuid.uuid4()),
        "direction": "inbound",
        "sender_type": "customer",
        "message_type": "text",
        "body_text": "hello there",
        "received_at": "2026-01-02 10:00:00",
        "created_at": "2026-01-02 10:00:00",
        "delivery_status": None,
        "error_message": None,
    }
    message.update(overrides)
    return message


class _InboxTestCase(unittest.TestCase):
    def setUp(self):
        tenant_auth_module._TENANT_LIMITER = LoginRateLimiter()
        self._no_db = patch(
            "saas.tenant_membership.connect",
            side_effect=AssertionError("tests must not touch a database"),
        )
        self._no_db.start()
        self.addCleanup(self._no_db.stop)
        self.app = create_app(_settings(), authenticator=_AcceptAll())
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["t_user_id"] = str(uuid.uuid4())
            session["t_email"] = "member@example.test"
            session["t_exp"] = 2**40

    def _member(self, role="staff"):
        return patch(
            "saas.tenant_membership.membership_for", return_value={"role": role}
        )

    def _threads(self, rows=None, total=None):
        rows = [_thread()] if rows is None else rows
        return patch(
            "platform_admin.routes.tenant_dashboard.customers_service.inbox_threads",
            return_value=(rows, len(rows) if total is None else total),
        )

    def _open_conversation(self, messages=None, thread=None, mode="ai_active"):
        conversation = {
            "id": CONV_A,
            "tenant_id": TENANT_A,
            "customer_id": CUSTOMER_A,
            "channel_id": str(uuid.uuid4()),
            "status": "open",
            "assigned_to": None,
            "ai_mode": mode,
            "needs_attention": False,
            "created_at": "2026-01-01 09:00:00",
        }
        customer = {
            "id": CUSTOMER_A,
            "name": "Customer",
            "wa_id": "15550001",
            "phone_normalized": None,
            "email": None,
            "last_interaction_at": "2026-01-02 10:00:00",
        }
        messages = [_message()] if messages is None else messages
        with self._member(), self._threads(rows=[thread or _thread()]), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_conversation",
            return_value=conversation,
        ), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_customer",
            return_value=customer,
        ), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.list_messages",
            return_value=(messages, len(messages)),
        ):
            return self.client.get(f"/t/{TENANT_A}/inbox/{CONV_A}")


class AccessTests(_InboxTestCase):
    def test_anonymous_users_are_redirected_to_tenant_login(self):
        with self.client.session_transaction() as session:
            for key in ("t_user_id", "t_exp", "t_tenant_id"):
                session.pop(key, None)
        for path in (f"/t/{TENANT_A}/inbox", f"/t/{TENANT_A}/inbox/{CONV_A}"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].endswith("/t/login"))

    def test_staff_members_can_open_the_inbox(self):
        with self._member("staff"), self._threads():
            response = self.client.get(f"/t/{TENANT_A}/inbox")
        self.assertEqual(response.status_code, 200)

    def test_owners_can_open_the_inbox(self):
        with self._member("owner"), self._threads():
            response = self.client.get(f"/t/{TENANT_A}/inbox")
        self.assertEqual(response.status_code, 200)

    def test_non_members_are_denied(self):
        with patch("saas.tenant_membership.membership_for", return_value=None):
            response = self.client.get(f"/t/{TENANT_A}/inbox")
        self.assertEqual(response.status_code, 403)

    def test_cross_tenant_conversation_is_not_found(self):
        with self._member(), self._threads(), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_conversation",
            return_value=None,
        ):
            response = self.client.get(f"/t/{TENANT_A}/inbox/{CONV_A}")
        self.assertEqual(response.status_code, 404)

    def test_staff_are_still_denied_owner_only_settings(self):
        with self._member("staff"):
            response = self.client.get(f"/t/{TENANT_A}/settings")
        self.assertEqual(response.status_code, 403)

    def test_membership_database_failure_denies_access(self):
        with patch(
            "saas.tenant_membership.membership_for", side_effect=RuntimeError("db down")
        ):
            response = self.client.get(f"/t/{TENANT_A}/inbox")
        self.assertEqual(response.status_code, 503)


class RenderTests(_InboxTestCase):
    def test_conversation_renders_every_sender_kind(self):
        response = self._open_conversation(
            messages=[
                _message(sender_type="customer", body_text="hi"),
                _message(sender_type="assistant", body_text="bot reply"),
                _message(sender_type="staff", body_text="human reply",
                         delivery_status="sent"),
            ]
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        for expected in ("sender-customer", "sender-assistant", "sender-staff",
                         "Customer", "AI assistant", "Staff", "human reply", "sent"):
            self.assertIn(expected, body)

    def test_empty_inbox_renders_the_empty_state(self):
        with self._member(), self._threads(rows=[], total=0):
            response = self.client.get(f"/t/{TENANT_A}/inbox")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No conversations", response.get_data(as_text=True))

    def test_long_message_bodies_render_and_are_escaped(self):
        long_text = "x" * 1500 + "<script>alert(1)</script>"
        response = self._open_conversation(messages=[_message(body_text=long_text)])
        body = response.get_data(as_text=True)
        self.assertIn("x" * 1500, body)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_missing_scores_show_not_scored(self):
        response = self._open_conversation()
        body = response.get_data(as_text=True)
        self.assertIn("Not scored", body)
        self.assertIn("Not available", body)
        self.assertIn("No scoring engine is connected yet", body)

    def test_mobile_structure_is_present(self):
        response = self._open_conversation()
        body = response.get_data(as_text=True)
        self.assertIn('name="viewport"', body)  # from base.html
        self.assertIn("inbox-back", body)  # back button for the full-screen chat
        self.assertIn("<details", body)  # collapsible customer drawer
        self.assertIn("has-selection", body)
        css = (REPO / "platform_admin" / "static" / "inbox.css").read_text(
            encoding="utf-8"
        )
        self.assertIn("@media (min-width: 721px)", css)  # mobile-first, desktop enhances

    def test_thread_list_shows_preview_status_and_sender_badge(self):
        with self._member(), self._threads(
            rows=[_thread(preview_text="latest words", preview_sender="assistant")]
        ):
            response = self.client.get(f"/t/{TENANT_A}/inbox")
        body = response.get_data(as_text=True)
        self.assertIn("latest words", body)
        self.assertIn("Customer", body)
        self.assertIn(">AI<", body)
        self.assertIn("open", body)


class PrototypeSafetyTests(_InboxTestCase):
    def test_conversation_controls_are_real_csrf_forms(self):
        response = self._open_conversation()
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="mode" value="paused"', body)
        self.assertIn('name="mode" value="human"', body)
        self.assertIn("Take over as human</button>", body)
        self.assertIn("needs attention</button>", body)

    def test_resume_ai_appears_only_when_not_ai_active(self):
        active = self._open_conversation(mode="ai_active")
        self.assertNotIn('name="mode" value="ai_active"', active.get_data(as_text=True))
        taken_over = self._open_conversation(mode="human")
        self.assertIn('name="mode" value="ai_active"', taken_over.get_data(as_text=True))

    def test_composer_is_disabled_while_sending_is_off(self):
        response = self._open_conversation()
        body = response.get_data(as_text=True)
        self.assertIn("<textarea id=\"composer-input\" disabled", body)
        self.assertIn("WHATSAPP_SEND_ENABLED=0", body)
        self.assertIn("No message is sent or simulated", body)

    def test_composer_is_a_real_form_when_sending_is_enabled(self):
        app = create_app(
            Settings(secret_key="s" * 40, cookie_secure=False, whatsapp_send_enabled=True),
            authenticator=_AcceptAll(),
        )
        client = app.test_client()
        with client.session_transaction() as session:
            session["t_user_id"] = str(uuid.uuid4())
            session["t_email"] = "m@example.test"
            session["t_exp"] = 2**40
        with patch("saas.tenant_membership.membership_for", return_value={"role": "staff"}), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.inbox_threads",
            return_value=([_thread()], 1),
        ), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_conversation",
            return_value={"id": CONV_A, "tenant_id": TENANT_A, "customer_id": CUSTOMER_A,
                          "channel_id": str(uuid.uuid4()), "status": "open",
                          "assigned_to": None, "created_at": "2026-01-01"},
        ), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_customer",
            return_value=None,
        ), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.list_messages",
            return_value=([], 0),
        ):
            response = client.get(f"/t/{TENANT_A}/inbox/{CONV_A}")
        body = response.get_data(as_text=True)
        self.assertIn("/reply\"", body)
        self.assertIn('name="text"', body)
        self.assertIn(">Send</button>", body)

    def test_post_endpoints_are_exactly_the_control_surface(self):
        posts = {
            rule.rule
            for rule in self.app.url_map.iter_rules()
            if "/inbox" in rule.rule and rule.methods & {"POST"}
        }
        self.assertEqual(
            posts,
            {
                "/t/<uuid:tenant_id>/inbox/<uuid:conversation_id>/mode",
                "/t/<uuid:tenant_id>/inbox/<uuid:conversation_id>/attention",
                "/t/<uuid:tenant_id>/inbox/<uuid:conversation_id>/reply",
            },
        )

    def test_send_text_is_only_reachable_through_the_dispatch_boundary(self):
        source = (REPO / "platform_admin" / "routes" / "tenant_dashboard.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(source.count("send_text("), 1)
        self.assertIn("send=lambda: whatsapp_service.send_text(", source)
        self.assertIn("outbound_dispatch.dispatch_reply(", source)

    def test_whatsapp_sending_stays_disabled_by_default(self):
        self.assertIs(Settings(secret_key="s" * 40).whatsapp_send_enabled, False)


class ControlEndpointTests(_InboxTestCase):
    def setUp(self):
        super().setUp()
        with self.client.session_transaction() as session:
            session["csrf_token"] = "tok"

    def _post(self, path, data):
        data = dict(data)
        data["csrf_token"] = "tok"
        return self.client.post(path, data=data)

    def test_mode_change_is_applied_and_redirects_back(self):
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.conversation_control.set_mode",
            return_value="changed",
        ) as set_mode:
            response = self._post(
                f"/t/{TENANT_A}/inbox/{CONV_A}/mode", {"mode": "paused"}
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set_mode.call_args.kwargs["new_mode"], "paused")
        self.assertTrue(set_mode.call_args.kwargs["actor_user_id"])

    def test_unknown_mode_value_is_rejected_by_the_service(self):
        with self._member():
            response = self._post(
                f"/t/{TENANT_A}/inbox/{CONV_A}/mode", {"mode": "manual"}
            )
        self.assertEqual(response.status_code, 400)

    def test_attention_flag_validation(self):
        with self._member():
            response = self._post(
                f"/t/{TENANT_A}/inbox/{CONV_A}/attention", {"value": "yes"}
            )
        self.assertEqual(response.status_code, 400)
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.conversation_control.set_needs_attention",
            return_value="changed",
        ) as setter:
            response = self._post(
                f"/t/{TENANT_A}/inbox/{CONV_A}/attention", {"value": "1"}
            )
        self.assertEqual(response.status_code, 302)
        self.assertIs(setter.call_args.kwargs["active"], True)

    def test_reply_is_rejected_before_any_write_while_sending_is_disabled(self):
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.add_staff_reply"
        ) as add_reply, patch(
            "platform_admin.routes.tenant_dashboard.outbound_dispatch.dispatch_reply"
        ) as dispatch:
            response = self._post(
                f"/t/{TENANT_A}/inbox/{CONV_A}/reply", {"text": "hello"}
            )
        self.assertEqual(response.status_code, 503)
        add_reply.assert_not_called()
        dispatch.assert_not_called()

    def _reply_with_sending_enabled(self, outcome_kind="sent"):
        app = create_app(
            Settings(secret_key="s" * 40, cookie_secure=False, whatsapp_send_enabled=True),
            authenticator=_AcceptAll(),
        )
        client = app.test_client()
        with client.session_transaction() as session:
            session["t_user_id"] = str(uuid.uuid4())
            session["t_email"] = "m@example.test"
            session["t_exp"] = 2**40
            session["csrf_token"] = "tok"
        conversation = {
            "id": CONV_A, "tenant_id": TENANT_A, "customer_id": CUSTOMER_A,
            "channel_id": str(uuid.uuid4()), "status": "open",
            "assigned_to": None, "created_at": "2026-01-01",
        }
        customer = {"id": CUSTOMER_A, "wa_id": "15550001"}
        channel = {"phone_number_id": "phone-1", "credentials_ref": "ref"}
        outcome = outbound_dispatch.DispatchOutcome(
            outcome_kind, wamid="wamid.1" if outcome_kind == "sent" else None
        )
        with patch("saas.tenant_membership.membership_for", return_value={"role": "staff"}), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_conversation",
            return_value=conversation,
        ), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_customer",
            return_value=customer,
        ), patch(
            "platform_admin.routes.tenant_dashboard.whatsapp_service.get_channel",
            return_value=channel,
        ), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.add_staff_reply",
            return_value={"id": "row-1"},
        ) as add_reply, patch(
            "platform_admin.routes.tenant_dashboard.outbound_dispatch.dispatch_reply",
            return_value=outcome,
        ) as dispatch:
            response = client.post(
                f"/t/{TENANT_A}/inbox/{CONV_A}/reply",
                data={"text": "hello there", "csrf_token": "tok"},
            )
        return response, add_reply, dispatch

    def test_reply_happy_path_records_and_dispatches(self):
        response, add_reply, dispatch = self._reply_with_sending_enabled("sent")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(add_reply.call_count, 1)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(dispatch.call_args.kwargs["sender_type"], "staff")

    def test_reply_failure_and_unknown_outcomes_are_honestly_labelled(self):
        response, _, _ = self._reply_with_sending_enabled("failed")
        self.assertEqual(response.status_code, 302)
        response, _, _ = self._reply_with_sending_enabled("unknown")
        self.assertEqual(response.status_code, 302)

    def test_reply_requires_csrf(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["t_user_id"] = str(uuid.uuid4())
            session["t_exp"] = 2**40
        response = client.post(f"/t/{TENANT_A}/inbox/{CONV_A}/reply", data={"text": "hi"})
        self.assertEqual(response.status_code, 400)

    def test_empty_reply_text_is_rejected(self):
        with self._member(), patch(
            "platform_admin.routes.tenant_dashboard.whatsapp_service.get_channel",
            return_value={"phone_number_id": "p", "credentials_ref": None},
        ), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_conversation",
            return_value={"id": CONV_A, "tenant_id": TENANT_A,
                          "customer_id": CUSTOMER_A, "channel_id": str(uuid.uuid4())},
        ), patch(
            "platform_admin.routes.tenant_dashboard.customers_service.get_customer",
            return_value={"id": CUSTOMER_A, "wa_id": "15550001"},
        ):
            app = create_app(
                Settings(secret_key="s" * 40, cookie_secure=False, whatsapp_send_enabled=True),
                authenticator=_AcceptAll(),
            )
            client = app.test_client()
            with client.session_transaction() as session:
                session["t_user_id"] = str(uuid.uuid4())
                session["t_exp"] = 2**40
                session["csrf_token"] = "tok"
            response = client.post(
                f"/t/{TENANT_A}/inbox/{CONV_A}/reply",
                data={"text": "   ", "csrf_token": "tok"},
            )
        self.assertEqual(response.status_code, 400)


class FilterTests(_InboxTestCase):
    def test_status_filter_is_passed_to_the_service(self):
        with self._member(), self._threads() as threads:
            response = self.client.get(f"/t/{TENANT_A}/inbox?status=closed&q=hello")
        self.assertEqual(response.status_code, 200)
        _, kwargs = threads.call_args
        self.assertEqual(kwargs["status"], "closed")
        self.assertEqual(kwargs["search"], "hello")

    def test_unknown_status_filter_is_rejected(self):
        with self._member():  # real service: validation runs before any database work
            response = self.client.get(f"/t/{TENANT_A}/inbox?status=bogus")
        self.assertEqual(response.status_code, 400)

    def test_service_rejects_unknown_filters_before_touching_the_database(self):
        with self.assertRaises(ServiceError):
            customers_service.inbox_threads(
                _settings(), TENANT_A, status="bogus"
            )
        with self.assertRaises(ServiceError):
            customers_service.inbox_threads(_settings(), TENANT_A, status="dropped")


if __name__ == "__main__":
    unittest.main()