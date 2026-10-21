"""Contracts for the customer-facing WhatsApp menu.

These tests are database-free: ``tenant_admin`` is replaced with a small fake so the
menu's routing, privacy rules and payload shapes can be asserted without a database.
No test sends a real WhatsApp message; the send functions are patched throughout.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import unittest
from contextlib import ExitStack, nullcontext
from unittest.mock import MagicMock, patch

from platform_admin.app import create_app
from platform_admin.config import Settings
from platform_admin.routes import _send_menu_reply
from platform_admin.services import menu
from platform_admin.services import customers as customer_service
from platform_admin.services import handover
from platform_admin.services import whatsapp as whatsapp_service
from platform_admin.services.validation import ServiceError

PHONE_ID = "phone-test-1"
SECRET = "app-secret"
PRODUCT_ID = "11111111-1111-4111-8111-111111111111"
PRODUCT_ID_2 = "22222222-2222-4222-8222-222222222222"

DOC_ENABLED = {
    "currency": "KES",
    "business_rules": {"delivery_fee": 200},
    "business_hours": {"mon": "9-5", "tue": "9-5"},
    "configuration": {
        "menu": {"enabled": True},
        "knowledge_base": {
            "faqs": [
                {"question": "Do you deliver?", "answer": "Yes, within the estate."},
                {"question": "How do I pay?", "answer": "The team confirms payment."},
            ]
        },
    },
}


def _settings() -> Settings:
    return Settings(
        secret_key="s" * 40,
        cookie_secure=False,
        whatsapp_verify_token="verify-me",
        whatsapp_app_secret=SECRET,
        whatsapp_send_enabled=True,
    )


def _product(pid=PRODUCT_ID, name="Chocolate Cake", category="Cakes", active=True,
             variants=None):
    return {
        "id": pid,
        "name": name,
        "description": "Rich and moist",
        "category": category,
        "is_active": active,
        "variants": variants
        if variants is not None
        else [
            {
                "id": "variant-1",
                "name": "1 kg",
                "price": "2500.00",
                "is_active": True,
                "availability": "IN_STOCK",
            }
        ],
    }


CATALOGUE = [
    _product(),
    _product(PRODUCT_ID_2, name="Brown Bread", category="Bread"),
]


class FakeAdmin:
    def __init__(self, document=None, catalogue=None, orders=None):
        self.document = document if document is not None else DOC_ENABLED
        self.catalogue = catalogue or []
        self.orders = orders or {}

    def get_settings(self):
        return self.document

    def list_catalogue(self):
        return self.catalogue

    def get_order_by_number_for_whatsapp(self, number, wa_id):
        return self.orders.get((number, wa_id))

    def get_order_payment_status(self, order_id):
        for order in self.orders.values():
            if str(order.get("id")) == str(order_id):
                return order["payment"]
        raise KeyError(order_id)


def _admin_patch(admin):
    return patch(
        "platform_admin.services.menu.tenant_admin",
        side_effect=lambda tenant_id, url=None: nullcontext(admin),
    )


def _ctx(admin, *, document=None, wa="254700000001", channel=True):
    return menu.MenuContext(
        settings=_settings(),
        tenant_id="tenant-1",
        wa_id=wa,
        document=document if document is not None else admin.document,
        channel={"tenant_id": "tenant-1", "channel_id": "channel-1",
                 "credentials_ref": "tenants/tenant-1/whatsapp"} if channel else None,
        phone_number_id=PHONE_ID,
        customer={"id": "customer-1", "name": "Alice"},
        conversation={"id": "conversation-1"},
        message_id="wamid.in-1",
    )


class PayloadTests(unittest.TestCase):
    def test_three_buttons_are_built_with_meta_limits(self):
        payload = whatsapp_service.build_button_payload(
            recipient="254700000001",
            body="Choose",
            header="Header",
            footer="Footer",
            buttons=[
                {"id": "a" * 256, "title": "T" * 30},
                {"id": "b", "title": "Second"},
                {"id": "c", "title": "Third"},
            ],
        )
        self.assertEqual(payload["type"], "interactive")
        interactive = payload["interactive"]
        self.assertEqual(interactive["type"], "button")
        self.assertEqual(len(interactive["action"]["buttons"]), 3)
        self.assertEqual(interactive["action"]["buttons"][0]["reply"]["id"], "a" * 256)
        self.assertEqual(interactive["action"]["buttons"][0]["reply"]["title"], "T" * 20)
        self.assertEqual(interactive["body"]["text"], "Choose")

    def test_four_buttons_are_rejected(self):
        with self.assertRaises(ValueError):
            whatsapp_service.build_button_payload(
                recipient="254700000001",
                body="Choose",
                buttons=[{"id": str(i), "title": "x"} for i in range(4)],
            )

    def test_list_rows_are_bounded(self):
        with self.assertRaises(ValueError):
            whatsapp_service.build_list_payload(
                recipient="254700000001",
                body="Choose",
                button_text="Choose",
                sections=[{"title": "S", "rows": [
                    {"id": str(i), "title": "x"} for i in range(11)
                ]}],
            )

    def test_list_truncates_display_text_but_never_the_id(self):
        action_id = "m1.prod." + "a" * 120 + ".9999999999"
        payload = whatsapp_service.build_list_payload(
            recipient="254700000001",
            body="Choose",
            button_text="Choose",
            sections=[{"title": "S" * 30, "rows": [
                {"id": action_id, "title": "T" * 30, "description": "D" * 90}
            ]}],
        )
        row = payload["interactive"]["action"]["sections"][0]["rows"][0]
        self.assertEqual(row["id"], action_id)
        self.assertEqual(row["title"], "T" * 24)
        self.assertEqual(row["description"], "D" * 72)

    def test_oversized_row_id_is_rejected(self):
        with self.assertRaises(ValueError):
            whatsapp_service.build_list_payload(
                recipient="254700000001",
                body="Choose",
                button_text="Choose",
                sections=[{"title": "S", "rows": [{"id": "x" * 257, "title": "T"}]}],
            )


class EnablementTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(menu.is_enabled({}))
        self.assertFalse(menu.is_enabled({"configuration": {}}))
        self.assertFalse(menu.is_enabled({"configuration": {"menu": {}}}))

    def test_only_explicit_true_enables(self):
        self.assertTrue(menu.is_enabled({"configuration": {"menu": {"enabled": True}}}))
        self.assertFalse(
            menu.is_enabled({"configuration": {"menu": {"enabled": "true"}}})
        )


class ActionIdTests(unittest.TestCase):
    def test_round_trip(self):
        value = menu.make_action("prod", PRODUCT_ID, now=1000)
        parsed = menu.parse_action(value, now=1000)
        self.assertEqual(parsed.selection, menu.Selection("prod", PRODUCT_ID))
        self.assertFalse(parsed.expired)

    def test_expired_action_is_reported_not_ignored(self):
        value = menu.make_action("main", now=1000)
        parsed = menu.parse_action(value, now=1000 + menu.MENU_TTL_SECONDS + 61)
        self.assertIsNone(parsed.selection)
        self.assertTrue(parsed.expired)

    def test_far_future_action_is_rejected_as_tampering(self):
        value = menu.make_action("main", now=1000)
        parsed = menu.parse_action(value, now=900)
        self.assertIsNone(parsed.selection)
        self.assertFalse(parsed.expired)

    def test_malformed_ids_are_ignored(self):
        for value in (
            "menu",
            "m1.main..123",
            "m1.unknown.-.9999999999",
            "m2.main.-.9999999999",
            "m1.prod." + "x" * 121 + ".9999999999",
            "m1.main.-.not-a-number",
            None,
        ):
            with self.subTest(value=value):
                self.assertIsNone(menu.parse_action(value, now=1000).selection)


class MainMenuTests(unittest.TestCase):
    def test_six_rows_with_whatsapp_safe_titles(self):
        reply = menu.main_menu(now=1000)
        self.assertEqual(reply.kind, "list")
        rows = reply.sections[0]["rows"]
        self.assertEqual(len(rows), 6)
        self.assertEqual(
            [row["title"] for row in rows],
            [
                "Browse Products",
                "Place an Order",
                "My Order",
                "Delivery & Collection",
                "FAQs",
                "Talk to Staff",
            ],
        )
        for row in rows:
            self.assertLessEqual(len(row["title"]), 24)
            self.assertLessEqual(len(row["id"]), 256)
            self.assertIsNotNone(menu.parse_action(row["id"], now=1000).selection)


class CatalogueTests(unittest.TestCase):
    def test_browse_with_an_empty_catalogue_offers_staff(self):
        admin = FakeAdmin(catalogue=[])
        with _admin_patch(admin):
            reply = menu._browse(_ctx(admin), now=1000)
        self.assertEqual(reply.kind, "buttons")
        self.assertIn("empty", reply.text.lower())
        self.assertIn("Talk to staff", [button["title"] for button in reply.buttons])

    def test_browse_lists_categories(self):
        admin = FakeAdmin(catalogue=CATALOGUE)
        with _admin_patch(admin):
            reply = menu._browse(_ctx(admin), now=1000)
        self.assertEqual(reply.kind, "list")
        rows = reply.sections[0]["rows"]
        self.assertEqual([row["title"] for row in rows], ["Bread", "Cakes"])
        for row in rows:
            self.assertEqual(menu.parse_action(row["id"], now=1000).selection.step, "cat")

    def test_category_lists_its_products(self):
        admin = FakeAdmin(catalogue=CATALOGUE)
        with _admin_patch(admin):
            # Categories are sorted: Bread (0), Cakes (1).
            reply = menu._category(_ctx(admin), "1", now=1000)
        self.assertEqual(reply.kind, "list")
        rows = reply.sections[0]["rows"]
        self.assertEqual([row["title"] for row in rows], ["Chocolate Cake"])
        parsed = menu.parse_action(rows[0]["id"], now=1000).selection
        self.assertEqual((parsed.step, parsed.arg), ("prod", PRODUCT_ID))

    def test_product_detail_shows_only_configured_prices_and_availability(self):
        admin = FakeAdmin(catalogue=CATALOGUE)
        with _admin_patch(admin):
            reply = menu._product(_ctx(admin), PRODUCT_ID, now=1000)
        self.assertEqual(reply.kind, "buttons")
        self.assertIn("Chocolate Cake", reply.text)
        self.assertIn("KES 2,500.00", reply.text)
        self.assertIn("in stock", reply.text)
        self.assertIn("Main menu", [button["title"] for button in reply.buttons])

    def test_product_without_a_price_does_not_invent_one(self):
        admin = FakeAdmin(
            catalogue=[
                _product(
                    variants=[
                        {"id": "v", "name": "Custom", "price": None,
                         "is_active": True, "availability": "MADE_TO_ORDER"}
                    ]
                )
            ]
        )
        with _admin_patch(admin):
            detail = menu._product(_ctx(admin), PRODUCT_ID, now=1000)
            summary = menu._price_summary(_ctx(admin), admin.catalogue[0])
        self.assertNotIn("KES", detail.text)
        self.assertIn("made to order", detail.text)
        self.assertEqual(summary, "Ask us for the price")

    def test_inactive_or_unknown_product_is_not_shown(self):
        admin = FakeAdmin(catalogue=[_product(active=False)])
        with _admin_patch(admin):
            inactive = menu._product(_ctx(admin), PRODUCT_ID, now=1000)
            unknown = menu._product(
                _ctx(admin), "33333333-3333-4333-8333-333333333333", now=1000
            )
            malformed = menu._product(_ctx(admin), "not-a-uuid", now=1000)
        self.assertIn("no longer listed", inactive.text)
        self.assertIn("no longer listed", unknown.text)
        self.assertIn("no longer available", malformed.text)

    def test_menu_command_is_case_and_whitespace_insensitive(self):
        admin = FakeAdmin()
        with _admin_patch(admin):
            reply = menu.handle_text(_ctx(admin), "  MeNu  ", now=1000)
        self.assertIsNotNone(reply)
        self.assertEqual(reply.kind, "list")

    def test_ordinary_text_is_not_treated_as_a_menu_command(self):
        self.assertFalse(menu.is_menu_command("hello there"))
        self.assertFalse(menu.is_menu_command("1"))
        self.assertIsNone(menu.handle_text(_ctx(FakeAdmin()), "hello there"))

    def test_option_names_are_a_text_fallback(self):
        admin = FakeAdmin(catalogue=CATALOGUE)
        with _admin_patch(admin):
            reply = menu.handle_text(_ctx(admin), "browse products")
        self.assertEqual(reply.kind, "list")

    def test_stale_category_token_is_rejected(self):
        admin = FakeAdmin(catalogue=CATALOGUE)
        stale = menu.index_token(1, "A category that no longer exists")
        with _admin_patch(admin):
            reply = menu._category(_ctx(admin), stale, now=1000)
        self.assertIn("changed", reply.text)


class OrderStatusTests(unittest.TestCase):
    @staticmethod
    def _order():
        return {
            "id": "order-1",
            "order_number": "CB-1",
            "customer_name": "Alice Other",
            "customer_phone": "0700000000",
            "payment": {
                "order_number": "CB-1",
                "status": "confirmed",
                "total": "5000.00",
                "required_deposit": "1000.00",
                "amount_paid": "2000.00",
                "outstanding_balance": "3000.00",
                "payment_status": "awaiting_final",
            },
        }

    def test_missing_sender_identity_fails_closed(self):
        admin = FakeAdmin(orders={("CB-1", "254700000001"): self._order()})
        with _admin_patch(admin):
            reply = menu._order_status(_ctx(admin, wa=None), "CB-1", now=1000)
        self.assertIn("can't verify", reply.text)
        self.assertNotIn("CB-1", reply.text)

    def test_another_customers_order_is_not_found(self):
        admin = FakeAdmin(orders={("CB-1", "254799999999"): self._order()})
        with _admin_patch(admin):
            reply = menu._order_status(_ctx(admin), "CB-1", now=1000)
        self.assertIn("couldn't find", reply.text)
        self.assertNotIn("5,000", reply.text)

    def test_own_order_shows_status_but_no_customer_fields(self):
        admin = FakeAdmin(orders={("CB-1", "254700000001"): self._order()})
        with _admin_patch(admin):
            reply = menu._order_status(_ctx(admin), "CB-1", now=1000)
        self.assertIn("CB-1", reply.text)
        self.assertIn("Confirmed", reply.text)
        self.assertIn("KES 5,000.00", reply.text)
        self.assertIn("KES 3,000.00", reply.text)
        self.assertNotIn("Alice Other", reply.text)
        self.assertNotIn("0700000000", reply.text)

    def test_lookup_failure_does_not_leak_details(self):
        admin = FakeAdmin()

        def boom(*args, **kwargs):
            raise RuntimeError("database exploded")

        admin.get_order_by_number_for_whatsapp = boom
        with _admin_patch(admin):
            reply = menu._order_status(_ctx(admin), "CB-1", now=1000)
        self.assertNotIn("database exploded", reply.text)
        self.assertIn("couldn't look up", reply.text)


class StaffTests(unittest.TestCase):
    def test_success_is_reported(self):
        with patch(
            "platform_admin.services.menu.handover_service.request_staff_assistance",
            return_value=(True, "A member of staff will reply."),
        ) as request:
            reply = menu._staff(_ctx(FakeAdmin()))
        self.assertEqual(reply.text, "A member of staff will reply.")
        request.assert_called_once()

    def test_failure_is_explained_without_claiming_delivery(self):
        with patch(
            "platform_admin.services.menu.handover_service.request_staff_assistance",
            return_value=(False, "I've recorded your request, but I couldn't reach the team."),
        ):
            reply = menu._staff(_ctx(FakeAdmin()))
        self.assertIn("recorded", reply.text)
        self.assertNotIn("will reply", reply.text)

    def test_exception_returns_an_apology(self):
        with patch(
            "platform_admin.services.menu.handover_service.request_staff_assistance",
            side_effect=RuntimeError("db down"),
        ):
            reply = menu._staff(_ctx(FakeAdmin()))
        self.assertIn("couldn't arrange", reply.text)

    def test_missing_channel_fails_closed(self):
        reply = menu._staff(_ctx(FakeAdmin(), channel=False))
        self.assertIn("can't arrange", reply.text)


class DeliveryAndFaqTests(unittest.TestCase):
    def test_delivery_uses_configured_values_only(self):
        reply = menu._delivery(_ctx(FakeAdmin()))
        self.assertIn("KES 200.00", reply.text)
        self.assertIn("mon: 9-5", reply.text)
        self.assertNotIn("free", reply.text.lower())

    def test_delivery_without_configuration_offers_staff(self):
        admin = FakeAdmin(document={"configuration": {"menu": {"enabled": True}}})
        reply = menu._delivery(_ctx(admin))
        self.assertIn("aren't set up", reply.text)
        self.assertIn("Talk to staff", [button["title"] for button in reply.buttons])

    def test_faqs_are_listed_and_answered(self):
        admin = FakeAdmin()
        with _admin_patch(admin):
            listing = menu._faqs(_ctx(admin), now=1000)
            answer = menu._faq_answer(_ctx(admin), "0", now=1000)
        self.assertEqual(listing.kind, "list")
        self.assertEqual(len(listing.sections[0]["rows"]), 2)
        self.assertIn("Yes, within the estate.", answer.text)

    def test_missing_or_stale_faq_is_handled(self):
        admin = FakeAdmin(document={"configuration": {"menu": {"enabled": True}}})
        with _admin_patch(admin):
            empty = menu._faqs(_ctx(admin), now=1000)
            stale = menu._faq_answer(_ctx(admin), "99", now=1000)
        self.assertIn("No FAQs", empty.text)
        self.assertIn("no longer available", stale.text)

    def test_a_changed_faq_is_rejected_as_stale(self):
        admin = FakeAdmin()
        stale = menu.index_token(0, "A question that was edited")
        with _admin_patch(admin):
            reply = menu._faq_answer(_ctx(admin), stale, now=1000)
        self.assertIn("changed", reply.text)


class PaginationAndConfigTests(unittest.TestCase):
    @staticmethod
    def _uuid(index: int) -> str:
        return f"00000000-0000-4000-8000-{index:012d}"

    def test_uncategorised_products_are_reachable_via_other(self):
        catalogue = [
            _product(category="Cakes"),
            _product(self._uuid(2), name="Mystery Box", category=""),
        ]
        admin = FakeAdmin(catalogue=catalogue)
        with _admin_patch(admin):
            listing = menu._browse(_ctx(admin), now=1000)
            other = menu._category(_ctx(admin), menu.OTHER_CATEGORY, now=1000)
        titles = [row["title"] for row in listing.sections[0]["rows"]]
        self.assertIn("Other products", titles)
        self.assertIn("Mystery Box", other.text)

    def test_a_catalogue_without_categories_lists_products_directly(self):
        admin = FakeAdmin(
            catalogue=[_product(name="Mystery Box", category="")]
        )
        with _admin_patch(admin):
            listing = menu._browse(_ctx(admin), now=1000)
        self.assertEqual(listing.kind, "list")
        self.assertIn("Mystery Box", listing.text)
        self.assertNotIn("Other products", listing.text)

    def test_more_categories_continues_the_list(self):
        catalogue = [
            _product(self._uuid(i), name=f"Product {i}", category=f"Category {i:02d}")
            for i in range(12)
        ]
        admin = FakeAdmin(catalogue=catalogue)
        ctx = _ctx(admin)
        with _admin_patch(admin):
            first = menu._browse(ctx, page=0, now=1000)
            more = menu.parse_action(first.sections[0]["rows"][-1]["id"], now=1000)
            second = menu._more(ctx, more.selection.arg, now=1000)
        self.assertEqual(len(first.sections[0]["rows"]), 10)
        self.assertEqual(more.selection.step, "more")
        self.assertEqual(len(second.sections[0]["rows"]), 3)

    def test_more_products_continues_a_category(self):
        catalogue = [
            _product(self._uuid(i), name=f"Cake {i}", category="Cakes")
            for i in range(12)
        ]
        admin = FakeAdmin(catalogue=catalogue)
        ctx = _ctx(admin)
        with _admin_patch(admin):
            first = menu._category(ctx, "0", page=0, now=1000)
            more = menu.parse_action(first.sections[0]["rows"][-1]["id"], now=1000)
            second = menu._more(ctx, more.selection.arg, now=1000)
        self.assertEqual(len(first.sections[0]["rows"]), 10)
        self.assertEqual(len(second.sections[0]["rows"]), 3)

    def test_more_questions_continues_the_faqs(self):
        faqs = [
            {"question": f"Question {i}?", "answer": f"Answer {i}"}
            for i in range(12)
        ]
        document = {
            "configuration": {"menu": {"enabled": True}, "knowledge_base": {"faqs": faqs}}
        }
        admin = FakeAdmin(document=document)
        ctx = _ctx(admin)
        with _admin_patch(admin):
            first = menu._faqs(ctx, page=0, now=1000)
            more = menu.parse_action(first.sections[0]["rows"][-1]["id"], now=1000)
            second = menu._more(ctx, more.selection.arg, now=1000)
        self.assertEqual(len(first.sections[0]["rows"]), 10)
        self.assertEqual(len(second.sections[0]["rows"]), 3)

    def test_malformed_more_id_is_rejected(self):
        reply = menu._more(_ctx(FakeAdmin()), "garbage", now=1000)
        self.assertIn("no longer available", reply.text)

    def test_enabling_preserves_other_configuration(self):
        document = {
            "currency": "KES",
            "configuration": {
                "knowledge_base": {"faqs": [{"question": "Q", "answer": "A"}]},
                "delivery": {"notes": "keep me"},
            },
        }
        merged = menu.with_menu_enabled(document)
        self.assertTrue(menu.is_enabled(merged))
        self.assertEqual(
            merged["configuration"]["knowledge_base"],
            {"faqs": [{"question": "Q", "answer": "A"}]},
        )
        self.assertEqual(merged["configuration"]["delivery"], {"notes": "keep me"})
        self.assertEqual(merged["currency"], "KES")
        self.assertNotIn("menu", document["configuration"])

    def test_set_enabled_writes_a_merged_document(self):
        document = {
            "currency": "KES",
            "configuration": {
                "knowledge_base": {"faqs": [{"question": "Q", "answer": "A"}]},
            },
        }
        admin = MagicMock()
        admin.get_settings.return_value = document
        transaction = MagicMock()
        transaction.__enter__.return_value = admin
        transaction.__exit__.return_value = False

        with patch.object(menu, "tenant_admin", return_value=transaction):
            merged = menu.set_enabled(_settings(), "tenant-1", True)

        self.assertTrue(menu.is_enabled(merged))
        admin.update_settings.assert_called_once()
        written = admin.update_settings.call_args.kwargs["configuration"]
        self.assertEqual(
            written["knowledge_base"], {"faqs": [{"question": "Q", "answer": "A"}]}
        )
        self.assertTrue(written["menu"]["enabled"])

    def test_native_list_body_does_not_repeat_the_rows(self):
        reply = menu.main_menu(now=1000)
        self.assertNotIn("Browse Products", reply.body)
        self.assertIn("Browse Products", reply.text)
        self.assertLess(len(reply.body), len(reply.text))

class StaffAssistanceDispatchTests(unittest.TestCase):
    def _call(self, record, created):
        with patch(
            "platform_admin.services.handover.handover_store.create_request",
            return_value=(record, created),
        ), patch("platform_admin.services.handover.deliver", return_value=True) as deliver:
            result = handover.request_staff_assistance(
                _settings(),
                tenant_id="tenant-1",
                channel={},
                phone_number_id=PHONE_ID,
                customer_key="254700000001",
                customer=None,
                conversation=None,
                message_id="wamid.staff-1",
                settings_document={},
                business_name="Cindy Bakes",
            )
        return result, deliver

    def test_an_already_notified_duplicate_does_not_renotify(self):
        (notified, text), deliver = self._call(
            {"id": "request-1", "status": "notified"}, False
        )
        self.assertTrue(notified)
        self.assertIn("let the team know", text)
        deliver.assert_not_called()

    def test_a_pending_duplicate_still_attempts_delivery(self):
        (notified, _), deliver = self._call(
            {"id": "request-1", "status": "failed"}, False
        )
        self.assertTrue(notified)
        deliver.assert_called_once()


class WebhookMenuRouteTests(unittest.TestCase):
    """End-to-end routing with every persistence and send call patched."""

    def setUp(self):
        self.settings = _settings()
        self.app = create_app(self.settings, bot_provider=MagicMock())
        self.client = self.app.test_client()
        self.stack = ExitStack()
        self.resolve = self.stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.resolve_channel",
            return_value={
                "tenant_id": "tenant-1",
                "channel_id": "channel-1",
                "credentials_ref": "tenants/tenant-1/whatsapp",
            },
        ))
        self.claim = self.stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.claim_event", return_value=True
        ))
        self.update = self.stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.update_event"
        ))
        self.stack.enter_context(patch(
            "platform_admin.routes.customer_service.get_or_create_customer",
            return_value={"id": "customer-1"},
        ))
        self.stack.enter_context(patch(
            "platform_admin.routes.customer_service.get_or_create_conversation",
            return_value={"id": "conversation-1"},
        ))
        self.save = self.stack.enter_context(patch(
            "platform_admin.routes.customer_service.save_message",
            return_value={"id": "row-1"},
        ))
        self.mark_sent = self.stack.enter_context(patch(
            "platform_admin.routes.customer_service.mark_message_sent"
        ))
        self.mark_failed = self.stack.enter_context(patch(
            "platform_admin.routes.customer_service.mark_message_failed"
        ))
        self.find_reply = self.stack.enter_context(patch(
            "platform_admin.routes.customer_service.find_outbound_reply",
            return_value=None,
        ))
        self.stack.enter_context(patch(
            "platform_admin.routes.customer_service.update_conversation_activity"
        ))
        self.engine = self.stack.enter_context(patch(
            "platform_admin.routes.BotResponseEngine"
        ))
        self.engine.return_value.respond.return_value.text = "AI reply"
        self.stack.enter_context(patch(
            "platform_admin.routes.handover_service.handle_turn", return_value=None
        ))
        self.stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.save_conversation"
        ))
        self.stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.conversation_history",
            return_value=[],
        ))
        self.load = self.stack.enter_context(patch(
            "platform_admin.routes.menu_service.load_document",
            return_value=DOC_ENABLED,
        ))
        self.send_list = self.stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.send_list",
            return_value="wamid.list",
        ))
        self.send_buttons = self.stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.send_buttons",
            return_value="wamid.buttons",
        ))
        self.send_text = self.stack.enter_context(patch(
            "platform_admin.routes.whatsapp_service.send_text",
            return_value="wamid.text",
        ))
        self.admin = FakeAdmin(
            catalogue=CATALOGUE,
            orders={("CB-1", "254700000001"): OrderStatusTests._order()},
        )
        self.stack.enter_context(_admin_patch(self.admin))

    def tearDown(self):
        self.stack.close()

    @staticmethod
    def _text_payload(body="menu", message_id="wamid.in-1"):
        return {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": PHONE_ID},
                "messages": [{
                    "id": message_id,
                    "from": "254700000001",
                    "type": "text",
                    "text": {"body": body},
                }],
            }}]}],
        }

    @staticmethod
    def _tap_payload(action_id, message_id="wamid.tap-1", kind="button_reply"):
        return {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": PHONE_ID},
                "messages": [{
                    "id": message_id,
                    "from": "254700000001",
                    "type": "interactive",
                    "interactive": {
                        "type": kind,
                        kind: {"id": action_id, "title": "Chosen"},
                    },
                }],
            }}]}],
        }

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

    def test_menu_command_sends_a_list_and_skips_the_ai(self):
        response = self._post(self._text_payload("  MeNu  "))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json.get("menu"))
        self.send_list.assert_called_once()
        self.send_text.assert_not_called()
        self.engine.assert_not_called()
        self.update.assert_called_once()

    def test_disabled_menu_falls_through_to_the_ai(self):
        self.load.return_value = {}
        response = self._post(self._text_payload("menu"))
        self.assertEqual(response.status_code, 200)
        self.send_list.assert_not_called()
        self.engine.assert_called_once()
        self.send_text.assert_called_once()

    def test_ordinary_text_never_reads_menu_settings(self):
        response = self._post(self._text_payload("hello there"))
        self.assertEqual(response.status_code, 200)
        self.load.assert_not_called()
        self.engine.assert_called_once()
        self.send_list.assert_not_called()

    def test_button_tap_routes_to_browse(self):
        action_id = menu.make_action("browse")
        response = self._post(self._tap_payload(action_id))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json.get("menu"))
        self.send_list.assert_called_once()
        self.claim.assert_called_once()
        self.engine.assert_not_called()

    def test_unknown_tap_is_ignored_without_claiming(self):
        response = self._post(self._tap_payload("v1:something-else"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json.get("ignored"))
        self.claim.assert_not_called()
        self.send_list.assert_not_called()

    def test_expired_tap_gets_an_explanation(self):
        expired = menu.make_action(
            "main", now=time.time() - menu.MENU_TTL_SECONDS - 120
        )
        response = self._post(self._tap_payload(expired))
        self.assertEqual(response.status_code, 200)
        self.send_text.assert_called_once()
        self.assertIn("expired", self.send_text.call_args.kwargs["text"].lower())
        self.send_list.assert_not_called()

    def test_interactive_failure_falls_back_to_text(self):
        self.send_list.side_effect = whatsapp_service.WhatsAppSendError(
            "WhatsApp message delivery failed.",
            400,
            provider_code="100",
            http_status=400,
            ambiguous=False,
        )
        response = self._post(self._tap_payload(menu.make_action("browse")))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json.get("delivery"), "sent")
        self.send_text.assert_called_once()
        self.assertIn("Browse Products", self.send_text.call_args.kwargs["text"])

    def test_order_command_is_identity_scoped(self):
        response = self._post(self._text_payload("order CB-1"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json.get("menu"))
        self.send_buttons.assert_called_once()
        body = self.send_buttons.call_args.kwargs["body"]
        self.assertIn("KES 5,000.00", body)
        self.assertNotIn("Alice Other", body)
        self.engine.assert_not_called()

    def test_another_customers_order_is_not_revealed(self):
        self.admin.orders = {("CB-1", "254799999999"): OrderStatusTests._order()}
        response = self._post(self._text_payload("order CB-1"))
        self.assertEqual(response.status_code, 200)
        body = self.send_buttons.call_args.kwargs["body"]
        self.assertIn("couldn't find", body)
        self.assertNotIn("5,000", body)

    def test_disabled_menu_keeps_the_existing_ai_reply(self):
        self.load.return_value = {}
        self._post(self._text_payload("hello"))
        self.engine.assert_called_once()
        self.send_text.assert_called_once()

    def test_a_long_interactive_body_is_sent_as_text(self):
        reply = menu.MenuReply(
            text="x" * 1100,
            kind="buttons",
            buttons=({"id": "a", "title": "A"},),
        )
        result = _send_menu_reply(
            self.settings,
            {"credentials_ref": "ref"},
            PHONE_ID,
            "tenant-1",
            "254700000001",
            reply,
        )
        self.assertEqual(result.outcome, "sent")
        self.assertEqual(result.wamid, "wamid.text")
        self.send_text.assert_called_once()
        self.send_buttons.assert_not_called()

    def test_ambiguous_timeout_does_not_resend(self):
        self.send_list.side_effect = whatsapp_service.WhatsAppSendError(
            "WhatsApp message delivery failed.",
            502,
            provider_message="TimeoutError",
            ambiguous=True,
        )
        response = self._post(self._tap_payload(menu.make_action("browse")))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json.get("delivery"), "uncertain")
        self.send_text.assert_not_called()
        self.mark_sent.assert_not_called()
        self.mark_failed.assert_called_once()
        self.assertEqual(self.mark_failed.call_args.kwargs["error_code"], "ambiguous_send")
        self.update.assert_called_once_with(
            self.settings, "tenant-1", "wamid.tap-1", "failed", "menu_delivery_uncertain"
        )

    def test_both_delivery_methods_failing_is_recorded_failed(self):
        definite = whatsapp_service.WhatsAppSendError(
            "WhatsApp message delivery failed.",
            400,
            provider_code="100",
            http_status=400,
            ambiguous=False,
        )
        self.send_list.side_effect = definite
        self.send_text.side_effect = definite
        response = self._post(self._tap_payload(menu.make_action("browse")))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json.get("delivery"), "failed")
        self.mark_sent.assert_not_called()
        self.mark_failed.assert_called_once()
        self.assertEqual(self.mark_failed.call_args.kwargs["error_code"], "100")
        self.update.assert_called_once_with(
            self.settings, "tenant-1", "wamid.tap-1", "failed", "100"
        )

    def test_outbound_intent_is_recorded_then_marked_sent(self):
        self._post(self._tap_payload(menu.make_action("browse")))
        outbound = [
            call for call in self.save.call_args_list
            if call.kwargs.get("direction") == "outbound"
        ]
        self.assertEqual(len(outbound), 1)
        self.assertEqual(
            outbound[0].kwargs.get("reply_to_provider_message_id"), "wamid.tap-1"
        )
        self.mark_sent.assert_called_once_with(
            self.settings, "tenant-1", "row-1", "wamid.list"
        )

    def test_0035_absent_records_an_uncorrelated_outbound_row(self):
        def save(settings, tenant_id, channel_id, conversation_id, **kwargs):
            if kwargs.get("direction") == "outbound" and "reply_to_provider_message_id" in kwargs:
                raise customer_service.SchemaOutOfDateError()
            return {"id": "row-1"}

        self.save.side_effect = save
        response = self._post(self._tap_payload(menu.make_action("browse")))
        self.assertEqual(response.status_code, 200)
        outbound = [
            call for call in self.save.call_args_list
            if call.kwargs.get("direction") == "outbound"
        ]
        self.assertEqual(len(outbound), 2)
        self.assertNotIn("reply_to_provider_message_id", outbound[1].kwargs)
        self.send_list.assert_called_once()

    def test_an_already_sent_reply_is_not_resent(self):
        def save(settings, tenant_id, channel_id, conversation_id, **kwargs):
            if kwargs.get("direction") == "outbound":
                raise customer_service.DuplicateReplyError()
            return {"id": "inbound-1"}

        self.save.side_effect = save
        self.find_reply.return_value = {
            "id": "old-row",
            "provider_message_id": "wamid.old",
        }
        response = self._post(self._tap_payload(menu.make_action("browse")))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json.get("duplicate_reply"))
        self.send_list.assert_not_called()
        self.update.assert_called_once_with(
            self.settings, "tenant-1", "wamid.tap-1", "processed",
            response_message_id="wamid.old",
        )

    def test_an_ambiguous_existing_reply_is_not_resent(self):
        def save(settings, tenant_id, channel_id, conversation_id, **kwargs):
            if kwargs.get("direction") == "outbound":
                raise customer_service.DuplicateReplyError()
            return {"id": "inbound-1"}

        self.save.side_effect = save
        self.find_reply.return_value = {"id": "old-row", "provider_message_id": None}
        response = self._post(self._tap_payload(menu.make_action("browse")))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json.get("ambiguous"))
        self.send_list.assert_not_called()
        self.update.assert_called_once_with(
            self.settings, "tenant-1", "wamid.tap-1", "failed", "ambiguous_send"
        )





