"""Stage 2 verification against a real database (RLS, row locks, real routes).

SKIPPED unless SAAS_TEST_DATABASE_URL points at a disposable database with migrations and
the dev seed applied (never staging or production). Only the WhatsApp HTTP call and the
AI model are faked; routes, services, dispatch locking and RLS are the real code. Every
row a test creates is removed afterwards with the disposable database's superuser, so
the seeded row counts other suites assert on are unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from platform_admin.app import create_app
from platform_admin.config import Settings
from platform_admin.services import conversation_control

try:
    from . import saas_test_support as support
except ImportError:  # discovery inside the tests directory
    import saas_test_support as support  # type: ignore[no-redef]

REPO = Path(__file__).resolve().parent.parent
TENANT = support.TENANT_ONE_ID
OTHER_TENANT = support.TENANT_TWO_ID
OWNER = support.TENANT_ONE_OWNER_USER_ID
STAFF = support.TENANT_ONE_STAFF_USER_ID
OTHER_OWNER = support.TENANT_TWO_OWNER_USER_ID
OUTSIDER = support.OUTSIDER_USER_ID
PHONE_ID = support.TENANT_ONE_PHONE_NUMBER_ID


def _reply(text="AI reply"):
    return SimpleNamespace(
        text=text, quotation=None, quotation_failed=False, quotation_pdf=None,
        handover=None, handover_failed=False,
    )


class _Engine:
    """Replaces BotResponseEngine; behaviour is set per test through ``impl``."""

    impl = staticmethod(lambda *a, **k: _reply())
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    def respond(self, *args, **kwargs):
        type(self).calls += 1
        return type(self).impl(*args, **kwargs)


class Stage2VerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.super_url = support.require_test_database_url()
        except Exception as error:  # noqa: BLE001 - skip without a disposable database
            raise unittest.SkipTest(str(error))
        cls.app_url = support.app_backend_test_url()
        cls.settings = Settings(
            secret_key="s" * 40,
            cookie_secure=False,
            app_database_url=cls.app_url,
            whatsapp_app_secret="app-secret",
            whatsapp_send_enabled=True,
        )

    # ------------------------------------------------------------------ fixtures
    def setUp(self):
        self.started = datetime.now(timezone.utc)
        self.wa = f"+2547{uuid.uuid4().int % 10**8:08d}"
        self.message_ids: list[str] = []
        self.sends: list[dict] = []
        self.send_lock = threading.Lock()
        _Engine.impl = staticmethod(lambda *a, **k: _reply())
        _Engine.calls = 0
        for target, kwargs in (
            ("platform_admin.routes.BotResponseEngine", {"new": _Engine}),
            ("platform_admin.services.whatsapp.send_text", {"side_effect": self._fake_send}),
        ):
            stub = patch(target, **kwargs)
            stub.start()
            self.addCleanup(stub.stop)
        self.app = create_app(self.settings, bot_provider=object())
        self.addCleanup(self._cleanup)

    def _fake_send(self, *args, **kwargs):
        with self.send_lock:
            self.sends.append(kwargs)
            return f"wamid.v-{uuid.uuid4().hex[:12]}"

    def _super(self):
        import psycopg

        return psycopg.connect(self.super_url, autocommit=True)

    def _cleanup(self):
        with self._super() as connection, connection.cursor() as cursor:
            cursor.execute("delete from public.handover_requests where wa_id = %s", (self.wa,))
            if self.message_ids:
                cursor.execute(
                    "delete from public.whatsapp_webhook_events where message_id = any(%s)",
                    (self.message_ids,),
                )
            cursor.execute(
                "delete from public.whatsapp_conversations where customer_key = %s", (self.wa,)
            )
            cursor.execute("delete from public.customers where wa_id = %s", (self.wa,))
            for extra in getattr(self, "extra_cleanup", []):
                cursor.execute(*extra)

    def _post(self, text, message_id=None, client=None):
        message_id = message_id or f"wamid.in-{uuid.uuid4().hex[:16]}"
        if message_id not in self.message_ids:
            self.message_ids.append(message_id)
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PHONE_ID},
            "messages": [{"id": message_id, "from": self.wa, "text": {"body": text}}],
        }}]}]}
        body = json.dumps(payload).encode()
        signature = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        return (client or self.app.test_client()).post(
            "/webhooks/whatsapp", data=body,
            headers={"Content-Type": "application/json",
                     "X-Hub-Signature-256": f"sha256={signature}"},
        )

    def _portal(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["t_user_id"] = user_id
            session["t_email"] = "member@example.test"
            session["t_exp"] = 2**40
            session["csrf_token"] = "tok"
        return client

    def _conversation(self):
        with self._super() as connection, connection.cursor() as cursor:
            cursor.execute(
                "select c.id from public.conversations c join public.customers cu "
                "on cu.id = c.customer_id where cu.wa_id = %s and c.tenant_id = %s",
                (self.wa, TENANT),
            )
            return str(cursor.fetchone()[0])

    def _state(self, conversation_id):
        return conversation_control.get_state(self.settings, TENANT, conversation_id)

    def _messages(self, conversation_id):
        with self._super() as connection, connection.cursor() as cursor:
            cursor.execute(
                "select direction, sender_type, body_text, provider_message_id, "
                "delivery_status, error_code, reply_to_provider_message_id "
                "from public.messages where conversation_id = %s order by received_at, id",
                (conversation_id,),
            )
            names = [c.name for c in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _event(self, message_id):
        with self._super() as connection, connection.cursor() as cursor:
            cursor.execute(
                "select status, error_code, response_message_id "
                "from public.whatsapp_webhook_events where message_id = %s",
                (message_id,),
            )
            return cursor.fetchone()

    def _to_customer(self):
        return [s for s in self.sends if s.get("recipient") == self.wa]

    def _ai_sends(self):
        return [s for s in self.sends if s.get("text") == "AI reply"]

    def _start(self):
        """A conversation with one ordinary AI exchange, AI active."""
        response = self._post("Hello")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conversation_id = self._conversation()
        self.assertEqual(self._state(conversation_id), ("ai_active", False))
        return conversation_id

    # ======================================================= 1. acknowledgement reliability
    def test_1a_ack_sent_but_finalisation_fails_is_logged_kept_and_never_resent(self):
        conversation_id = self._start()
        # Make the provider return a wamid that already exists in this channel: the
        # boundary's "mark sent" UPDATE then violates the unique index AFTER the send.
        taken = self._messages(conversation_id)[-1]["provider_message_id"]
        self.assertTrue(taken)
        self.sends.clear()
        original = self._fake_send

        def collide(*args, **kwargs):
            original(*args, **kwargs)
            return taken

        message_id = f"wamid.in-{uuid.uuid4().hex[:16]}"
        with patch("platform_admin.services.whatsapp.send_text", side_effect=collide), \
                self.assertLogs("platform_admin", level="ERROR") as logs:
            response = self._post("Can I talk to a human please", message_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["recorded"], False)
        self.assertEqual(len(self._to_customer()), 1)  # exactly one acknowledgement
        self.assertTrue(any("UNRECORDED OUTBOUND" in line and taken in line for line in logs.output))
        # Reconciliation keys: the event keeps the wamid and a specific code; the intent
        # row exists without a wamid, which the inbox shows as "Outcome unknown".
        self.assertEqual(self._event(message_id), ("failed", "ack_sent_unrecorded", taken))
        ack = [m for m in self._messages(conversation_id) if m["sender_type"] == "system"]
        self.assertEqual(len(ack), 1)
        self.assertIsNone(ack[0]["provider_message_id"])
        self.assertEqual(ack[0]["reply_to_provider_message_id"], message_id)
        self.assertEqual(self._state(conversation_id), ("paused", True))  # still orange
        # Meta redelivers the same message: nothing is sent again.
        retry = self._post("Can I talk to a human please", message_id)
        self.assertEqual(retry.json, {"ok": True, "duplicate": True})
        self.assertEqual(len(self._to_customer()), 1)

    def test_1b_intent_row_unavailable_ack_is_recorded_after_the_send(self):
        conversation_id = self._start()
        from platform_admin.services import customers as customer_service

        real_save = customer_service.save_message
        calls = {"n": 0}

        def flaky_save(*args, **kwargs):
            calls["n"] += 1
            if kwargs.get("sender_type") == "system" and kwargs.get("reply_to_provider_message_id"):
                raise RuntimeError("simulated intent-row failure")
            return real_save(*args, **kwargs)

        self.sends.clear()
        message_id = f"wamid.in-{uuid.uuid4().hex[:16]}"
        with patch("platform_admin.routes.customer_service.save_message", side_effect=flaky_save), \
                self.assertLogs("platform_admin", level="ERROR") as logs:
            response = self._post("I want to speak to a person", message_id)
        self.assertTrue(response.json["recorded"])
        self.assertTrue(any("intent not recorded" in line for line in logs.output))
        self.assertEqual(len(self._to_customer()), 1)
        ack = [m for m in self._messages(conversation_id) if m["sender_type"] == "system"]
        self.assertEqual(len(ack), 1)
        self.assertTrue(ack[0]["provider_message_id"])  # recorded post-send with its wamid
        self.assertEqual(self._event(message_id)[0:2], ("processed", None))

    def test_1c_ack_sent_and_every_write_fails_leaves_the_wamid_on_the_event(self):
        conversation_id = self._start()
        from platform_admin.services import customers as customer_service

        real_save = customer_service.save_message

        def failing_save(*args, **kwargs):
            if kwargs.get("sender_type") == "system":
                raise RuntimeError("simulated persistence failure")
            return real_save(*args, **kwargs)

        self.sends.clear()
        message_id = f"wamid.in-{uuid.uuid4().hex[:16]}"
        with patch("platform_admin.routes.customer_service.save_message", side_effect=failing_save), \
                self.assertLogs("platform_admin", level="ERROR") as logs:
            response = self._post("human please", message_id)
        self.assertFalse(response.json["recorded"])
        self.assertTrue(any("UNRECORDED OUTBOUND" in line for line in logs.output))
        status, code, wamid = self._event(message_id)
        self.assertEqual((status, code), ("processed", "ack_sent_unrecorded"))
        self.assertTrue(wamid and wamid.startswith("wamid.v-"))
        self.assertEqual(len(self._to_customer()), 1)
        self.assertEqual(self._post("human please", message_id).json["duplicate"], True)
        self.assertEqual(len(self._to_customer()), 1)
        self.assertEqual(self._state(conversation_id), ("paused", True))

    def test_1d_redelivered_human_request_gets_one_acknowledgement(self):
        conversation_id = self._start()
        self.sends.clear()
        message_id = f"wamid.in-{uuid.uuid4().hex[:16]}"
        first = self._post("Can I speak to someone from your team?", message_id)
        second = self._post("Can I speak to someone from your team?", message_id)
        self.assertEqual(first.json["delivery"], "sent")
        self.assertEqual(second.json, {"ok": True, "duplicate": True})
        acks = [s for s in self.sends if s.get("recipient") == self.wa]
        self.assertEqual(len(acks), 1)
        rows = [m for m in self._messages(conversation_id) if m["sender_type"] == "system"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delivery_status"], "sent")
        with self._super() as connection, connection.cursor() as cursor:
            cursor.execute("select count(*) from public.handover_requests where wa_id = %s",
                           (self.wa,))
            self.assertEqual(cursor.fetchone()[0], 1)

    # ======================================================= 2. restart persistence
    def test_2_pause_survives_an_application_restart(self):
        env = dict(os.environ)

        def probe(phase, message_id="-"):
            result = subprocess.run(
                [sys.executable, str(REPO / "tests" / "stage2_restart_probe.py"),
                 phase, self.wa, message_id],
                cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120,
            )
            line = [l for l in result.stdout.splitlines() if l.startswith("PROBE_RESULT ")]
            self.assertTrue(line, result.stdout + result.stderr)
            return json.loads(line[0][len("PROBE_RESULT "):])

        first_id = f"wamid.in-{uuid.uuid4().hex[:16]}"
        second_id = f"wamid.in-{uuid.uuid4().hex[:16]}"
        self.message_ids += [first_id, second_id]
        # Process 1: a customer message, answered by the AI.
        first = probe("customer", first_id)
        self.assertEqual((first["status"], first["engine_calls"], first["sends"]), (200, 1, 1))
        # Process 2: the owner pauses AI through the portal.
        self.assertEqual(probe("pause")["status"], 302)
        # Process 3 (fresh interpreter = restarted app): another customer message.
        second = probe("customer", second_id)
        self.assertEqual(second["status"], 200)
        self.assertEqual(second["json"].get("stage"), "before_generation")
        self.assertEqual((second["engine_calls"], second["sends"]), (0, 0))
        # Process 4: the inbox shows the human-handling state and the new message.
        inbox = probe("inbox")
        self.assertEqual(inbox, {"status": 200, "red_state": True, "label": True,
                                 "resume_offered": True, "message_shown": True})
        conversation_id = self._conversation()
        self.assertEqual(self._state(conversation_id), ("paused", False))
        with self._super() as connection, connection.cursor() as cursor:
            cursor.execute(
                "select metadata->>'stage' from public.conversation_control_events "
                "where conversation_id = %s and event_type = 'ai_reply_suppressed'",
                (conversation_id,),
            )
            self.assertEqual([r[0] for r in cursor.fetchall()], ["before_generation"])

    # ======================================================= 3. concurrency
    def _wait_for_lock_waiter(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        with self._super() as connection, connection.cursor() as cursor:
            while time.monotonic() < deadline:
                cursor.execute(
                    "select count(*) from pg_stat_activity "
                    "where datname = current_database() and wait_event_type = 'Lock'"
                )
                if cursor.fetchone()[0]:
                    return True
                time.sleep(0.05)
        return False

    def test_3a_customer_message_during_an_uncommitted_takeover_gets_no_ai_reply(self):
        conversation_id = self._start()
        self.sends.clear()
        _Engine.calls = 0
        import psycopg

        takeover = psycopg.connect(self.super_url)  # staff takeover transaction, open
        cursor = takeover.cursor()
        cursor.execute(
            "select ai_mode from public.conversations where id = %s for update",
            (conversation_id,),
        )
        cursor.execute(
            "update public.conversations set ai_mode = 'human' where id = %s",
            (conversation_id,),
        )
        result = {}
        worker = threading.Thread(
            target=lambda: result.setdefault("response", self._post("Is it ready?"))
        )
        worker.start()
        self.assertTrue(self._wait_for_lock_waiter(), "webhook never blocked on the takeover")
        takeover.commit()  # takeover lands while the webhook is mid-flight
        takeover.close()
        worker.join(30)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["response"].status_code, 200)
        self.assertEqual(self._ai_sends(), [])
        self.assertEqual(_Engine.calls, 0)  # blocked before the gate, then saw 'human'
        self.assertEqual(self._state(conversation_id)[0], "human")

    def test_3b_ai_reply_still_generating_when_staff_take_over_is_never_sent(self):
        conversation_id = self._start()
        self.sends.clear()
        generating = threading.Event()
        release = threading.Event()

        def slow_model(*args, **kwargs):
            generating.set()
            release.wait(15)
            return _reply()

        _Engine.impl = staticmethod(slow_model)
        result = {}
        message_id = f"wamid.in-{uuid.uuid4().hex[:16]}"
        worker = threading.Thread(
            target=lambda: result.setdefault("response", self._post("Price for 2kg?", message_id))
        )
        worker.start()
        self.assertTrue(generating.wait(15))
        # Staff press "Take over" in the portal while the model is still working.
        client = self._portal(STAFF)
        takeover = client.post(f"/t/{TENANT}/inbox/{conversation_id}/mode",
                               data={"mode": "human", "csrf_token": "tok"})
        self.assertEqual(takeover.status_code, 302)
        release.set()
        worker.join(30)
        self.assertEqual(result["response"].json, {"ok": True, "suppressed": True})
        self.assertEqual(self._ai_sends(), [])
        queued = [m for m in self._messages(conversation_id)
                  if m["sender_type"] == "assistant" and m["reply_to_provider_message_id"] == message_id]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["error_code"], "ai_reply_suppressed")
        self.assertIsNone(queued[0]["provider_message_id"])
        self.assertEqual(self._event(message_id)[0:2], ("failed", "ai_reply_suppressed"))

    def test_3c_takeover_waits_for_a_send_already_in_progress(self):
        """The only AI message that can go out is one whose send began BEFORE the
        takeover committed: the takeover waits on the dispatch lock."""
        conversation_id = self._start()
        self.sends.clear()
        in_send = threading.Event()
        timeline = {}
        original = self._fake_send

        def slow_send(*args, **kwargs):
            in_send.set()
            time.sleep(1.0)
            wamid = original(*args, **kwargs)
            timeline["send_done"] = time.monotonic()
            return wamid

        with patch("platform_admin.services.whatsapp.send_text", side_effect=slow_send):
            worker = threading.Thread(target=lambda: self._post("Hi again"))
            worker.start()
            self.assertTrue(in_send.wait(15))
            conversation_control.set_mode(
                self.settings, actor_user_id=STAFF, tenant_id=TENANT,
                conversation_id=conversation_id, new_mode="human",
            )
            timeline["takeover_committed"] = time.monotonic()
            worker.join(30)
        self.assertEqual(len(self._ai_sends()), 1)
        self.assertLess(timeline["send_done"], timeline["takeover_committed"])
        # After the takeover, nothing further is generated or sent.
        _Engine.calls = 0
        self.sends.clear()
        self._post("Anyone there?")
        self.assertEqual((_Engine.calls, self.sends), (0, []))

    # ======================================================= 4. staff messaging
    def test_4_staff_reply_is_persisted_sent_and_never_resumes_ai(self):
        conversation_id = self._start()
        self._post("Can I talk to a human?")
        self.assertEqual(self._state(conversation_id), ("paused", True))  # orange
        self.sends.clear()
        client = self._portal(STAFF)
        response = client.post(f"/t/{TENANT}/inbox/{conversation_id}/reply",
                               data={"text": "Hi, this is Grace from the bakery.",
                                     "csrf_token": "tok"})
        self.assertEqual(response.status_code, 302)
        # Sent once through the existing adapter, to the customer, on the tenant's number.
        self.assertEqual(len(self.sends), 1)
        sent = self.sends[0]
        self.assertEqual((sent["recipient"], sent["phone_number_id"], sent["tenant_id"]),
                         (self.wa, PHONE_ID, TENANT))
        self.assertEqual(sent["text"], "Hi, this is Grace from the bakery.")
        staff_rows = [m for m in self._messages(conversation_id) if m["sender_type"] == "staff"]
        self.assertEqual(len(staff_rows), 1)
        self.assertEqual(staff_rows[0]["direction"], "outbound")
        self.assertEqual(staff_rows[0]["delivery_status"], "sent")
        self.assertTrue(staff_rows[0]["provider_message_id"].startswith("wamid.v-"))
        self.assertEqual(self._state(conversation_id), ("human", False))  # red
        # The customer answers: still no AI.
        _Engine.calls = 0
        self.sends.clear()
        self._post("Thanks Grace")
        self.assertEqual((_Engine.calls, self.sends), (0, []))
        self.assertEqual(self._state(conversation_id), ("human", False))
        # A second staff reply keeps it human.
        client.post(f"/t/{TENANT}/inbox/{conversation_id}/reply",
                    data={"text": "Your cake is ready.", "csrf_token": "tok"})
        self.assertEqual(self._state(conversation_id)[0], "human")
        # The inbox shows what the database says.
        page = client.get(f"/t/{TENANT}/inbox/{conversation_id}").get_data(as_text=True)
        self.assertIn("state-pill state-red", page)
        self.assertIn("Your cake is ready.", page)
        self.assertIn("Thanks Grace", page)
        self.assertNotIn("state-pill state-green", page)
        listing = client.get(f"/t/{TENANT}/inbox?view=human").get_data(as_text=True)
        self.assertIn(conversation_id, listing)
        # Only the explicit, confirmed Resume brings the AI back.
        client.post(f"/t/{TENANT}/inbox/{conversation_id}/mode",
                    data={"mode": "ai_active", "confirm": "yes", "csrf_token": "tok"})
        self.assertEqual(self._state(conversation_id), ("ai_active", False))

    # ======================================================= 5. security
    def test_5a_other_tenants_and_outsiders_cannot_read_or_act(self):
        conversation_id = self._start()
        self.sends.clear()
        before = self._state(conversation_id)
        targets = (
            ("get", f"/t/{TENANT}/inbox/{conversation_id}", {}),
            ("get", f"/t/{TENANT}/inbox/updates?c={conversation_id}", {}),
            ("post", f"/t/{TENANT}/inbox/{conversation_id}/reply", {"text": "x"}),
            ("post", f"/t/{TENANT}/inbox/{conversation_id}/mode",
             {"mode": "ai_active", "confirm": "yes"}),
            ("post", f"/t/{TENANT}/inbox/{conversation_id}/mode", {"mode": "human"}),
            ("post", f"/t/{TENANT}/inbox/{conversation_id}/attention", {"value": "1"}),
        )
        for user in (OTHER_OWNER, OUTSIDER):
            client = self._portal(user)
            for method, url, data in targets:
                with self.subTest(user=user, url=url, method=method):
                    response = getattr(client, method)(url, data={**data, "csrf_token": "tok"})
                    self.assertIn(response.status_code, (302, 403, 404))
                    self.assertNotIn("Hello", response.get_data(as_text=True))
        # Another tenant's own URL with this tenant's conversation id: 404, never data.
        client = self._portal(OTHER_OWNER)
        for method, url, data in (
            ("get", f"/t/{OTHER_TENANT}/inbox/{conversation_id}", {}),
            ("post", f"/t/{OTHER_TENANT}/inbox/{conversation_id}/reply", {"text": "x"}),
            ("post", f"/t/{OTHER_TENANT}/inbox/{conversation_id}/mode", {"mode": "human"}),
            ("post", f"/t/{OTHER_TENANT}/inbox/{conversation_id}/mode",
             {"mode": "ai_active", "confirm": "yes"}),
        ):
            with self.subTest(cross=url, method=method):
                response = getattr(client, method)(url, data={**data, "csrf_token": "tok"})
                self.assertEqual(response.status_code, 404)
        other_inbox = client.get(f"/t/{OTHER_TENANT}/inbox").get_data(as_text=True)
        self.assertNotIn(conversation_id, other_inbox)
        self.assertNotIn(self.wa, other_inbox)
        self.assertEqual(self.sends, [])
        self.assertEqual(self._state(conversation_id), before)

    def test_5b_unauthenticated_and_csrf_less_requests_change_nothing(self):
        conversation_id = self._start()
        self.sends.clear()
        anonymous = self.app.test_client()
        for url, data in (
            (f"/t/{TENANT}/inbox/{conversation_id}/reply", {"text": "x"}),
            (f"/t/{TENANT}/inbox/{conversation_id}/mode", {"mode": "human"}),
        ):
            self.assertIn(anonymous.post(url, data=data).status_code, (302, 400, 401, 403))
        self.assertIn(anonymous.get(f"/t/{TENANT}/inbox/updates").status_code, (302, 401, 403))
        member = self._portal(STAFF)
        no_token = member.post(f"/t/{TENANT}/inbox/{conversation_id}/reply", data={"text": "x"})
        self.assertEqual(no_token.status_code, 400)
        self.assertEqual(self.sends, [])
        self.assertEqual(self._state(conversation_id), ("ai_active", False))

    def test_5c_owner_and_staff_members_may_reply_and_resume(self):
        conversation_id = self._start()
        for user in (OWNER, STAFF):
            with self.subTest(user=user):
                client = self._portal(user)
                reply = client.post(f"/t/{TENANT}/inbox/{conversation_id}/reply",
                                    data={"text": "Hello from the team", "csrf_token": "tok"})
                self.assertEqual(reply.status_code, 302)
                self.assertEqual(self._state(conversation_id)[0], "human")
                resume = client.post(f"/t/{TENANT}/inbox/{conversation_id}/mode",
                                     data={"mode": "ai_active", "confirm": "yes",
                                           "csrf_token": "tok"})
                self.assertEqual(resume.status_code, 302)
                self.assertEqual(self._state(conversation_id)[0], "ai_active")

    def test_5d_quotations_of_another_tenant_are_not_reachable(self):
        from saas import quotations as saas_quotations

        with self._super() as connection, connection.cursor() as cursor:
            cursor.execute(
                "select id from public.product_variants where tenant_id = %s "
                "and is_active order by id limit 1", (TENANT,),
            )
            row = cursor.fetchone()
        if row is None:
            self.skipTest("no seeded catalogue variant")
        quotation = saas_quotations.create_quotation(
            self.settings, TENANT, customer_name="Verification Customer",
            selections=[(str(row[0]), 1)], source="dashboard",
            idempotency_key=f"verify-{uuid.uuid4().hex}",
        )
        quotation_id = str(quotation["id"])
        self.extra_cleanup = [
            ("delete from public.audit_log where tenant_id = %s and created_at >= %s",
             (TENANT, self.started)),
            ("delete from public.quotations where id = %s", (quotation_id,)),
        ]
        own = self._portal(OWNER).get(f"/t/{TENANT}/quotations/{quotation_id}")
        self.assertEqual(own.status_code, 200)
        other = self._portal(OTHER_OWNER)
        self.assertEqual(other.get(f"/t/{OTHER_TENANT}/quotations/{quotation_id}").status_code, 404)
        self.assertIn(other.get(f"/t/{TENANT}/quotations/{quotation_id}").status_code, (302, 403, 404))
        listing = other.get(f"/t/{OTHER_TENANT}/quotations").get_data(as_text=True)
        self.assertNotIn("Verification Customer", listing)
        outsider = self._portal(OUTSIDER).get(f"/t/{TENANT}/quotations")
        self.assertIn(outsider.status_code, (302, 403, 404))


if __name__ == "__main__":
    unittest.main()
