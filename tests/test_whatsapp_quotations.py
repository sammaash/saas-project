"""Database-backed tests for WhatsApp-origin quotations.

Covers the customer-facing workflow end to end at the service and tool layers:
menu selection, free-text requests, multi-turn context and corrections,
server-side pricing, feature gates, PDF document delivery, webhook replay,
send failures (definite and ambiguous) and tenant/customer isolation.

Every outbound WhatsApp call is mocked: no test sends a real message.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from platform_admin.config import Settings
from platform_admin.routes import _deliver_quotation_pdf
from platform_admin.services import menu as menu_service
from platform_admin.services import whatsapp as whatsapp_service
from saas import quotations as service
from saas.tools import (
    RequestQuotationPdfTool,
    RequestQuoteTool,
    ToolContext,
    UpdateQuotationBriefTool,
)
from saas_test_support import (
    TENANT_ONE_ID,
    TENANT_ONE_PHONE_NUMBER_ID,
    TENANT_TWO_ID,
    TENANT_TWO_PHONE_NUMBER_ID,
    SaasDatabaseTestCase,
    _connect,
    app_backend_test_url,
    platform_admin_test_url,
)

WA_ONE = "+254700990001"
WA_TWO = "+254700990002"
CHANNEL_ONE = "c0000000-0000-4000-8000-000000000001"
CHANNEL_TWO = "c0000000-0000-4000-8000-000000000002"


class WhatsAppQuotationTestCase(SaasDatabaseTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.settings = Settings(
            secret_key="q" * 40,
            cookie_secure=False,
            app_database_url=app_backend_test_url(),
            platform_database_url=platform_admin_test_url(),
            whatsapp_send_enabled=True,
        )

    def setUp(self) -> None:
        self._cleanup()
        self.addCleanup(self._cleanup)
        self.customer_one = self._customer(WA_ONE, "Asha", TENANT_ONE_ID)
        self.customer_two = self._customer(WA_TWO, "Brian", TENANT_ONE_ID)
        self.conversation_one = self._conversation(
            TENANT_ONE_ID, self.customer_one, CHANNEL_ONE
        )
        self.conversation_two = self._conversation(
            TENANT_ONE_ID, self.customer_two, CHANNEL_ONE
        )

    @staticmethod
    def _execute(sql: str, parameters: tuple = ()) -> list[tuple]:
        conn = _connect()
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute(sql, parameters)
                return cursor.fetchall() if cursor.description else []
        finally:
            conn.close()

    def _cleanup(self) -> None:
        # Audit rows first: they reference the quotation by id, and the isolation suite
        # asserts the seeded audit_log state exactly, so test traffic must leave none.
        self._execute(
            "delete from public.audit_log where tenant_id in (%s, %s) "
            "and action = 'quotation.issued'",
            (TENANT_ONE_ID, TENANT_TWO_ID),
        )
        self._execute(
            "delete from public.quotation_deliveries where tenant_id in (%s, %s) "
            "and customer_wa_id in (%s, %s)",
            (TENANT_ONE_ID, TENANT_TWO_ID, WA_ONE, WA_TWO),
        )
        self._execute(
            "delete from public.quotation_briefs where tenant_id in (%s, %s) "
            "and customer_wa_id in (%s, %s)",
            (TENANT_ONE_ID, TENANT_TWO_ID, WA_ONE, WA_TWO),
        )
        self._execute(
            "delete from public.customers where tenant_id in (%s, %s) and wa_id in (%s, %s)",
            (TENANT_ONE_ID, TENANT_TWO_ID, WA_ONE, WA_TWO),
        )
        self._execute(
            "delete from public.quotations where tenant_id in (%s, %s) "
            "and customer_wa_id in (%s, %s)",
            (TENANT_ONE_ID, TENANT_TWO_ID, WA_ONE, WA_TWO),
        )
        self._execute(
            "delete from public.handover_requests where tenant_id in (%s, %s) "
            "and wa_id in (%s, %s)",
            (TENANT_ONE_ID, TENANT_TWO_ID, WA_ONE, WA_TWO),
        )

    def _customer(self, wa_id: str, name: str, tenant_id: str) -> str:
        rows = self._execute(
            "insert into public.customers (tenant_id, wa_id, name) "
            "values (%s, %s, %s) returning id::text",
            (tenant_id, wa_id, name),
        )
        return rows[0][0]

    def _conversation(self, tenant_id: str, customer_id: str, channel_id: str) -> str:
        rows = self._execute(
            "insert into public.conversations (tenant_id, customer_id, channel_id) "
            "values (%s, %s, %s) returning id::text",
            (tenant_id, customer_id, channel_id),
        )
        return rows[0][0]

    def _brief(self, items: list[dict], conversation_id: str | None = None) -> dict:
        return service.update_brief(
            self.settings,
            TENANT_ONE_ID,
            conversation_id or self.conversation_one,
            WA_ONE,
            {"items": items},
        )

    def _quotation(self, message_id: str = "wamid.quote-1", **kwargs) -> dict:
        self._brief(
            [{"product_name": "Vanilla", "weight_kg": 2, "quantity": 2}]
        )
        return service.create_quotation_from_brief(
            self.settings,
            TENANT_ONE_ID,
            conversation_id=self.conversation_one,
            customer_wa_id=WA_ONE,
            customer_name=kwargs.pop("customer_name", "Asha"),
            message_id=message_id,
            **kwargs,
        )



class ServerSidePricingTests(WhatsAppQuotationTestCase):
    def test_quotation_prices_from_the_catalogue_and_keeps_an_immutable_snapshot(self):
        quotation = self._quotation("wamid.price-1")

        self.assertEqual(quotation["source"], "whatsapp")
        self.assertEqual(quotation["customer_wa_id"], WA_ONE)
        self.assertIsNone(quotation["created_by_user_id"])
        self.assertEqual(quotation["subtotal"], Decimal("8400.00"))
        self.assertEqual(quotation["total"], Decimal("8400.00"))
        self.assertEqual(len(quotation["lines"]), 1)
        line = quotation["lines"][0]
        self.assertEqual(line["quantity"], 2)
        self.assertEqual(line["unit_price"], Decimal("4200.00"))
        self.assertEqual(line["subtotal"], Decimal("8400.00"))
        self.assertTrue(line["sku"].endswith("-2kg"))

        # A later catalogue edit never changes the issued quotation.
        self._execute(
            "update public.product_variants set price = 9999 where sku = %s",
            (line["sku"],),
        )
        try:
            stored = service.get_quotation(self.settings, TENANT_ONE_ID, quotation["id"])
            self.assertEqual(stored["lines"][0]["unit_price"], Decimal("4200.00"))
            self.assertEqual(stored["total"], Decimal("8400.00"))
        finally:
            self._execute(
                "update public.product_variants set price = 4200 where sku = %s",
                (line["sku"],),
            )

        audit = self._execute(
            "select actor_type, action from public.audit_log "
            "where tenant_id = %s and entity_id = %s",
            (TENANT_ONE_ID, quotation["id"]),
        )
        self.assertEqual(audit, [("system", "quotation.issued")])

    def test_multi_turn_details_merge_and_corrections_update_the_same_item(self):
        first = self._brief([{"product_name": "Chocolate", "quantity": 1}])
        self.assertEqual(first["missing_fields"], ["items[1].weight_kg"])
        self.assertEqual(first["status"], "collecting")

        second = self._brief([{"product_name": "Chocolate", "weight_kg": 3}])
        self.assertEqual(second["missing_fields"], [])
        self.assertEqual(second["status"], "ready")
        self.assertEqual(second["brief"]["items"][0]["quantity"], 1)

        corrected = self._brief([{"product_name": "Chocolate", "quantity": 2}])
        self.assertEqual(len(corrected["brief"]["items"]), 1)
        self.assertEqual(corrected["brief"]["items"][0]["weight_kg"], 3)
        self.assertEqual(corrected["brief"]["items"][0]["quantity"], 2)

        quotation = service.create_quotation_from_brief(
            self.settings,
            TENANT_ONE_ID,
            conversation_id=self.conversation_one,
            customer_wa_id=WA_ONE,
            customer_name="Asha",
            message_id="wamid.correction-1",
        )
        self.assertEqual(quotation["total"], Decimal("12000.00"))

    def test_missing_details_are_reported_per_item(self):
        brief = self._brief([{"product_name": "Vanilla"}])
        self.assertIn("items[1].weight_kg", brief["missing_fields"])
        self.assertIn("items[1].quantity", brief["missing_fields"])
        self.assertFalse(brief["evaluation"]["needs_staff"])


    def test_ambiguous_product_wording_asks_instead_of_guessing(self):
        brief = self._brief([{"product_name": "Forest", "weight_kg": 1, "quantity": 1}])
        self.assertEqual(brief["missing_fields"], ["items[1].product"])
        self.assertIn("White Forest", brief["evaluation"]["message"])
        self.assertIn("Black Forest", brief["evaluation"]["message"])

    def test_unlisted_or_custom_items_require_staff_review(self):
        brief = self._brief([{"product_name": "Wedding tower cake", "quantity": 1}])
        self.assertTrue(brief["evaluation"]["needs_staff"])
        self.assertEqual(brief["status"], "handed_over")
        self.assertEqual(brief["evaluation"]["reason"], "unlisted_product")

        with self.assertRaises(service.QuotationNeedsStaff):
            service.create_quotation_from_brief(
                self.settings,
                TENANT_ONE_ID,
                conversation_id=self.conversation_one,
                customer_wa_id=WA_ONE,
                customer_name="Asha",
                message_id="wamid.custom-1",
            )
        rows = self._execute(
            "select count(*) from public.quotations where tenant_id = %s "
            "and customer_wa_id = %s",
            (TENANT_ONE_ID, WA_ONE),
        )
        self.assertEqual(rows[0][0], 0)

    def test_webhook_replay_returns_the_same_quotation(self):
        quotation = self._quotation("wamid.replay-1")
        duplicate = service.create_quotation_from_brief(
            self.settings,
            TENANT_ONE_ID,
            conversation_id=self.conversation_one,
            customer_wa_id=WA_ONE,
            customer_name="Asha",
            message_id="wamid.replay-1",
        )
        self.assertEqual(duplicate["id"], quotation["id"])
        self.assertTrue(duplicate["duplicate"])
        rows = self._execute(
            "select count(*) from public.quotations where tenant_id = %s "
            "and idempotency_key = %s",
            (TENANT_ONE_ID, "wamid.replay-1"),
        )
        self.assertEqual(rows[0][0], 1)

    def test_another_customer_cannot_read_or_reuse_the_quotation(self):
        quotation = self._quotation("wamid.private-1")
        variant = self._execute(
            "select id::text from public.product_variants where tenant_id = %s "
            "and sku like %s limit 1",
            (TENANT_ONE_ID, "%2kg"),
        )[0][0]

        self.assertIsNone(
            service.get_quotation(
                self.settings, TENANT_ONE_ID, quotation["id"], customer_wa_id=WA_TWO
            )
        )
        self.assertIsNone(
            service.latest_quotation_for_customer(self.settings, TENANT_ONE_ID, WA_TWO)
        )
        self.assertIsNone(
            service.latest_quotation_for_customer(self.settings, TENANT_TWO_ID, WA_ONE)
        )
        with self.assertRaises(service.QuotationConflictError):
            service.create_quotation(
                self.settings,
                TENANT_ONE_ID,
                customer_name="Brian",
                selections=[(variant, 1)],
                source="whatsapp",
                customer_wa_id=WA_TWO,
                idempotency_key="wamid.private-1",
            )

    def test_a_variant_from_another_tenant_cannot_be_quoted(self):
        variant = self._execute(
            "select id::text from public.product_variants where tenant_id = %s "
            "and sku like %s limit 1",
            (TENANT_ONE_ID, "%1kg"),
        )[0][0]
        with self.assertRaises(service.QuotationError):
            service.create_quotation(
                self.settings,
                TENANT_TWO_ID,
                customer_name="Cross tenant",
                selections=[(variant, 1)],
                source="whatsapp",
                customer_wa_id=WA_TWO,
                idempotency_key="wamid.cross-tenant",
            )

    def test_briefs_are_scoped_to_one_conversation(self):
        self._brief([{"product_name": "Vanilla", "weight_kg": 1, "quantity": 1}])
        self.assertIsNotNone(
            service.load_brief(self.settings, TENANT_ONE_ID, self.conversation_one)
        )
        self.assertIsNone(
            service.load_brief(self.settings, TENANT_ONE_ID, self.conversation_two)
        )
        self.assertIsNone(
            service.load_brief(self.settings, TENANT_TWO_ID, self.conversation_one)
        )



class QuotationToolTests(WhatsAppQuotationTestCase):
    def _context(self, **overrides) -> ToolContext:
        values = {
            "tenant_id": TENANT_ONE_ID,
            "customer_key": WA_ONE,
            "message_id": "wamid.tool-1",
            "database_url": self.settings.app_database_url,
            "conversation_id": self.conversation_one,
            "customer_name": "Asha",
            "quotations_enabled": True,
            "send_enabled": True,
        }
        values.update(overrides)
        return ToolContext(**values)

    def test_tools_collect_then_issue_a_catalogue_priced_quotation(self):
        collected = UpdateQuotationBriefTool().execute(
            self._context(),
            {"items": [{"product_name": "Vanilla", "weight_kg": 2, "quantity": 2}]},
        )
        self.assertTrue(collected.ok)
        self.assertEqual(collected.data["missing_fields"], [])
        self.assertFalse(collected.data["needs_staff"])

        issued = RequestQuoteTool().execute(self._context(), {"use_brief": True})
        self.assertTrue(issued.ok, issued.error)
        quotation = issued.data["quotation"]
        self.assertEqual(quotation["total"], "8400.00")
        self.assertEqual(quotation["currency"], "KES")
        self.assertIn("Quotation", quotation["summary"])

        found = RequestQuotationPdfTool().execute(self._context(), {})
        self.assertTrue(found.ok)
        self.assertEqual(found.data["quotation"]["id"], quotation["id"])

    def test_explicit_items_are_priced_by_the_server_not_the_model(self):
        issued = RequestQuoteTool().execute(
            self._context(),
            {
                "items": [
                    {
                        "product_name": "Carrot",
                        "weight_kg": 1,
                        "quantity": 1,
                        "price": "1.00",
                        "total": "1.00",
                    }
                ]
            },
        )
        self.assertTrue(issued.ok, issued.error)
        self.assertEqual(issued.data["quotation"]["total"], "2500.00")

    def test_disabled_gates_and_test_mode_refuse_without_writing(self):
        cases = (
            {"quotations_enabled": False},
            {"send_enabled": False},
            {"safe_test_mode": True},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                result = RequestQuoteTool().execute(
                    self._context(**overrides),
                    {"items": [{"product_name": "Vanilla", "weight_kg": 1, "quantity": 1}]},
                )
                self.assertFalse(result.ok)
        rows = self._execute(
            "select count(*) from public.quotations where tenant_id = %s "
            "and customer_wa_id = %s",
            (TENANT_ONE_ID, WA_ONE),
        )
        self.assertEqual(rows[0][0], 0)

    def test_a_quotation_requires_the_originating_message(self):
        result = RequestQuoteTool().execute(
            self._context(message_id=None),
            {"items": [{"product_name": "Vanilla", "weight_kg": 1, "quantity": 1}]},
        )
        self.assertFalse(result.ok)
        self.assertIn("idempotency", result.error)

    def test_custom_item_reports_needs_staff_instead_of_a_price(self):
        result = RequestQuoteTool().execute(
            self._context(),
            {"items": [{"product_name": "Sculpted dragon cake", "quantity": 1}]},
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.data["needs_staff"])
        self.assertEqual(result.data["reason"], "unlisted_product")

    def test_no_quotation_yet_is_reported_truthfully(self):
        result = RequestQuotationPdfTool().execute(self._context(), {})
        self.assertTrue(result.ok)
        self.assertIsNone(result.data["quotation"])
        self.assertIn("No quotation", result.data["message"])
        other = RequestQuotationPdfTool().execute(
            self._context(customer_key=WA_TWO), {}
        )
        self.assertIsNone(other.data["quotation"])



class QuotationMenuTests(WhatsAppQuotationTestCase):
    def _context(self, *, quotations: bool = True, menu: bool = True) -> "menu_service.MenuContext":
        document = {
            "configuration": {
                "menu": {"enabled": menu},
                "quotations": {"enabled": quotations},
            }
        }
        return menu_service.MenuContext(
            settings=self.settings,
            tenant_id=TENANT_ONE_ID,
            wa_id=WA_ONE,
            document=document,
            conversation={"id": self.conversation_one},
            message_id="wamid.menu-1",
        )

    def test_main_menu_offers_request_a_quotation(self):
        reply = menu_service.main_menu(quotations_enabled=True)
        self.assertIn("Request a Quotation", reply.text)
        self.assertIn("Request a Quotation", [row["title"] for row in reply.sections[0]["rows"]])

    def test_an_enabled_tenant_is_offered_the_option_on_the_real_menu_path(self):
        reply = menu_service.handle_text(self._context(quotations=True), "menu")
        titles = [row["title"] for row in reply.sections[0]["rows"]]
        self.assertIn("Request a Quotation", titles)
        self.assertIn("Request a Quotation", reply.text)

    def test_a_disabled_tenant_is_not_advertised_an_option_it_cannot_use(self):
        reply = menu_service.handle_text(self._context(quotations=False), "menu")
        titles = [row["title"] for row in reply.sections[0]["rows"]]
        self.assertNotIn("Request a Quotation", titles)
        self.assertNotIn("Request a Quotation", reply.text)
        self.assertEqual(len(titles), 6)

    def test_tapping_main_menu_does_not_reintroduce_the_disabled_option(self):
        reply = menu_service.handle_action(
            self._context(quotations=False), menu_service.make_action("main")
        )
        self.assertNotIn(
            "Request a Quotation", [row["title"] for row in reply.sections[0]["rows"]]
        )

    def test_selecting_the_option_starts_a_conversation_scoped_brief(self):
        reply = menu_service.handle_text(self._context(), "request a quotation")
        self.assertIsNotNone(reply)
        self.assertIn("tell me what you'd like a quotation for", reply.text.lower())
        brief = service.load_brief(self.settings, TENANT_ONE_ID, self.conversation_one)
        self.assertEqual(brief["status"], "collecting")
        self.assertEqual(brief["missing_fields"], ["items"])

    def test_disabled_quotation_gate_offers_staff_and_writes_nothing(self):
        reply = menu_service.handle_text(
            self._context(quotations=False), "request a quotation"
        )
        self.assertIn("team", reply.text.lower())
        self.assertIsNone(
            service.load_brief(self.settings, TENANT_ONE_ID, self.conversation_one)
        )

    def test_disabled_sending_gate_offers_staff(self):
        disabled = Settings(
            secret_key="q" * 40,
            cookie_secure=False,
            app_database_url=self.settings.app_database_url,
            whatsapp_send_enabled=False,
        )
        ctx = self._context()
        ctx.settings = disabled
        reply = menu_service.handle_text(ctx, "request a quotation")
        self.assertIn("can't be issued right now", reply.text.lower())
        self.assertIsNone(
            service.load_brief(self.settings, TENANT_ONE_ID, self.conversation_one)
        )

    def test_ordinary_chat_is_not_a_menu_command(self):
        self.assertFalse(menu_service.is_menu_command("how much is a vanilla cake?"))
        self.assertIsNone(
            menu_service.handle_text(self._context(), "how much is a vanilla cake?")
        )



class _ScriptedProvider:
    """Returns prepared reply texts in order and records the prompts it received."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def generate(self, request):
        self.prompts.append(request.system_prompt)
        return self._replies.pop(0)


class BotEngineQuotationTests(WhatsAppQuotationTestCase):
    def _context(self, *, enabled: bool = True):
        from saas.bot import TenantBotContext

        return TenantBotContext(
            tenant={"id": TENANT_ONE_ID, "name": "Cindy Bakes"},
            settings={
                "configuration": {"quotations": {"enabled": enabled}},
                "currency": "KES",
                "timezone": "Africa/Nairobi",
                "business_rules": {},
                "bot_persona": {},
                "business_hours": {},
            },
            catalogue=[],
        )

    def _respond(self, replies: list[str], *, enabled: bool = True, message_id="wamid.engine-1"):
        from saas.bot import BotResponseEngine, TenantBotContextLoader

        provider = _ScriptedProvider(replies)
        engine = BotResponseEngine(
            TenantBotContextLoader(self.settings.app_database_url), provider
        )
        response = engine.respond(
            TENANT_ONE_ID,
            "I'd like 2 vanilla cakes, 2kg each",
            [],
            channel="whatsapp",
            safe_test_mode=False,
            context=self._context(enabled=enabled),
            customer_key=WA_ONE,
            message_id=message_id,
            conversation_id=self.conversation_one,
            customer_name="Asha",
            send_enabled=True,
        )
        return response, provider

    def test_the_prompt_advertises_quotations_only_when_the_gate_is_on(self):
        _, enabled = self._respond(["Hello"], enabled=True)
        self.assertIn("QUOTATION PROTOCOL", enabled.prompts[0])
        self.assertIn("request_quote", enabled.prompts[0])

        _, disabled = self._respond(["Hello"], enabled=False)
        self.assertNotIn("QUOTATION PROTOCOL", disabled.prompts[0])

    def test_a_free_text_request_collects_then_issues_a_quotation(self):
        response, _ = self._respond(
            [
                'TOOL_CALL: {"name": "update_quotation_brief", "arguments": '
                '{"items": [{"product_name": "Vanilla", "weight_kg": 2, "quantity": 2}]}}',
                'TOOL_CALL: {"name": "request_quote", "arguments": {"use_brief": true}}',
                "Your quotation is ready.",
            ]
        )
        self.assertIsNotNone(response.quotation)
        self.assertEqual(response.quotation["total"], "8400.00")
        self.assertFalse(response.quotation_failed)
        self.assertEqual(response.text, "Your quotation is ready.")

    def test_a_custom_request_is_handed_to_staff_and_never_priced(self):
        response, _ = self._respond(
            [
                'TOOL_CALL: {"name": "update_quotation_brief", "arguments": '
                '{"items": [{"product_name": "Sculpted dragon cake", "quantity": 1}]}}',
                'TOOL_CALL: {"name": "request_quote", "arguments": {"use_brief": true}}',
                "Our team will prepare your quote.",
            ]
        )
        self.assertTrue(response.quotation_failed)
        self.assertIsNone(response.quotation)
        self.assertIsNotNone(response.handover)
        rows = self._execute(
            "select count(*) from public.handover_requests where tenant_id = %s "
            "and wa_id = %s",
            (TENANT_ONE_ID, WA_ONE),
        )
        self.assertEqual(rows[0][0], 1)

    def test_the_pdf_tool_surfaces_the_persisted_quotation(self):
        quotation = self._quotation("wamid.pdf-tool-1")
        response, _ = self._respond(
            [
                'TOOL_CALL: {"name": "request_quotation_pdf", "arguments": {}}',
                "I've sent your quotation.",
            ],
            message_id="wamid.pdf-tool-2",
        )
        self.assertIsNotNone(response.quotation_pdf)
        self.assertEqual(response.quotation_pdf["id"], quotation["id"])



class QuotationDocumentDeliveryTests(WhatsAppQuotationTestCase):
    def _deliver(self, quotation: dict, message_id: str = "wamid.doc-1") -> dict:
        return _deliver_quotation_pdf(
            self.settings,
            tenant_id=TENANT_ONE_ID,
            channel={
                "phone_number_id": TENANT_ONE_PHONE_NUMBER_ID,
                "credentials_ref": "tenants/one/whatsapp",
            },
            channel_id=CHANNEL_ONE,
            conversation={"id": self.conversation_one},
            customer_key=WA_ONE,
            quotation=quotation,
            inbound_message_id=message_id,
            caption="Your quotation",
        )

    def test_pdf_is_built_from_the_snapshot_and_provider_ids_are_recorded(self):
        quotation = self._quotation("wamid.doc-quote-1")
        with patch(
            "platform_admin.routes.whatsapp_service.upload_media",
            return_value="media-1",
        ) as upload, patch(
            "platform_admin.routes.whatsapp_service.send_document",
            return_value="wamid.doc-1",
        ) as send:
            outcome = self._deliver(quotation)

        self.assertEqual(outcome["outcome"], "sent")
        self.assertTrue(upload.call_args.kwargs["content"].startswith(b"%PDF-"))
        self.assertEqual(upload.call_args.kwargs["mime_type"], "application/pdf")
        self.assertEqual(send.call_args.kwargs["media_id"], "media-1")
        self.assertEqual(send.call_args.kwargs["recipient"], WA_ONE)

        rows = service.list_deliveries(self.settings, TENANT_ONE_ID, quotation["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delivery_status"], "sent")
        self.assertEqual(rows[0]["media_id"], "media-1")
        self.assertEqual(rows[0]["provider_message_id"], "wamid.doc-1")

    def test_replayed_webhook_does_not_send_the_document_again(self):
        quotation = self._quotation("wamid.doc-quote-2")
        with patch(
            "platform_admin.routes.whatsapp_service.upload_media",
            return_value="media-2",
        ), patch(
            "platform_admin.routes.whatsapp_service.send_document",
            return_value="wamid.doc-2",
        ):
            first = self._deliver(quotation)

        with patch(
            "platform_admin.routes.whatsapp_service.upload_media"
        ) as upload, patch(
            "platform_admin.routes.whatsapp_service.send_document"
        ) as send:
            second = self._deliver(quotation)

        self.assertEqual(first["outcome"], "sent")
        self.assertEqual(second["outcome"], "already")
        self.assertEqual(second["status"], "sent")
        upload.assert_not_called()
        send.assert_not_called()
        self.assertEqual(
            len(service.list_deliveries(self.settings, TENANT_ONE_ID, quotation["id"])),
            1,
        )



    def test_an_ambiguous_timeout_is_uncertain_and_never_retried(self):
        quotation = self._quotation("wamid.doc-quote-3")
        ambiguous = whatsapp_service.WhatsAppSendError(
            "WhatsApp message delivery failed.",
            502,
            provider_message="ReadTimeout",
            ambiguous=True,
        )
        with patch(
            "platform_admin.routes.whatsapp_service.upload_media",
            return_value="media-3",
        ), patch(
            "platform_admin.routes.whatsapp_service.send_document",
            side_effect=ambiguous,
        ):
            first = self._deliver(quotation)

        rows = service.list_deliveries(self.settings, TENANT_ONE_ID, quotation["id"])
        self.assertEqual(first["outcome"], "uncertain")
        self.assertEqual(rows[0]["delivery_status"], "uncertain")
        self.assertEqual(rows[0]["media_id"], "media-3")

        with patch(
            "platform_admin.routes.whatsapp_service.upload_media"
        ) as upload, patch(
            "platform_admin.routes.whatsapp_service.send_document"
        ) as send:
            second = self._deliver(quotation)
        self.assertEqual(second["outcome"], "already")
        upload.assert_not_called()
        send.assert_not_called()

    def test_a_definite_rejection_is_recorded_failed_and_can_be_retried(self):
        quotation = self._quotation("wamid.doc-quote-4")
        rejected = whatsapp_service.WhatsAppSendError(
            "WhatsApp message delivery failed.",
            502,
            provider_code="131047",
            provider_message="Re-engagement message",
            http_status=400,
            ambiguous=False,
        )
        with patch(
            "platform_admin.routes.whatsapp_service.upload_media",
            return_value="media-4",
        ), patch(
            "platform_admin.routes.whatsapp_service.send_document",
            side_effect=rejected,
        ) as failed_send:
            first = self._deliver(quotation)
        self.assertEqual(first["outcome"], "failed")
        rows = service.list_deliveries(self.settings, TENANT_ONE_ID, quotation["id"])
        self.assertEqual(rows[0]["delivery_status"], "failed")
        self.assertEqual(rows[0]["error_code"], "131047")

        with patch(
            "platform_admin.routes.whatsapp_service.upload_media",
            return_value="media-4b",
        ), patch(
            "platform_admin.routes.whatsapp_service.send_document",
            return_value="wamid.doc-4",
        ):
            second = self._deliver(quotation)
        self.assertEqual(second["outcome"], "sent")
        rows = service.list_deliveries(self.settings, TENANT_ONE_ID, quotation["id"])
        self.assertEqual(rows[0]["delivery_status"], "sent")
        self.assertEqual(rows[0]["provider_message_id"], "wamid.doc-4")
        self.assertEqual(failed_send.call_count, 1)

    def test_upload_failure_before_the_send_is_a_definite_failure(self):
        quotation = self._quotation("wamid.doc-quote-5")
        rejected = whatsapp_service.WhatsAppSendError(
            "WhatsApp message delivery failed.",
            502,
            provider_code="100",
            provider_message="Invalid parameter",
            http_status=400,
            ambiguous=False,
        )
        with patch(
            "platform_admin.routes.whatsapp_service.upload_media", side_effect=rejected
        ), patch(
            "platform_admin.routes.whatsapp_service.send_document"
        ) as send:
            outcome = self._deliver(quotation)
        self.assertEqual(outcome["outcome"], "failed")
        send.assert_not_called()
        rows = service.list_deliveries(self.settings, TENANT_ONE_ID, quotation["id"])
        self.assertEqual(rows[0]["delivery_status"], "failed")
        self.assertIsNone(rows[0]["provider_message_id"])

    def test_another_customer_cannot_deliver_the_quotation(self):
        quotation = self._quotation("wamid.doc-quote-6")
        with self.assertRaises(service.QuotationError):
            service.begin_delivery(
                self.settings,
                TENANT_ONE_ID,
                quotation_id=quotation["id"],
                conversation_id=self.conversation_two,
                channel_id=CHANNEL_ONE,
                customer_wa_id=WA_TWO,
                idempotency_key="wamid.other-customer",
            )


if __name__ == "__main__":
    unittest.main()

