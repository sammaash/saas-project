"""Portal redesign Stage 2: inbox retrieval, human handover and the pre-generation gate.

Unit and webhook tests are database-free. ``Stage2DatabaseTests`` needs
SAAS_TEST_DATABASE_URL (a disposable database with migrations applied) and is
SKIPPED otherwise; it never touches staging or production.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

from platform_admin.app import create_app
from platform_admin.auth import Authenticator, Identity, LoginRateLimiter
from platform_admin.config import Settings
from platform_admin.routes import tenant_auth as tenant_auth_module
from platform_admin.services import conversation_control, outbound_dispatch
from platform_admin.services.human_request import is_human_request
from platform_admin.services.outbound_dispatch import DispatchOutcome
from platform_admin.services.validation import ServiceError

TENANT = str(uuid.uuid4())
CONV = str(uuid.uuid4())


# --------------------------------------------------------------------------- helpers
class _Column:
    def __init__(self, name):
        self.name = name


class _StubCursor:
    def __init__(self, results=()):
        self._results = list(results)
        self.executed = []
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        result = (
            self._results.pop(0) if self._results else {"columns": ("ok",), "rows": [("ok",)]}
        )
        if isinstance(result, Exception):
            raise result
        self.description = [_Column(name) for name in result["columns"]]
        self._rows = list(result["rows"])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


@contextlib.contextmanager
def _txn(cursor):
    yield cursor


def _settings(**overrides):
    values = {"secret_key": "s" * 40, "cookie_secure": False}
    values.update(overrides)
    return Settings(**values)


def _events(cursor):
    return [
        params[3]
        for sql, params in cursor.executed
        if "insert into public.conversation_control_events" in sql
    ]


# --------------------------------------------------------------------------- detector
class HumanRequestDetectorTests(unittest.TestCase):
    POSITIVE = (
        "Can I speak to a human?",
        "I want to talk to a real person",
        "human please",
        "Agent!",
        "connect me to an agent",
        "I need to speak with someone from your team",
        "talk to staff",
        "Is there a human there?",
        "I'd like to speak to customer service",
        "nataka kuongea na mtu",
        "Can I talk to a person pls",
    )
    NEGATIVE = (
        "I don't need a human",
        "no need for an agent, just the price",
        "How much is a cake for 20 people?",
        "Is this person available on Saturday?",
        "the cake is for someone special",
        "I am a human",
        "what time do you close",
        "someone told me you sell cupcakes",
        "",
        None,
    )

    def test_explicit_requests_are_detected(self):
        for text in self.POSITIVE:
            with self.subTest(text=text):
                self.assertTrue(is_human_request(text))

    def test_ordinary_messages_and_negations_are_not(self):
        for text in self.NEGATIVE:
            with self.subTest(text=text):
                self.assertFalse(is_human_request(text))


# --------------------------------------------------------------------------- states
class IndicatorTests(unittest.TestCase):
    def test_colour_mapping(self):
        self.assertEqual(conversation_control.indicator("ai_active", False), "green")
        self.assertEqual(conversation_control.indicator(None, None), "green")
        self.assertEqual(conversation_control.indicator("paused", True), "orange")
        self.assertEqual(conversation_control.indicator("ai_active", True), "orange")
        self.assertEqual(conversation_control.indicator("human", True), "orange")
        self.assertEqual(conversation_control.indicator("human", False), "red")
        self.assertEqual(conversation_control.indicator("paused", False), "red")


class RequestHumanUnitTests(unittest.TestCase):
    def _run(self, mode, attention):
        cursor = _StubCursor(
            [
                {"columns": ("ok",), "rows": [("ok",)]},  # lock_timeout
                {"columns": ("ai_mode", "needs_attention"), "rows": [(mode, attention)]},
            ]
        )
        with patch(
            "platform_admin.services.conversation_control.tenant_transaction",
            lambda *a, **k: _txn(cursor),
        ):
            result = conversation_control.request_human(
                _settings(), tenant_id=TENANT, conversation_id=CONV, source="customer_message"
            )
        return result, cursor

    def test_ai_active_becomes_paused_with_alert_and_two_system_events(self):
        result, cursor = self._run("ai_active", False)
        self.assertEqual(result["mode"], "paused")
        self.assertTrue(result["changed"])
        self.assertTrue(any("for update" in sql for sql, _ in cursor.executed))
        self.assertEqual(_events(cursor), ["ai_paused", "needs_attention_set"])
        actors = [p[1] for sql, p in cursor.executed if "conversation_control_events" in sql]
        self.assertEqual(actors, [None, None])  # system actor

    def test_human_owned_conversation_keeps_its_mode_and_gains_the_alert(self):
        result, cursor = self._run("human", False)
        self.assertEqual(result["mode"], "human")
        self.assertEqual(_events(cursor), ["needs_attention_set"])

    def test_repeat_request_is_a_noop(self):
        result, cursor = self._run("paused", True)
        self.assertFalse(result["changed"])
        self.assertEqual(_events(cursor), [])
        self.assertFalse(any(sql.startswith("update") for sql, _ in cursor.executed))


class SetModeAcknowledgesAlertTests(unittest.TestCase):
    def _set(self, previous, attention, new_mode, actor="user-1"):
        cursor = _StubCursor(
            [
                {"columns": ("ok",), "rows": [("ok",)]},
                {"columns": ("ai_mode", "needs_attention"), "rows": [(previous, attention)]},
            ]
        )
        with patch(
            "platform_admin.services.conversation_control.tenant_transaction",
            lambda *a, **k: _txn(cursor),
        ):
            result = conversation_control.set_mode(
                _settings(), actor_user_id=actor, tenant_id=TENANT,
                conversation_id=CONV, new_mode=new_mode,
            )
        return result, cursor

    def test_take_over_clears_the_orange_alert(self):
        result, cursor = self._set("paused", True, "human")
        self.assertEqual(result, "changed")
        self.assertEqual(_events(cursor), ["human_takeover", "needs_attention_cleared"])

    def test_resume_clears_the_alert(self):
        _result, cursor = self._set("paused", True, "ai_active")
        self.assertEqual(_events(cursor), ["ai_resumed", "needs_attention_cleared"])

    def test_take_over_when_already_human_still_acknowledges(self):
        result, cursor = self._set("human", True, "human")
        self.assertEqual(result, "unchanged")
        self.assertEqual(_events(cursor), ["needs_attention_cleared"])

    def test_pause_keeps_the_alert(self):
        _result, cursor = self._set("ai_active", True, "paused")
        self.assertEqual(_events(cursor), ["ai_paused"])


class DispatchNoticeTests(unittest.TestCase):
    def _dispatch(self, mode, **overrides):
        cursor = _StubCursor(
            [
                {"columns": ("ok",), "rows": [("ok",)]},
                {"columns": ("ai_mode",), "rows": [(mode,)]},
            ]
        )
        calls = []
        arguments = dict(
            tenant_id=TENANT, conversation_id=CONV, message_row_id=str(uuid.uuid4()),
            sender_type="system", send=lambda: calls.append(1) or "wamid.n",
        )
        arguments.update(overrides)
        with patch(
            "platform_admin.services.outbound_dispatch.tenant_transaction",
            lambda *a, **k: _txn(cursor),
        ):
            outcome = outbound_dispatch.dispatch_reply(_settings(), **arguments)
        return outcome, calls

    def test_notice_is_sent_while_paused(self):
        outcome, calls = self._dispatch("paused", notice=True)
        self.assertEqual(outcome, DispatchOutcome("sent", wamid="wamid.n"))
        self.assertEqual(calls, [1])

    def test_without_notice_paused_is_still_suppressed(self):
        outcome, calls = self._dispatch("paused")
        self.assertEqual(outcome.kind, "suppressed")
        self.assertEqual(calls, [])

    def test_model_replies_never_carry_notice_by_default(self):
        outcome, calls = self._dispatch("human", sender_type="assistant")
        self.assertEqual(outcome.kind, "suppressed")
        self.assertEqual(calls, [])


# --------------------------------------------------------------------------- webhook
def _payload(text="Hello", message_id="wamid.in-1"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "phone-test-1"},
            "messages": [{"id": message_id, "from": "+254700000001", "text": {"body": text}}],
        }}]}],
    }


class WebhookGateTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            _settings(whatsapp_verify_token="v", whatsapp_app_secret="app-secret"),
            bot_provider=object(),
        )
        self.client = self.app.test_client()
        self.stubs = {}
        for name, target, kwargs in (
            ("resolve", "platform_admin.routes.whatsapp_service.resolve_channel",
             {"return_value": {"tenant_id": TENANT, "channel_id": "ch-1",
                               "credentials_ref": "ref"}}),
            ("claim", "platform_admin.routes.whatsapp_service.claim_event",
             {"return_value": True}),
            ("update_event", "platform_admin.routes.whatsapp_service.update_event", {}),
            ("customer", "platform_admin.routes.customer_service.get_or_create_customer",
             {"return_value": {"id": "cust-1", "name": None}}),
            ("conversation", "platform_admin.routes.customer_service.get_or_create_conversation",
             {"return_value": {"id": CONV}}),
            ("save_message", "platform_admin.routes.customer_service.save_message",
             {"return_value": {"id": "row-1"}}),
            ("touch", "platform_admin.routes.customer_service.update_conversation_activity", {}),
            ("engine", "platform_admin.routes.BotResponseEngine", {}),
            ("send", "platform_admin.routes.whatsapp_service.send_text",
             {"return_value": "wamid.out"}),
            ("dispatch", "platform_admin.routes.outbound_dispatch.dispatch_reply",
             {"return_value": DispatchOutcome("sent", wamid="wamid.out")}),
            ("skipped", "platform_admin.routes.conversation_control.record_generation_skipped", {}),
            ("request_human", "platform_admin.routes.conversation_control.request_human",
             {"return_value": {"changed": True, "mode": "paused"}}),
            ("assist", "platform_admin.routes.handover_service.request_staff_assistance",
             {"return_value": (True, "ok")}),
            ("menu_doc", "platform_admin.routes.menu_service.load_document",
             {"return_value": {"business_name": "Cindy Bakes"}}),
            ("history", "platform_admin.routes.whatsapp_service.conversation_history",
             {"return_value": []}),
        ):
            stub = patch(target, **kwargs)
            self.stubs[name] = stub.start()
            self.addCleanup(stub.stop)

    def _state(self, value):
        stub = patch(
            "platform_admin.routes.conversation_control.get_state",
            **({"side_effect": value} if isinstance(value, Exception) else {"return_value": value}),
        )
        mock = stub.start()
        self.addCleanup(stub.stop)
        return mock

    def _post(self, text="Hello"):
        body = json.dumps(_payload(text)).encode()
        signature = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        return self.client.post(
            "/webhooks/whatsapp", data=body,
            headers={"Content-Type": "application/json",
                     "X-Hub-Signature-256": f"sha256={signature}"},
        )

    def test_paused_conversation_never_reaches_the_model_or_sends(self):
        for mode in ("paused", "human"):
            with self.subTest(mode=mode):
                self._state((mode, False))
                response = self._post("How much is a cake?")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["stage"], "before_generation")
                self.stubs["engine"].assert_not_called()
                self.stubs["dispatch"].assert_not_called()
                self.stubs["send"].assert_not_called()
                self.stubs["skipped"].assert_called()
                self.assertEqual(
                    self.stubs["update_event"].call_args.args[3:], ("failed", "ai_reply_suppressed")
                )
                # the inbound message was still stored, and the thread bumped
                self.assertEqual(self.stubs["save_message"].call_args.kwargs["direction"], "inbound")
                self.stubs["touch"].assert_called()

    def test_menu_commands_are_also_silent_while_paused(self):
        self._state(("human", False))
        response = self._post("menu")
        self.assertTrue(response.json["suppressed"])
        self.stubs["dispatch"].assert_not_called()

    def test_unreadable_state_fails_closed(self):
        self._state(RuntimeError("db down"))
        response = self._post("Hello")
        self.assertTrue(response.json["suppressed"])
        self.stubs["engine"].assert_not_called()
        self.stubs["dispatch"].assert_not_called()
        self.assertEqual(
            self.stubs["update_event"].call_args.args[3:], ("failed", "ai_state_unavailable")
        )

    def test_human_request_pauses_first_then_sends_one_notice(self):
        self._state(("ai_active", False))
        order = []
        self.stubs["request_human"].side_effect = lambda *a, **k: order.append("pause") or {}
        self.stubs["assist"].side_effect = lambda *a, **k: order.append("notify") or (True, "")
        self.stubs["dispatch"].side_effect = (
            lambda *a, **k: order.append("send") or DispatchOutcome("sent", wamid="w")
        )
        response = self._post("Can I talk to a real person please?")
        self.assertTrue(response.json["human_requested"])
        self.assertEqual(order, ["pause", "notify", "send"])
        self.stubs["engine"].assert_not_called()
        kwargs = self.stubs["dispatch"].call_args.kwargs
        self.assertTrue(kwargs["notice"])
        self.assertEqual(kwargs["sender_type"], "system")
        self.assertEqual(self.stubs["assist"].call_args.kwargs["source"], "customer_message")
        outbound = self.stubs["save_message"].call_args_list[-1].kwargs
        self.assertEqual(outbound["reply_to_provider_message_id"], "wamid.in-1")
        self.assertEqual(outbound["sender_type"], "system")

    def test_human_request_while_paused_raises_alert_but_sends_nothing(self):
        self._state(("paused", False))
        self._post("I want to speak to a human")
        self.stubs["request_human"].assert_called_once()
        self.stubs["dispatch"].assert_not_called()
        self.stubs["engine"].assert_not_called()

    def test_redelivered_human_request_is_not_answered_twice(self):
        from platform_admin.services import customers as customer_service

        self._state(("ai_active", False))
        self.stubs["save_message"].side_effect = [
            {"id": "in-row"}, customer_service.DuplicateReplyError("dup"),
        ]
        with patch(
            "platform_admin.routes.customer_service.find_outbound_reply",
            return_value={"provider_message_id": "wamid.earlier"},
        ):
            response = self._post("human please")
        self.assertTrue(response.json["duplicate_reply"])
        self.stubs["dispatch"].assert_not_called()

    def test_ordinary_message_with_ai_active_still_reaches_the_model(self):
        self._state(("ai_active", False))
        response_obj = MagicMock(text="Hi!", quotation=None, quotation_failed=False,
                                 quotation_pdf=None, handover=None, handover_failed=False)
        self.stubs["engine"].return_value.respond.return_value = response_obj
        with patch("platform_admin.routes.handover_service.handle_turn", return_value=None), \
                patch("platform_admin.routes.whatsapp_service.save_conversation"):
            response = self._post("Do you have chocolate cake?")
        self.assertEqual(response.status_code, 200)
        self.stubs["engine"].return_value.respond.assert_called_once()
        self.assertFalse(self.stubs["dispatch"].call_args.kwargs["notice"])
        self.stubs["request_human"].assert_not_called()

    def test_model_recorded_customer_request_pauses_before_its_reply(self):
        self._state(("ai_active", False))
        response_obj = MagicMock(text="x", quotation=None, quotation_failed=False,
                                 quotation_pdf=None)

        def fake_turn(*args, outcome=None, **kwargs):
            outcome["reason"] = "customer_request"
            return "I've let the team know."

        self.stubs["engine"].return_value.respond.return_value = response_obj
        with patch("platform_admin.routes.handover_service.handle_turn", side_effect=fake_turn), \
                patch("platform_admin.routes.whatsapp_service.save_conversation"):
            self._post("there is a problem with my order, I'd rather sort it with you")
        self.stubs["request_human"].assert_called_once()
        self.assertEqual(
            self.stubs["request_human"].call_args.kwargs["source"], "ai_handover_tool"
        )
        self.assertTrue(self.stubs["dispatch"].call_args.kwargs["notice"])

    def test_product_handover_does_not_pause_ai(self):
        self._state(("ai_active", False))
        response_obj = MagicMock(text="x", quotation=None, quotation_failed=False,
                                 quotation_pdf=None)

        def fake_turn(*args, outcome=None, **kwargs):
            outcome["reason"] = "unlisted_product"
            return "Our staff will confirm the price."

        self.stubs["engine"].return_value.respond.return_value = response_obj
        with patch("platform_admin.routes.handover_service.handle_turn", side_effect=fake_turn), \
                patch("platform_admin.routes.whatsapp_service.save_conversation"):
            self._post("do you make a 5 tier wedding cake")
        self.stubs["request_human"].assert_not_called()
        self.assertFalse(self.stubs["dispatch"].call_args.kwargs["notice"])


# --------------------------------------------------------------------------- portal
class _AcceptAll(Authenticator):
    def login(self, email, password):
        return Identity(str(uuid.uuid4()), email)


class PortalControlTests(unittest.TestCase):
    def setUp(self):
        tenant_auth_module._TENANT_LIMITER = LoginRateLimiter()
        self.app = create_app(_settings(), authenticator=_AcceptAll())
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["t_user_id"] = str(uuid.uuid4())
            session["t_email"] = "member@example.test"
            session["t_exp"] = 2**40
        member = patch("saas.tenant_membership.membership_for", return_value={"role": "staff"})
        member.start()
        self.addCleanup(member.stop)

    def _csrf(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf-token"
        return "test-csrf-token"

    def test_resume_requires_explicit_confirmation(self):
        with patch(
            "platform_admin.routes.tenant_dashboard.customers_service.inbox_signature",
            return_value="s",
        ), patch(
            "platform_admin.routes.tenant_dashboard.conversation_control.set_mode",
            return_value="changed",
        ) as set_mode:
            token = self._csrf()
            unconfirmed = self.client.post(
                f"/t/{TENANT}/inbox/{CONV}/mode", data={"mode": "ai_active", "csrf_token": token}
            )
            self.assertEqual(unconfirmed.status_code, 400)
            self.assertIn("Confirm that the AI should resume", unconfirmed.get_data(as_text=True))
            set_mode.assert_not_called()
            confirmed = self.client.post(
                f"/t/{TENANT}/inbox/{CONV}/mode",
                data={"mode": "ai_active", "confirm": "yes", "csrf_token": token},
            )
            self.assertEqual(confirmed.status_code, 302)
            set_mode.assert_called_once()

    def test_updates_endpoint_returns_only_a_marker(self):
        with patch(
            "platform_admin.routes.tenant_dashboard.customers_service.inbox_signature",
            return_value="abc123",
        ) as signature:
            response = self.client.get(f"/t/{TENANT}/inbox/updates?c={CONV}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json, {"signature": "abc123"})
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(signature.call_args.args[2], CONV)
            self.assertEqual(self.client.get(f"/t/{TENANT}/inbox/updates?c=nope").status_code, 400)

    def test_updates_endpoint_requires_membership(self):
        with patch("saas.tenant_membership.membership_for", return_value=None):
            response = self.client.get(f"/t/{TENANT}/inbox/updates")
        self.assertIn(response.status_code, (302, 403, 404))


# --------------------------------------------------------------------------- database
class Stage2DatabaseTests(unittest.TestCase):
    """Real RLS + row locks. SKIPPED without a disposable test database."""

    @classmethod
    def setUpClass(cls):
        try:
            from .saas_test_support import (
                TENANT_ONE_ID, TENANT_TWO_ID, app_backend_test_url, require_test_database_url,
            )
        except ImportError:
            from saas_test_support import (  # type: ignore[no-redef]
                TENANT_ONE_ID, TENANT_TWO_ID, app_backend_test_url, require_test_database_url,
            )
        try:
            require_test_database_url()
        except Exception as error:  # noqa: BLE001
            raise unittest.SkipTest(str(error))
        cls.tenant_id = TENANT_ONE_ID
        cls.other_tenant = TENANT_TWO_ID
        cls.settings = Settings(
            secret_key="s" * 40, cookie_secure=False, app_database_url=app_backend_test_url()
        )

    def _conversation(self):
        from platform_admin.services import customers as customer_service
        from saas.tenant_context import tenant_transaction

        with tenant_transaction(self.tenant_id, self.settings.app_database_url) as cursor:
            cursor.execute(
                "select id from public.tenant_channels where tenant_id = app.current_tenant_id() limit 1"
            )
            channel_id = str(cursor.fetchone()[0])
        customer = customer_service.get_or_create_customer(
            self.settings, self.tenant_id, channel_id, f"+2547{uuid.uuid4().hex[:10]}"
        )
        conversation = customer_service.get_or_create_conversation(
            self.settings, self.tenant_id, customer["id"], channel_id
        )

        def _cleanup():
            with tenant_transaction(self.tenant_id, self.settings.app_database_url) as cursor:
                cursor.execute(
                    "delete from public.customers where id = %s and tenant_id = app.current_tenant_id()",
                    (str(customer["id"]),),
                )

        self.addCleanup(_cleanup)
        return channel_id, str(conversation["id"])

    def _insert_messages(self, channel_id, conversation_id, count, start):
        from saas.tenant_context import tenant_transaction

        with tenant_transaction(self.tenant_id, self.settings.app_database_url) as cursor:
            for index in range(count):
                cursor.execute(
                    """
                    insert into public.messages
                      (tenant_id, channel_id, conversation_id, direction, sender_type,
                       message_type, body_text, received_at)
                    values (app.current_tenant_id(), %s, %s, 'inbound', 'customer', 'text', %s, %s)
                    """,
                    (channel_id, conversation_id, f"m{index:03d}", start + timedelta(seconds=index)),
                )

    def test_inbox_shows_the_newest_messages_and_pages_backwards(self):
        from platform_admin.services import customers as customer_service

        channel_id, conversation_id = self._conversation()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._insert_messages(channel_id, conversation_id, 174, start)
        latest, older = customer_service.latest_messages(
            self.settings, self.tenant_id, conversation_id, limit=50
        )
        self.assertEqual([m["body_text"] for m in latest][-1], "m173")
        self.assertEqual(latest[0]["body_text"], "m124")  # oldest of the newest 50, first
        self.assertIsNotNone(older)
        seen = [m["body_text"] for m in latest]
        while older:
            page, older = customer_service.latest_messages(
                self.settings, self.tenant_id, conversation_id, limit=50, before=older
            )
            seen = [m["body_text"] for m in page] + seen
        self.assertEqual(seen, [f"m{i:03d}" for i in range(174)])  # no gaps, no duplicates
        # Same timestamps do not break the keyset (ties broken by id).
        with self.assertRaises(ServiceError):
            customer_service.latest_messages(
                self.settings, self.tenant_id, conversation_id, before="garbage"
            )

    def test_other_tenants_get_nothing(self):
        from platform_admin.services import customers as customer_service

        channel_id, conversation_id = self._conversation()
        self._insert_messages(channel_id, conversation_id, 3, datetime.now(timezone.utc))
        rows, older = customer_service.latest_messages(
            self.settings, self.other_tenant, conversation_id
        )
        self.assertEqual((rows, older), ([], None))
        self.assertIsNone(
            conversation_control.get_state(self.settings, self.other_tenant, conversation_id)
        )
        with self.assertRaises(ServiceError):
            conversation_control.request_human(
                self.settings, tenant_id=self.other_tenant,
                conversation_id=conversation_id, source="test",
            )

    def test_full_handover_lifecycle(self):
        from platform_admin.services import customers as customer_service

        channel_id, conversation_id = self._conversation()
        get = lambda: conversation_control.get_state(  # noqa: E731
            self.settings, self.tenant_id, conversation_id
        )
        self.assertEqual(get(), ("ai_active", False))  # green
        conversation_control.request_human(
            self.settings, tenant_id=self.tenant_id,
            conversation_id=conversation_id, source="test",
        )
        self.assertEqual(get(), ("paused", True))  # orange
        repeat = conversation_control.request_human(
            self.settings, tenant_id=self.tenant_id,
            conversation_id=conversation_id, source="test",
        )
        self.assertFalse(repeat["changed"])
        # A staff reply takes over and acknowledges; it never resumes AI.
        from saas.tenant_context import tenant_transaction

        try:
            from .saas_test_support import TENANT_ONE_OWNER_USER_ID as actor
        except ImportError:
            from saas_test_support import TENANT_ONE_OWNER_USER_ID as actor  # type: ignore
        customer_service.add_staff_reply(
            self.settings, tenant_id=self.tenant_id, channel_id=channel_id,
            conversation_id=conversation_id, actor_user_id=actor, body_text="Hi, Jane here",
        )
        self.assertEqual(get(), ("human", False))  # red
        conversation_control.set_mode(
            self.settings, actor_user_id=actor, tenant_id=self.tenant_id,
            conversation_id=conversation_id, new_mode="ai_active",
        )
        self.assertEqual(get(), ("ai_active", False))  # green again
        with tenant_transaction(self.tenant_id, self.settings.app_database_url) as cursor:
            # Events written in one transaction share created_at (now()); their
            # relative order is not defined, so compare each transaction as a set.
            cursor.execute(
                "select created_at, array_agg(event_type::text order by event_type) "
                "from public.conversation_control_events "
                "where conversation_id = %s group by created_at order by created_at",
                (conversation_id,),
            )
            events = [set(row[1]) for row in cursor.fetchall()]
        self.assertEqual(
            events,
            [{"ai_paused", "needs_attention_set"},
             {"needs_attention_cleared", "human_takeover"},
             {"ai_resumed"}],
        )

    def test_signature_changes_when_a_message_arrives(self):
        from platform_admin.services import customers as customer_service

        channel_id, conversation_id = self._conversation()
        before = customer_service.inbox_signature(self.settings, self.tenant_id, conversation_id)
        self._insert_messages(channel_id, conversation_id, 1, datetime.now(timezone.utc))
        after = customer_service.inbox_signature(self.settings, self.tenant_id, conversation_id)
        self.assertNotEqual(before, after)

    def test_thread_list_puts_orange_first_and_filters_by_colour(self):
        from platform_admin.services import customers as customer_service

        _channel, quiet = self._conversation()
        _channel, asked = self._conversation()
        conversation_control.request_human(
            self.settings, tenant_id=self.tenant_id, conversation_id=asked, source="test"
        )
        rows, _ = customer_service.inbox_threads(self.settings, self.tenant_id, page_size=100)
        ids = [str(r["id"]) for r in rows]
        self.assertLess(ids.index(asked), ids.index(quiet))
        attention, _ = customer_service.inbox_threads(
            self.settings, self.tenant_id, view="attention", page_size=100
        )
        self.assertIn(asked, [str(r["id"]) for r in attention])
        self.assertNotIn(quiet, [str(r["id"]) for r in attention])
        with self.assertRaises(ServiceError):
            customer_service.inbox_threads(self.settings, self.tenant_id, view="purple")


if __name__ == "__main__":
    unittest.main()
