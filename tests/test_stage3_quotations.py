"""Stage 3 (Quotations) end-to-end tests against a real database.

SKIPPED unless SAAS_TEST_DATABASE_URL points at a disposable database with migrations
(including 0044) and the dev seed applied -- never staging or production. Routes,
services, the dispatch boundary, RLS and the database are real; only the WhatsApp HTTP
calls (send_text / upload_media / send_document) and the AI model are faked. Every row a
test creates is removed afterwards with the disposable database's superuser, and the
tenant configuration / catalogue rows a test changes are restored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from platform_admin.app import create_app
from platform_admin.config import Settings
from platform_admin.services import conversation_control
from platform_admin.services import whatsapp as whatsapp_service
from platform_admin.services.validation import ServiceError
from saas import output_safety
from saas import quotation_intent as intent

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


def _ai_reply(text="AI reply"):
    return SimpleNamespace(
        text=text, quotation=None, quotation_failed=False, quotation_pdf=None,
        handover=None, handover_failed=False, output_blocked=None,
    )


class _Engine:
    """Replaces BotResponseEngine where a test only needs to know if the AI ran."""

    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    def respond(self, *args, **kwargs):
        type(self).calls += 1
        return _ai_reply()


class _Provider:
    """The model, for the strict extraction call and for real-engine tests."""

    def __init__(self):
        self.replies: list[str] = []
        self.requests: list = []

    def generate(self, request):
        self.requests.append(request)
        return self.replies.pop(0) if self.replies else "OK"


_DEFAULT = object()


class Stage3Base(unittest.TestCase):
    send_enabled = True
    real_engine = False
    profile_name: str | None = "Asha Test"

    @classmethod
    def setUpClass(cls):
        try:
            cls.super_url = support.require_test_database_url()
        except Exception as error:  # noqa: BLE001 - skip without a disposable database
            raise unittest.SkipTest(str(error))
        cls.app_url = support.app_backend_test_url()

    # ------------------------------------------------------------------ fixtures
    def setUp(self):
        self.settings = Settings(
            secret_key="s" * 40,
            cookie_secure=False,
            app_database_url=self.app_url,
            whatsapp_app_secret="app-secret",
            whatsapp_send_enabled=self.send_enabled,
        )
        self.wa = f"+2547{uuid.uuid4().int % 10**8:08d}"
        self.message_ids: list[str] = []
        self.sends: list[dict] = []
        self.uploads: list[dict] = []
        self.documents: list[dict] = []
        self.restore: list[tuple] = []
        _Engine.calls = 0
        self.provider = _Provider()
        stubs = [
            ("platform_admin.services.whatsapp.send_text", {"side_effect": self._fake_send}),
            ("platform_admin.services.whatsapp.upload_media", {"side_effect": self._fake_upload}),
            ("platform_admin.services.whatsapp.send_document",
             {"side_effect": self._fake_document}),
        ]
        if not self.real_engine:
            stubs.append(("platform_admin.routes.BotResponseEngine", {"new": _Engine}))
        for target, kwargs in stubs:
            stub = patch(target, **kwargs)
            stub.start()
            self.addCleanup(stub.stop)
        self.app = create_app(self.settings, bot_provider=self.provider)
        self.addCleanup(self._cleanup)
        self._enable_quotations()

    def _fake_send(self, *args, **kwargs):
        if not self.settings.whatsapp_send_enabled:  # mirrors the real _post gate
            raise ServiceError("WhatsApp sending is disabled in this environment.", 503)
        self.sends.append(kwargs)
        return f"wamid.t-{uuid.uuid4().hex[:12]}"

    def _fake_upload(self, *args, **kwargs):
        self.uploads.append(kwargs)
        return f"media-{uuid.uuid4().hex[:8]}"

    def _fake_document(self, *args, **kwargs):
        self.documents.append(kwargs)
        return f"wamid.d-{uuid.uuid4().hex[:12]}"

    def _super(self):
        import psycopg

        return psycopg.connect(self.super_url, autocommit=True)

    def _sql(self, sql, params=()):
        with self._super() as connection, connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else []

    def _enable_quotations(self, **extra):
        original = self._sql(
            "select configuration from public.tenant_settings where tenant_id = %s", (TENANT,)
        )[0][0]
        self.restore.append(
            ("update public.tenant_settings set configuration = %s::jsonb where tenant_id = %s",
             (json.dumps(original), TENANT))
        )
        updated = {**original, "quotations": {"enabled": True}, **extra}
        self._sql(
            "update public.tenant_settings set configuration = %s::jsonb where tenant_id = %s",
            (json.dumps(updated), TENANT),
        )

    def _cleanup(self):
        quotation_ids = [row[0] for row in self._sql(
            "select id::text from public.quotations where customer_wa_id = %s", (self.wa,))]
        if quotation_ids:
            self._sql("delete from public.audit_log where entity_id = any(%s)",
                      (quotation_ids,))
            self._sql("delete from public.quotations where id = any(%s::uuid[])",
                      (quotation_ids,))
        self._sql("delete from public.handover_requests where wa_id = %s", (self.wa,))
        if self.message_ids:
            self._sql("delete from public.whatsapp_webhook_events where message_id = any(%s)",
                      (self.message_ids,))
        self._sql("delete from public.whatsapp_conversations where customer_key = %s",
                  (self.wa,))
        self._sql("delete from public.customers where wa_id = %s", (self.wa,))
        for sql, params in reversed(self.restore):
            self._sql(sql, params)

    # ------------------------------------------------------------------ helpers
    def _post(self, text, message_id=None, profile_name=_DEFAULT):
        if profile_name is _DEFAULT:
            profile_name = self.profile_name
        message_id = message_id or f"wamid.in-{uuid.uuid4().hex[:16]}"
        if message_id not in self.message_ids:
            self.message_ids.append(message_id)
        value = {
            "metadata": {"phone_number_id": PHONE_ID},
            "messages": [{"id": message_id, "from": self.wa, "text": {"body": text}}],
        }
        if profile_name:
            value["contacts"] = [{"wa_id": self.wa, "profile": {"name": profile_name}}]
        payload = {"object": "whatsapp_business_account",
                   "entry": [{"changes": [{"value": value}]}]}
        body = json.dumps(payload).encode()
        signature = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        response = self.app.test_client().post(
            "/webhooks/whatsapp", data=body,
            headers={"Content-Type": "application/json",
                     "X-Hub-Signature-256": f"sha256={signature}"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response

    def _portal(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["t_user_id"] = user_id
            session["t_email"] = "member@example.test"
            session["t_exp"] = 2**40
            session["csrf_token"] = "tok"
        return client

    def _conversation(self):
        rows = self._sql(
            "select c.id::text from public.conversations c join public.customers cu "
            "on cu.id = c.customer_id where cu.wa_id = %s and c.tenant_id = %s",
            (self.wa, TENANT),
        )
        return rows[0][0] if rows else None

    def _texts(self):
        return [s.get("text") for s in self.sends if s.get("recipient") == self.wa]

    def _quotations(self):
        return self._sql(
            "select id::text, quotation_number, customer_name, subtotal, total "
            "from public.quotations where customer_wa_id = %s order by created_at", (self.wa,))

    def _lines(self, quotation_id):
        return self._sql(
            "select description, quantity, unit_price, subtotal from public.quotation_lines "
            "where quotation_id = %s order by id", (quotation_id,))

    def _deliveries(self, quotation_id):
        return self._sql(
            "select delivery_status, error_code, provider_message_id "
            "from public.quotation_deliveries where quotation_id = %s order by created_at",
            (quotation_id,))

    def _document(self, quotation_id):
        rows = self._sql(
            "select content, sha256, size_bytes from public.quotation_documents "
            "where quotation_id = %s", (quotation_id,))
        return rows[0] if rows else None

    def _brief(self):
        conversation = self._conversation()
        rows = self._sql(
            "select status, brief, missing_fields from public.quotation_briefs "
            "where conversation_id = %s", (conversation,)) if conversation else []
        return rows[0] if rows else None

    def _price(self, product, weight):
        return self._sql(
            "select v.price from public.product_variants v join public.products p "
            "on p.id = v.product_id where p.tenant_id = %s and p.name = %s "
            "and (v.attributes->>'weight_kg')::int = %s", (TENANT, product, weight))[0][0]

    def _variant(self, product, weight):
        return self._sql(
            "select v.id::text, v.price, v.availability from public.product_variants v "
            "join public.products p on p.id = v.product_id where p.tenant_id = %s "
            "and p.name = %s and (v.attributes->>'weight_kg')::int = %s",
            (TENANT, product, weight))[0]

    def _quote_now(self, text="Please send me a quote for 2 Vanilla cakes, 1kg each",
                   name="Asha Test"):
        self._post(text, profile_name=name)
        quotations = self._quotations()
        self.assertEqual(len(quotations), 1, self._texts())
        return quotations[0]


# =========================================================================== workflow
class QuotationWorkflowTests(Stage3Base):
    def test_complete_whatsapp_to_pdf_to_dashboard_flow(self):
        self.profile_name = None  # no WhatsApp profile name: the name is asked for
        self._post("Hi! Could I get a quotation for 2 chocolate cakes please?")
        self.assertIn("What size would you like for the Chocolate", self._texts()[-1])
        self.assertIn("1 kg", self._texts()[-1])
        self.assertEqual(self._brief()[0], "collecting")

        self._post("1kg each")
        self.assertEqual(self._texts()[-1], "Thanks. What name should I put on the quotation?")
        self.assertEqual(self._quotations(), [])

        self._post("Asha Wanjiru")
        quotations = self._quotations()
        self.assertEqual(len(quotations), 1)
        quotation_id, number, customer, subtotal, total = quotations[0]
        price = self._price("Chocolate", 1)
        self.assertEqual(customer, "Asha Wanjiru")
        self.assertEqual(Decimal(total), Decimal(price) * 2)
        self.assertRegex(number, r"^Q-\d{8}-[0-9A-F]{8}$")
        lines = self._lines(quotation_id)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][1], 2)
        self.assertEqual(Decimal(lines[0][2]), Decimal(price))

        summary = self._texts()[-1]
        self.assertIn(number, summary)
        self.assertIn("Total:", summary)
        self.assertIn("Delivery charges are calculated separately based on the delivery "
                      "location.", summary)

        # PDF: stored once, exactly what was uploaded and sent.
        content, sha, size = self._document(quotation_id)
        self.assertTrue(bytes(content).startswith(b"%PDF-"))
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.uploads[0]["content"], bytes(content))
        self.assertEqual(hashlib.sha256(bytes(content)).hexdigest(), sha)
        self.assertEqual(len(self.documents), 1)
        self.assertEqual(self.documents[0]["recipient"], self.wa)
        deliveries = self._deliveries(quotation_id)
        self.assertEqual([d[0] for d in deliveries], ["sent"])
        self.assertTrue(deliveries[0][2].startswith("wamid.d-"))
        # The document is in the inbox history with its WhatsApp id.
        rows = self._sql(
            "select message_type, delivery_status, provider_message_id from public.messages "
            "where conversation_id = %s and message_type = 'document'", (self._conversation(),))
        self.assertEqual(rows, [("document", "sent", deliveries[0][2])])
        # The model never ran for any of these three messages.
        self.assertEqual(_Engine.calls, 0)

        # Dashboard: list, detail, view and download for owner AND staff.
        for user in (OWNER, STAFF):
            client = self._portal(user)
            listing = client.get(f"/t/{TENANT}/quotations")
            self.assertEqual(listing.status_code, 200)
            html = listing.get_data(as_text=True)
            self.assertIn(number, html)
            self.assertIn("quote-sent", html)
            self.assertIn(self.wa, html)
            detail = client.get(f"/t/{TENANT}/quotations/{quotation_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("Download PDF", detail.get_data(as_text=True))
            view = client.get(f"/t/{TENANT}/quotations/{quotation_id}/pdf")
            self.assertEqual(view.status_code, 200)
            self.assertEqual(view.mimetype, "application/pdf")
            self.assertEqual(view.get_data(), bytes(content))
            self.assertIn("no-store", view.headers["Cache-Control"])
            self.assertNotIn("attachment", view.headers.get("Content-Disposition", ""))
            download = client.get(f"/t/{TENANT}/quotations/{quotation_id}/pdf?download=1")
            self.assertIn("attachment", download.headers["Content-Disposition"])
            self.assertIn(f"{number}.pdf", download.headers["Content-Disposition"])
            self.assertEqual(download.get_data(), bytes(content))

    def test_whatsapp_profile_name_completes_in_one_message(self):
        _, number, customer, _, total = self._quote_now(
            "Please send me a quote for one 2kg Red Velvet cake.", name="Brian Otieno")
        self.assertEqual(customer, "Brian Otieno")
        self.assertEqual(Decimal(total), Decimal(self._price("Red Velvet", 2)))
        self.assertEqual(len(self.documents), 1)

    def test_multiple_items_in_one_request(self):
        quotation_id, *_ = self._quote_now(
            "quote please: 2 vanilla 1kg and 3 chocolate 2kg")
        lines = self._lines(quotation_id)
        self.assertEqual([line[1] for line in lines], [2, 3])
        expected = Decimal(self._price("Vanilla", 1)) * 2 + Decimal(self._price("Chocolate", 2)) * 3
        self.assertEqual(sum(Decimal(line[3]) for line in lines), expected)

    def test_missing_information_is_asked_one_question_at_a_time_and_corrections_apply(self):
        self._post("Can I get a quotation?", profile_name="Asha Test")
        self.assertIn("Which product would you like a quotation for", self._texts()[-1])
        self._post("Vanilla")
        self.assertIn("What size would you like for the Vanilla", self._texts()[-1])
        self._post("2kg")
        self.assertEqual(self._texts()[-1], "How many Vanilla would you like?")
        self.assertEqual(self._quotations(), [])
        self._post("make it 3kg")  # a correction to the open request, not a new one
        self.assertEqual(self._texts()[-1], "How many Vanilla would you like?")
        self._post("2")
        quotation_id = self._quotations()[0][0]
        lines = self._lines(quotation_id)
        self.assertEqual(lines[0][1], 2)
        self.assertEqual(Decimal(lines[0][2]), Decimal(self._price("Vanilla", 3)))

    def test_unclear_size_is_asked_not_guessed(self):
        self._post("quote for half a kilo vanilla cake", profile_name="Asha Test")
        self.assertIn("size", self._texts()[-1].lower())
        self.assertEqual(self._quotations(), [])

    def test_unrelated_message_during_a_request_goes_to_the_ai_and_request_stays_open(self):
        self._post("Can I get a quote for vanilla?", profile_name="Asha Test")
        self.assertEqual(_Engine.calls, 0)
        self._post("What are your opening hours?")
        self.assertEqual(_Engine.calls, 1)
        self.assertEqual(self._brief()[0], "collecting")
        self._post("1kg, 2 cakes")
        self.assertEqual(len(self._quotations()), 1)

    def test_price_enquiry_is_answered_by_the_ai_and_creates_no_quotation(self):
        self._post("How much is a 1kg vanilla cake?", profile_name="Asha Test")
        self.assertEqual(_Engine.calls, 1)
        self.assertEqual(self._quotations(), [])
        self.assertIsNone(self._brief())
        self.assertEqual(self.uploads, [])

    def test_document_request_is_not_a_new_quotation(self):
        self.assertFalse(intent.is_quote_request("Please resend the quotation pdf"))
        self.assertEqual(intent.classify("Please resend the quotation pdf"), None)


# =========================================================================== idempotency
class DuplicateWebhookTests(Stage3Base):
    def test_replaying_every_message_creates_and_sends_nothing_twice(self):
        first = f"wamid.in-{uuid.uuid4().hex[:16]}"
        second = f"wamid.in-{uuid.uuid4().hex[:16]}"
        self._post("quote for 2 chocolate cakes", message_id=first, profile_name="Asha")
        self._post("quote for 2 chocolate cakes", message_id=first, profile_name="Asha")
        self.assertEqual(len(self._texts()), 1)
        self._post("1kg", message_id=second)
        self._post("1kg", message_id=second)
        self._post("1kg", message_id=second)
        self.assertEqual(len(self._quotations()), 1)
        self.assertEqual(len(self._texts()), 2)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(len(self.documents), 1)
        quotation_id = self._quotations()[0][0]
        self.assertEqual(len(self._deliveries(quotation_id)), 1)
        self.assertEqual(
            self._sql("select count(*) from public.quotation_documents where quotation_id = %s",
                      (quotation_id,))[0][0], 1)

    def test_creation_is_idempotent_on_the_inbound_message_even_without_the_claim(self):
        from saas import quotations as saas_quotations

        self._quote_now()
        conversation = self._conversation()
        message_id = self.message_ids[-1]
        saas_quotations.save_brief(
            self.settings, TENANT, conversation, self.wa,
            {"items": [{"product_name": "Vanilla", "weight_kg": 1, "quantity": 2}]},
            status="ready", missing=[])
        again = saas_quotations.create_quotation_from_brief(
            self.settings, TENANT, conversation_id=conversation, customer_wa_id=self.wa,
            customer_name="Asha Test", message_id=message_id)
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self._quotations()), 1)


# =========================================================================== catalogue data
class CatalogueDataTests(Stage3Base):
    def test_unlisted_product_goes_to_staff_and_is_never_priced(self):
        self.provider.replies = [json.dumps({"items": [], "unlisted": ["unicorn cake"]})]
        self._post("Can you quote a unicorn cake for me?", profile_name="Asha Test")
        self.assertEqual(self._quotations(), [])
        self.assertIn("passed your request to our team", self._texts()[-1])
        self.assertNotRegex(self._texts()[-1], r"KES|\d{3,}")
        handovers = self._sql(
            "select reason, status from public.handover_requests where wa_id = %s", (self.wa,))
        self.assertEqual(len(handovers), 1)
        self.assertEqual(handovers[0][0], "unlisted_product")
        # A product handover does not pause the AI.
        self.assertEqual(conversation_control.get_state(
            self.settings, TENANT, self._conversation()), ("ai_active", False))
        # The extraction call was the strict JSON prompt, not the customer assistant.
        self.assertEqual(len(self.provider.requests), 1)
        self.assertIn("Return ONLY one JSON object", self.provider.requests[0].system_prompt)

    def test_an_invented_product_name_from_extraction_is_treated_as_unlisted(self):
        self.provider.replies = [json.dumps(
            {"items": [{"product_name": "Galaxy Supreme", "weight_kg": 2, "quantity": 1}],
             "unlisted": []})]
        self._post("could you quote the galaxy one?", profile_name="Asha Test")
        self.assertEqual(self._quotations(), [])
        self.assertEqual(len(self._sql(
            "select 1 from public.handover_requests where wa_id = %s", (self.wa,))), 1)

    def test_malformed_extraction_output_asks_instead_of_guessing(self):
        self.provider.replies = ["Sure! I think they want chocolate."]
        self._post("could you quote the thing I mentioned yesterday?", profile_name="Asha")
        self.assertEqual(self._quotations(), [])
        self.assertIn("Which product would you like a quotation for", self._texts()[-1])

    def test_out_of_stock_variant_is_reported_and_the_request_stays_open(self):
        variant_id, _price, availability = self._variant("Carrot", 2)
        self.restore.append(("update public.product_variants set availability = %s "
                             "where id = %s", (availability, variant_id)))
        self._sql("update public.product_variants set availability = 'OUT_OF_STOCK' "
                  "where id = %s", (variant_id,))
        self._post("quote for one 2kg carrot cake", profile_name="Asha Test")
        self.assertEqual(self._quotations(), [])
        self.assertIn("out of stock", self._texts()[-1])
        self.assertEqual(self._brief()[0], "collecting")
        self._post("1kg then")
        self.assertEqual(len(self._quotations()), 1)

    def test_catalogue_price_change_never_alters_an_issued_quotation(self):
        quotation_id, number, _, _, total = self._quote_now()
        before_pdf = bytes(self._document(quotation_id)[0])
        variant_id, price, _ = self._variant("Vanilla", 1)
        self.restore.append(("update public.product_variants set price = %s where id = %s",
                             (price, variant_id)))
        self.restore.append(("update public.products set name = 'Vanilla' "
                             "where tenant_id = %s and name = 'Vanilla Renamed'", (TENANT,)))
        self._sql("update public.product_variants set price = price + 999 where id = %s",
                  (variant_id,))
        self._sql("update public.products set name = 'Vanilla Renamed' "
                  "where tenant_id = %s and name = 'Vanilla'", (TENANT,))
        self.assertEqual(self._quotations()[0][4], total)
        self.assertEqual(Decimal(self._lines(quotation_id)[0][2]), Decimal(price))
        self.assertIn("Vanilla", self._lines(quotation_id)[0][0])
        client = self._portal(OWNER)
        self.assertEqual(
            client.get(f"/t/{TENANT}/quotations/{quotation_id}/pdf").get_data(), before_pdf)
        detail = client.get(f"/t/{TENANT}/quotations/{quotation_id}").get_data(as_text=True)
        self.assertIn(f"{Decimal(price):,.2f}", detail)
        self.assertNotIn("Vanilla Renamed", detail)

    def test_another_tenants_product_is_never_matched_or_priced(self):
        product_id = self._sql(
            "insert into public.products (tenant_id, name) values (%s, 'Zebra Mousse') "
            "returning id::text", (OTHER_TENANT,))[0][0]
        self.restore.append(("delete from public.products where id = %s", (product_id,)))
        self._sql(
            "insert into public.product_variants (tenant_id, product_id, code, name, price, "
            "sku, attributes) values (%s, %s, '1kg', '1kg', 10, %s, '{\"weight_kg\": 1}')",
            (OTHER_TENANT, product_id, f"ZM-{uuid.uuid4().hex[:6]}"))
        self.provider.replies = [json.dumps({"items": [], "unlisted": ["Zebra Mousse"]})]
        self._post("quote for 1kg zebra mousse", profile_name="Asha Test")
        self.assertEqual(self._quotations(), [])
        self.assertNotIn("Zebra Mousse", self.provider.requests[0].system_prompt)


# =========================================================================== failures
class FailureTests(Stage3Base):
    def test_pdf_failure_keeps_the_quotation_and_records_not_sent(self):
        with patch("platform_admin.services.quotations.quotation_pdf",
                   side_effect=RuntimeError("renderer down")):
            quotation_id, number, *_ = self._quote_now()
        self.assertIsNone(self._document(quotation_id))
        self.assertEqual(self.uploads, [])
        self.assertEqual(self._deliveries(quotation_id), [("failed", "pdf_render_failed", None)])
        html = self._portal(OWNER).get(f"/t/{TENANT}/quotations").get_data(as_text=True)
        self.assertIn("quote-not_sent", html)
        # The renderer recovers: the dashboard can still produce and store the PDF.
        pdf = self._portal(OWNER).get(f"/t/{TENANT}/quotations/{quotation_id}/pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertIsNotNone(self._document(quotation_id))

    def test_definite_rejection_is_not_sent_and_staff_can_resend_once(self):
        rejected = whatsapp_service.WhatsAppSendError(
            "failed", 502, provider_code="131047", provider_message="Re-engagement",
            http_status=400, ambiguous=False)
        with patch("platform_admin.services.whatsapp.send_document", side_effect=rejected):
            quotation_id, *_ = self._quote_now()
        self.assertEqual(self._deliveries(quotation_id)[0][:2], ("failed", "131047"))
        doc_rows = self._sql(
            "select delivery_status, provider_message_id from public.messages "
            "where conversation_id = %s and message_type = 'document'", (self._conversation(),))
        self.assertEqual(doc_rows, [("failed", None)])
        client = self._portal(STAFF)
        detail = client.get(f"/t/{TENANT}/quotations/{quotation_id}").get_data(as_text=True)
        self.assertIn("Resend PDF on WhatsApp", detail)
        key = re.search(r'name="resend_key" value="([0-9a-f-]{36})"', detail).group(1)
        response = client.post(f"/t/{TENANT}/quotations/{quotation_id}/resend",
                               data={"csrf_token": "tok", "resend_key": key})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.documents), 1)
        statuses = [d[0] for d in self._deliveries(quotation_id)]
        self.assertEqual(statuses, ["failed", "sent"])
        # The same form submitted again (double click) does nothing more.
        client.post(f"/t/{TENANT}/quotations/{quotation_id}/resend",
                    data={"csrf_token": "tok", "resend_key": key})
        self.assertEqual(len(self.documents), 1)
        html = client.get(f"/t/{TENANT}/quotations").get_data(as_text=True)
        self.assertIn("quote-sent", html)

    def test_ambiguous_send_is_never_marked_sent_or_resent(self):
        ambiguous = whatsapp_service.WhatsAppSendError(
            "failed", 502, provider_message="ReadTimeout", ambiguous=True)
        with patch("platform_admin.services.whatsapp.send_document", side_effect=ambiguous):
            quotation_id, *_ = self._quote_now()
        self.assertEqual(self._deliveries(quotation_id)[0][0], "uncertain")
        client = self._portal(OWNER)
        detail = client.get(f"/t/{TENANT}/quotations/{quotation_id}").get_data(as_text=True)
        self.assertNotIn("Resend PDF on WhatsApp", detail)
        self.assertIn("not resent automatically", detail)
        response = client.post(f"/t/{TENANT}/quotations/{quotation_id}/resend",
                               data={"csrf_token": "tok", "resend_key": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.documents, [])
        self.assertIn("quote-not_sent",
                      client.get(f"/t/{TENANT}/quotations").get_data(as_text=True))

    def test_upload_failure_is_a_definite_failure(self):
        rejected = whatsapp_service.WhatsAppSendError(
            "failed", 502, provider_code="100", provider_message="Invalid parameter",
            http_status=400, ambiguous=False)
        with patch("platform_admin.services.whatsapp.upload_media", side_effect=rejected):
            quotation_id, *_ = self._quote_now()
        self.assertEqual(self._deliveries(quotation_id)[0][:2], ("failed", "100"))
        self.assertEqual(self.documents, [])

    def test_summary_text_failure_never_marks_the_quotation_sent(self):
        def _reject(*args, **kwargs):
            raise whatsapp_service.WhatsAppSendError(
                "failed", 502, provider_code="131026", provider_message="Undeliverable",
                http_status=400, ambiguous=False)

        with patch("platform_admin.services.whatsapp.send_text", side_effect=_reject), \
                patch("platform_admin.services.whatsapp.send_document", side_effect=_reject):
            quotation_id, *_ = self._quote_now()
        self.assertEqual(self._deliveries(quotation_id)[0][0], "failed")
        event = self._sql("select status, error_code from public.whatsapp_webhook_events "
                          "where message_id = %s", (self.message_ids[-1],))[0]
        self.assertEqual(event[0], "failed")


class SendingDisabledTests(Stage3Base):
    send_enabled = False

    def test_quotation_is_generated_stored_and_shown_not_sent(self):
        quotation_id, number, *_ = self._quote_now()
        self.assertTrue(bytes(self._document(quotation_id)[0]).startswith(b"%PDF-"))
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.documents, [])
        self.assertEqual(self.sends, [])
        self.assertEqual(self._deliveries(quotation_id),
                         [("failed", "sending_disabled", None)])
        client = self._portal(OWNER)
        html = client.get(f"/t/{TENANT}/quotations").get_data(as_text=True)
        self.assertIn(number, html)
        self.assertIn("quote-not_sent", html)
        self.assertNotIn("quote-sent", html)
        detail = client.get(f"/t/{TENANT}/quotations/{quotation_id}").get_data(as_text=True)
        self.assertIn("Sending is disabled in this environment", detail)


# =========================================================================== Stage 2 controls
class Stage2ControlTests(Stage3Base):
    def test_paused_conversation_generates_nothing(self):
        self._post("Hello")
        conversation = self._conversation()
        conversation_control.set_mode(self.settings, tenant_id=TENANT,
                                      conversation_id=conversation, new_mode="paused",
                                      actor_user_id=OWNER)
        sent_before = len(self.sends)
        self._post("quote for 2 vanilla 1kg", profile_name="Asha Test")
        self.assertEqual(self._quotations(), [])
        self.assertIsNone(self._brief())
        self.assertEqual(len(self.sends), sent_before)
        event = self._sql("select error_code from public.whatsapp_webhook_events "
                          "where message_id = %s", (self.message_ids[-1],))[0][0]
        self.assertEqual(event, "ai_reply_suppressed")

    def test_discount_request_hands_over_and_pauses_the_ai(self):
        self._post("quote for 2 vanilla 1kg", profile_name="Asha Test")
        self.assertEqual(len(self._quotations()), 1)
        engine_before = _Engine.calls
        self._post("That's too expensive, can you give me a discount?")
        conversation = self._conversation()
        self.assertEqual(conversation_control.get_state(self.settings, TENANT, conversation),
                         ("paused", True))
        self.assertIn("Pricing changes are handled by our team", self._texts()[-1])
        handovers = self._sql("select reason, requested_product from public.handover_requests "
                              "where wa_id = %s", (self.wa,))
        self.assertEqual(len(handovers), 1)
        self.assertIn("Discount or special pricing", handovers[0][1])
        self.assertEqual(_Engine.calls, engine_before)
        sent_before = len(self.sends)
        self._post("ok so can you do 4000?")
        self.assertEqual(len(self.sends), sent_before)
        self.assertEqual(_Engine.calls, engine_before)
        self.assertEqual(len(self._quotations()), 1)

    def test_discount_classifier(self):
        for text in ("Can I get a discount?", "is that your best price", "can you do it for 3000",
                     "any promo code?", "too expensive", "naomba punguza bei"):
            self.assertEqual(intent.classify(text), intent.DISCOUNT, text)
        for text in ("How much is a 1kg cake?", "quote for 2 vanilla 1kg",
                     "do you deliver to Kilimani?"):
            self.assertNotEqual(intent.classify(text), intent.DISCOUNT, text)


class DeliveryEnquiryTests(Stage3Base):
    def test_delivery_enquiry_collects_the_location_then_hands_over(self):
        self._post("Do you deliver?", profile_name="Asha Test")
        self.assertIn("Where would you like your order delivered", self._texts()[-1])
        self.assertIn("calculated separately", self._texts()[-1])
        self.assertEqual(self._sql("select 1 from public.handover_requests where wa_id = %s",
                                   (self.wa,)), [])
        self._post("Kilimani, near Yaya Centre")
        self.assertIn("Kilimani", self._texts()[-1])
        self.assertNotRegex(self._texts()[-1], r"KES|\d{2,}")
        handovers = self._sql("select requested_product from public.handover_requests "
                              "where wa_id = %s", (self.wa,))
        self.assertEqual(handovers, [("Delivery charge to Kilimani, near Yaya Centre",)])
        self.assertEqual(conversation_control.get_state(
            self.settings, TENANT, self._conversation()), ("ai_active", False))
        self.assertEqual(_Engine.calls, 0)

    def test_delivery_enquiry_with_a_location_hands_over_directly(self):
        self._post("How much is delivery to Westlands?", profile_name="Asha Test")
        self.assertIn("Westlands", self._texts()[-1])
        self.assertEqual(self._sql("select requested_product from public.handover_requests "
                                   "where wa_id = %s", (self.wa,)),
                         [("Delivery charge to Westlands",)])


# =========================================================================== isolation
class IsolationAndPermissionTests(Stage3Base):
    def test_other_tenants_outsiders_and_anonymous_users_are_denied(self):
        quotation_id, number, *_ = self._quote_now()
        paths = [f"/t/{TENANT}/quotations", f"/t/{TENANT}/quotations/{quotation_id}",
                 f"/t/{TENANT}/quotations/{quotation_id}/pdf",
                 f"/t/{TENANT}/quotations/{quotation_id}/pdf?download=1"]
        for user in (OTHER_OWNER, OUTSIDER):
            client = self._portal(user)
            for path in paths:
                response = client.get(path)
                self.assertIn(response.status_code, (302, 403, 404), (user, path))
                self.assertNotIn(b"%PDF", response.get_data())
                self.assertNotIn(number.encode(), response.get_data())
            response = client.post(f"/t/{TENANT}/quotations/{quotation_id}/resend",
                                   data={"csrf_token": "tok", "resend_key": str(uuid.uuid4())})
            self.assertIn(response.status_code, (302, 403, 404))
        # Another tenant's own portal cannot reach it by id either.
        other = self._portal(OTHER_OWNER)
        for path in (f"/t/{OTHER_TENANT}/quotations/{quotation_id}",
                     f"/t/{OTHER_TENANT}/quotations/{quotation_id}/pdf"):
            self.assertEqual(other.get(path).status_code, 404)
        self.assertNotIn(number, other.get(f"/t/{OTHER_TENANT}/quotations")
                         .get_data(as_text=True))
        anonymous = self.app.test_client()
        for path in paths:
            response = anonymous.get(path)
            self.assertIn(response.status_code, (302, 401, 403))
            self.assertNotIn(b"%PDF", response.get_data())

    def test_resend_requires_csrf(self):
        rejected = whatsapp_service.WhatsAppSendError(
            "failed", 502, provider_code="131047", http_status=400, ambiguous=False)
        with patch("platform_admin.services.whatsapp.send_document", side_effect=rejected):
            quotation_id, *_ = self._quote_now()
        response = self._portal(OWNER).post(
            f"/t/{TENANT}/quotations/{quotation_id}/resend",
            data={"resend_key": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.documents, [])

    def test_documents_are_tenant_scoped_backend_only_and_immutable(self):
        import psycopg
        from saas.tenant_context import tenant_transaction

        quotation_id, *_ = self._quote_now()
        with tenant_transaction(OTHER_TENANT, self.app_url) as cursor:
            cursor.execute("select count(*) from public.quotation_documents "
                           "where quotation_id = %s", (quotation_id,))
            self.assertEqual(cursor.fetchone()[0], 0)
        with self.assertRaises(psycopg.Error):
            with tenant_transaction(OTHER_TENANT, self.app_url) as cursor:
                cursor.execute(
                    "insert into public.quotation_documents "
                    "(quotation_id, tenant_id, content, size_bytes, sha256) values "
                    "(%s, %s, '\\x255044462d'::bytea, 5, repeat('a', 64))",
                    (quotation_id, TENANT))
        with self.assertRaises(psycopg.Error):
            with tenant_transaction(TENANT, self.app_url) as cursor:
                cursor.execute("update public.quotation_documents set sha256 = repeat('b', 64) "
                               "where quotation_id = %s", (quotation_id,))
        with self.assertRaises(psycopg.Error):
            with tenant_transaction(TENANT, self.app_url) as cursor:
                cursor.execute("delete from public.quotation_documents where quotation_id = %s",
                               (quotation_id,))
        # Even the table owner cannot rewrite a stored document (trigger).
        with self.assertRaisesRegex(psycopg.Error, "immutable"):
            self._sql("update public.quotation_documents set sha256 = repeat('c', 64) "
                      "where quotation_id = %s", (quotation_id,))
        privileges = self._sql(
            "select r, p, has_table_privilege(r, 'public.quotation_documents', p) "
            "from unnest(array['authenticated','anon']) r, "
            "unnest(array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES',"
            "'TRIGGER']) p")
        self.assertTrue(all(not held for _, _, held in privileges), privileges)
        backend = {p: held for _, p, held in self._sql(
            "select 'app_backend', p, has_table_privilege('app_backend', "
            "'public.quotation_documents', p) from unnest(array['SELECT','INSERT','UPDATE',"
            "'DELETE','TRUNCATE']) p")}
        self.assertEqual(backend, {"SELECT": True, "INSERT": True, "UPDATE": False,
                                   "DELETE": False, "TRUNCATE": False})
        forced = self._sql("select relrowsecurity, relforcerowsecurity from pg_class "
                           "where oid = 'public.quotation_documents'::regclass")[0]
        self.assertEqual(forced, (True, True))


# =========================================================================== output safety
class OutputSafetyWebhookTests(Stage3Base):
    """The real BotResponseEngine with a scripted model, through the real webhook."""

    real_engine = True

    def test_repeated_raw_tool_calls_never_reach_the_customer(self):
        call = ('TOOL_CALL: {"name":"update_quotation_brief","arguments":'
                '{"product":"Chocolate cake","size":"1 kg each","quantity":2}}')
        self.provider.replies = [call] * 8
        self._post("hello there, tell me about your cakes")
        texts = self._texts()
        self.assertEqual(texts, [output_safety.FALLBACK_TEXT])
        stored = self._sql("select body_text from public.messages where conversation_id = %s "
                           "and direction = 'outbound'", (self._conversation(),))
        self.assertEqual(stored, [(output_safety.FALLBACK_TEXT,)])
        self.assertTrue(all("TOOL_CALL" not in (t or "") for t in texts))

    def test_tool_errors_arguments_and_unbacked_promises_are_blocked(self):
        for reply in (
            'Sorry, Tool execution failed. {"tool": "get_order_status", "ok": false}',
            "Here are the details: use_brief=true",
            "Please hold on a moment while I finalize that for you.",
            "A team member will prepare the quote for you shortly.",
            "I've sent your quotation.",
        ):
            with self.subTest(reply=reply):
                before = len(self.sends)
                self.provider.replies = [reply]
                self._post("tell me more")
                self.assertEqual(self._texts()[before:], [output_safety.FALLBACK_TEXT])

    def test_ordinary_answers_pass_unchanged(self):
        self.provider.replies = ["A 1 kg Vanilla cake is KES 2,500."]
        self._post("How much is a 1kg vanilla cake?")
        self.assertEqual(self._texts(), ["A 1 kg Vanilla cake is KES 2,500."])
        self.assertEqual(self._quotations(), [])

    # ---- deferred-work promises (Stage 3 output-gate refinement) -------------------
    def _spy_engine(self):
        from saas.bot import BotResponseEngine

        captured: list = []
        original = BotResponseEngine.respond

        def spy(engine, *args, **kwargs):
            response = original(engine, *args, **kwargs)
            captured.append(response)
            return response

        stub = patch.object(BotResponseEngine, "respond", spy)
        stub.start()
        self.addCleanup(stub.stop)
        return captured

    def test_deferred_work_promises_are_replaced_by_the_fallback(self):
        for reply in (
            "Let me check that for you.",
            "I'll check and get back to you.",
            "I'll confirm and let you know.",
            "Checking now...",
            "I'll check that for you. What size would you prefer?",
        ):
            with self.subTest(reply=reply):
                before = len(self.sends)
                self.provider.replies = [reply]
                self._post("do you have a 4kg vanilla cake?")
                self.assertEqual(self._texts()[before:], [output_safety.FALLBACK_TEXT])
        self.assertEqual(self._sql(
            "select 1 from public.handover_requests where wa_id = %s", (self.wa,)), [])

    def test_a_clarification_question_without_a_promise_passes(self):
        self.provider.replies = ["Which size would you like: 1 kg, 2 kg or 3 kg?"]
        self._post("I want a vanilla cake")
        self.assertEqual(self._texts(), ["Which size would you like: 1 kg, 2 kg or 3 kg?"])

    def test_a_recorded_handover_allows_staff_follow_up_wording(self):
        captured = self._spy_engine()
        self.provider.replies = [
            'TOOL_CALL: {"name": "request_staff_handover", "arguments": '
            '{"product": "Rainbow unicorn cake", "reason": "unlisted_product"}}',
            "I'll check with our team and get back to you.",
        ]
        self._post("Do you make a rainbow unicorn cake?")
        response = captured[-1]
        self.assertTrue(response.handover)
        self.assertIsNone(response.output_blocked)
        self.assertEqual(response.text, "I'll check with our team and get back to you.")
        self.assertEqual(len(self._sql(
            "select 1 from public.handover_requests where wa_id = %s", (self.wa,))), 1)
        # The customer receives the platform's own confirmation, never the fallback.
        self.assertNotIn(output_safety.FALLBACK_TEXT, self._texts())

    def test_a_failed_handover_does_not_allow_the_promise(self):
        from platform_admin.services import handover as handover_service

        captured = self._spy_engine()
        self.provider.replies = [
            'TOOL_CALL: {"name": "request_staff_handover", "arguments": '
            '{"product": "Rainbow unicorn cake", "reason": "unlisted_product"}}',
            "I'll check with our team and get back to you.",
        ]
        with patch("saas.handover.create_request", side_effect=RuntimeError("db down")):
            self._post("Do you make a rainbow unicorn cake?")
        response = captured[-1]
        self.assertFalse(response.handover)
        self.assertTrue(response.handover_failed)
        self.assertEqual(response.output_blocked, "unbacked_promise")
        self.assertEqual(response.text, output_safety.FALLBACK_TEXT)
        self.assertEqual(self._texts(), [handover_service.APOLOGY])
        self.assertEqual(self._sql(
            "select 1 from public.handover_requests where wa_id = %s", (self.wa,)), [])


class SharedPricingSourceTests(unittest.TestCase):
    def test_admin_console_uses_the_shared_pricing_engine(self):
        source = (REPO / "platform_admin" / "services" / "quotations.py").read_text(
            encoding="utf-8")
        self.assertIn("saas_quotations.create_quotation(", source)
        self.assertNotIn("insert into public.quotations", source)
        self.assertNotIn("insert into public.quotation_lines", source)
        self.assertNotIn("v.price", source.split("def create_quotation(", 1)[1])

    def test_output_gate_unit_cases(self):
        blocked = {
            'TOOL_CALL: {"name":"request_quote","arguments":{"use_brief":true}}': "raw_tool_call",
            'TOOL_RESULT: {"tool": "x", "ok": true}': "raw_tool_result",
            '{"name": "request_quote", "arguments": {}}': "tool_arguments",
            "Unknown tool: foo.": "tool_error",
            "": "empty",
        }
        for text, reason in blocked.items():
            self.assertEqual(output_safety.check_customer_text(text).reason, reason, text)
        self.assertTrue(output_safety.check_customer_text(
            "Our team will confirm the price.", allow_promises=True).safe)
        self.assertTrue(output_safety.check_customer_text(output_safety.FALLBACK_TEXT).safe)


# The 28 realistic replies approved in the Stage 3 reconciliation (must pass unchanged).
APPROVED_SAMPLE_REPLIES = (
    "Hello and welcome to our bakery! How can I help you today?",
    "A 1 kg Vanilla cake is KES 2,500. A 2 kg is KES 4,200.",
    "We have Vanilla, Chocolate, Red Velvet, Black Forest and White Forest cakes in 1, 2 "
    "and 3 kg sizes.",
    "We're open Monday to Friday 8am to 6pm, and Saturday 9am to 4pm.",
    "Yes, we can write a message on the cake. What would you like it to say?",
    "Our cakes contain eggs, milk and wheat. We can't guarantee they're nut-free.",
    "To place an order, tell me the cake, size and the date you need it.",
    "Your order ORD-1042 is being prepared and will be ready on Friday.",
    "Payment is by M-Pesa. A 50% deposit confirms your order.",
    "Thank you! Have a lovely day.",
    "Sorry, we don't make cupcakes at the moment.",
    "Karibu! Tunaweza kukusaidia vipi leo?",
    "Bei ya keki ya Vanilla ya kilo 1 ni KES 2,500.",
    "Would you like a quotation for that? Just say 'quote' with the cake, size and quantity.",
    "If you'd like a written quotation, send me the cake, size and how many you need.",
    "Delivery charges depend on your location. Where would you like it delivered?",
    "Our Red Velvet is our most popular cake!",
    "The 3 kg Chocolate cake serves about 30 people.",
    "We need at least 2 days' notice for custom orders.",
    "You can collect from our shop in Kilimani.",
    "Please confirm the date you need the cake.",
    "I can help with prices, our menu, orders and opening hours.",
    "That size is out of stock right now, but the 2 kg is available.",
    "Sure, here are our prices: Vanilla 1 kg KES 2,500; Chocolate 1 kg KES 3,100.",
    "We don't offer discounts through this chat, but you can ask our team.",
    "Reply MENU to see our menu.",
    "A quotation shows the price of your cake before you order.",
    "Your quotation Q-20261119-AB12CD34 is valid until 3 December.",
)


class DeferredPromiseGateTests(unittest.TestCase):
    """Unit tests for unsupported promises of future work (no database needed)."""

    UNSUPPORTED = (
        # the approved examples
        "Let me check that for you.",
        "I'll check and get back to you.",
        "I'll confirm and let you know.",
        "Checking now...",
        # wording and punctuation variations
        "Checking now…",
        "checking now",
        "CHECKING...",
        "LET ME CHECK THAT FOR YOU",
        "let me just double-check that!",
        "Let me check that for you 🙂",
        "Let me check with the kitchen.",
        "Let me find out for you.",
        "Let me see if we have it in stock.",
        "I will look into it.",
        "I am going to verify that and revert.",
        "I'll ask our team and update you.",
        "I'm checking with our team now.",
        "I'm currently confirming availability.",
        "We'll get back to you shortly.",
        "We will look into it and revert.",
        "Sure! Let me confirm the price and get back to you.",
        "Checking availability for you now.",
        "Great choice.\nLet me check that for you.",
        # a promise followed by a clarification question is still a promise
        "I'll check that for you. What size would you prefer?",
        "Let me check: would you like 1 kg or 2 kg?",
        "Let me find out. Do you need it by Friday?",
    )

    LEGITIMATE = (
        # genuine clarification questions
        "What size would you prefer: 1 kg, 2 kg or 3 kg?",
        "Which flavour would you like?",
        "Let me ask you a few questions about the cake. What date do you need it?",
        # actions completed in this turn
        "I checked our menu: the 2 kg Vanilla is KES 4,200.",
        "I've checked the catalogue and the Red Velvet comes in 1, 2 and 3 kg.",
        "Let me confirm your order: 2 x Vanilla 1 kg. Is that right?",
        # mentions of checking that promise no future work
        "You can check our menu anytime by replying MENU.",
        "Please check the spelling of the product name.",
        "Thanks for checking with us!",
        "We check every cake before it leaves the shop.",
        "Have you checked your M-Pesa messages?",
        "Let me know if you need anything else!",
        "We'll let you know when your order is ready for collection.",
        # work that happens when the customer replies
        "Once you tell me the size, I'll confirm the price.",
        "Tell me the date and I'll check what we can make.",
        "If you send me the date, I'll check our calendar.",
    )

    def test_each_unsupported_promise_is_blocked(self):
        for text in self.UNSUPPORTED:
            with self.subTest(text=text):
                self.assertEqual(output_safety.check_customer_text(text).reason,
                                 "unbacked_promise")
                self.assertEqual(output_safety.safe_customer_text(text),
                                 (output_safety.FALLBACK_TEXT, "unbacked_promise"))

    def test_legitimate_replies_pass_unchanged(self):
        for text in self.LEGITIMATE:
            with self.subTest(text=text):
                self.assertEqual(output_safety.safe_customer_text(text), (text, None))

    def test_the_28_approved_sample_replies_still_pass_unchanged(self):
        self.assertEqual(len(APPROVED_SAMPLE_REPLIES), 28)
        for text in APPROVED_SAMPLE_REPLIES:
            with self.subTest(text=text):
                self.assertEqual(output_safety.safe_customer_text(text), (text, None))

    def test_a_recorded_handover_allows_follow_up_wording(self):
        for text in ("I'll check with our team and get back to you.",
                     "Our team will get back to you shortly.",
                     "Let me check that for you."):
            with self.subTest(text=text):
                self.assertTrue(output_safety.check_customer_text(
                    text, allow_promises=True).safe)
                self.assertFalse(output_safety.check_customer_text(text).safe)

    def test_a_recorded_handover_never_allows_protocol_errors_or_false_claims(self):
        for text in ('TOOL_CALL: {"name": "x"}', "Tool execution failed",
                     "I've sent your quotation."):
            with self.subTest(text=text):
                self.assertFalse(output_safety.check_customer_text(
                    text, allow_promises=True).safe)

    def test_the_engine_allows_promises_only_for_a_recorded_handover(self):
        source = (REPO / "saas" / "bot.py").read_text(encoding="utf-8")
        self.assertIn("allow_promises=bool(handover)\n", source.replace("\r\n", "\n"))
        self.assertNotIn("or handover_failed\n            )", source.replace("\r\n", "\n"))


if __name__ == "__main__":
    unittest.main()
