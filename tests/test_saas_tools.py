"""Unit tests for the generic AI tool-execution layer in saas.tools.

Tools are unit-tested with a mocked ``tenant_admin`` so no database is
required. The protocol (parse/execute) is tested independently.
"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from saas.config import (
    DEFAULT_DELIVERY_FEE_KES,
    DEFAULT_DEPOSIT_RATE,
    order_policy_from_settings,
)
from saas.tools import (
    ORDER_NOT_FOUND_MESSAGE,
    ToolContext,
    ToolRegistry,
    ToolResult,
    CalculateQuoteTool,
    ConfirmOrderTool,
    GetBusinessRulesTool,
    GetCatalogueTool,
    GetOrderDraftTool,
    GetOrderStatusTool,
    GetPaymentStatusTool,
    UpdateOrderDraftTool,
    default_tool_registry,
    execute_tool_calls,
    parse_tool_calls,
)


class ToolContextTests(unittest.TestCase):
    def test_creation(self):
        ctx = ToolContext(tenant_id="tenant-a", customer_key="wa-123", is_staff=True)
        self.assertEqual(ctx.tenant_id, "tenant-a")
        self.assertEqual(ctx.customer_key, "wa-123")
        self.assertTrue(ctx.is_staff)

    def test_require_staff_allows_staff(self):
        ctx = ToolContext(tenant_id="tenant-a", customer_key="wa-123", is_staff=True)
        ctx.require_staff()

    def test_require_staff_rejects_customer(self):
        ctx = ToolContext(tenant_id="tenant-a", customer_key="wa-123", is_staff=False)
        with self.assertRaises(PermissionError):
            ctx.require_staff()


class ToolRegistryTests(unittest.TestCase):
    def test_register_get_names(self):
        registry = ToolRegistry()
        self.assertEqual(registry.names(), [])
        tool = MagicMock(name="test_tool")
        tool.name = "test_tool"
        registry.register(tool)
        self.assertEqual(registry.get("test_tool"), tool)
        self.assertIsNone(registry.get("missing"))
        self.assertEqual(registry.names(), ["test_tool"])


class ParseToolCallsTests(unittest.TestCase):
    def test_parses_single_tool_call(self):
        text = 'TOOL_CALL: {"name": "get_catalogue", "arguments": {}}'
        calls = parse_tool_calls(text)
        self.assertEqual(calls, [("get_catalogue", {})])

    def test_parses_multiple_tool_calls(self):
        text = (
            "Some text TOOL_CALL: {\"name\": \"get_catalogue\", \"arguments\": {}} "
            "more text TOOL_CALL: {\"name\": \"get_order_draft\", \"arguments\": {}} done"
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ("get_catalogue", {}))
        self.assertEqual(calls[1], ("get_order_draft", {}))

    def test_parses_arguments(self):
        text = (
            'TOOL_CALL: {"name": "update_order_draft", "arguments": '
            '{"product_name": "cake", "weight_kg": 2, "quantity": 3}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "update_order_draft")
        self.assertEqual(calls[0][1]["product_name"], "cake")
        self.assertEqual(calls[0][1]["weight_kg"], 2)
        self.assertEqual(calls[0][1]["quantity"], 3)

    def test_skips_malformed_json(self):
        text = "TOOL_CALL: {not valid json}"
        self.assertEqual(parse_tool_calls(text), [])

    def test_skips_missing_name(self):
        text = 'TOOL_CALL: {"arguments": {}}'
        self.assertEqual(parse_tool_calls(text), [])

    def test_skips_empty_name(self):
        text = 'TOOL_CALL: {"name": "", "arguments": {}}'
        self.assertEqual(parse_tool_calls(text), [])

    def test_returns_empty_for_no_calls(self):
        self.assertEqual(parse_tool_calls("Just plain text, no tools."), [])


class ExecuteToolCallsTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.tool = MagicMock()
        self.tool.name = "fake_tool"
        self.tool.execute.return_value = ToolResult(ok=True, data={"result": "ok"})
        self.registry.register(self.tool)
        self.context = ToolContext(tenant_id="tenant-a", customer_key="wa-123")

    def test_executes_known_tool(self):
        results = execute_tool_calls(
            self.registry, self.context, [("fake_tool", {"arg": 1})]
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["tool"], "fake_tool")
        self.assertEqual(results[0]["data"], {"result": "ok"})
        self.tool.execute.assert_called_once()
        call_args = self.tool.execute.call_args
        self.assertIs(call_args[0][0], self.context)
        self.assertEqual(call_args[0][1], {"arg": 1})

    def test_unknown_tool_returns_error(self):
        results = execute_tool_calls(
            self.registry, self.context, [("missing_tool", {})]
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["tool"], "missing_tool")
        self.assertIn("Unknown tool", results[0]["error"])

    def test_tool_exception_is_captured(self):
        self.tool.execute.side_effect = RuntimeError("boom")
        results = execute_tool_calls(
            self.registry, self.context, [("fake_tool", {})]
        )
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error"], "Tool execution failed.")


class _FakeAdmin:
    """Stand-in for TenantAdmin that records calls and returns canned data."""

    def __init__(self, data: dict | None = None):
        self.data = data or {}
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_catalogue(self):
        self.calls.append("list_catalogue")
        return self.data.get("catalogue", [])

    def get_settings(self):
        self.calls.append("get_settings")
        return self.data.get("settings")

    def get_order_draft_id_by_whatsapp(self, wa_id):
        self.calls.append("get_order_draft_id_by_whatsapp")
        return self.data.get("draft_id")

    def get_order(self, order_id):
        self.calls.append("get_order")
        return self.data.get("order")

    def list_order_items(self, order_id):
        self.calls.append("list_order_items")
        return self.data.get("items", [])

    def create_order(self, **kwargs):
        self.calls.append("create_order")
        return "order-id-new"

    def update_order(self, order_id, **values):
        self.calls.append("update_order")
        return True

    def add_order_item(self, order_id, **kwargs):
        self.calls.append("add_order_item")
        return 1

    def quote_order(self, order_id):
        self.calls.append("quote_order")
        return self.data.get("quote", {})

    def confirm_order(self, order_id):
        self.calls.append("confirm_order")
        return self.data.get("quote", {})

    def get_order_by_number(self, order_number, *, whatsapp_wa_id=None):
        self.calls.append("get_order_by_number")
        self.last_wa_filter = whatsapp_wa_id
        owner = self.data.get("order_owner")
        if whatsapp_wa_id and owner and whatsapp_wa_id != owner:
            # Mirrors the SQL predicate: a number that is not the owner's matches nothing.
            return None
        return self.data.get("order")

    def get_order_payment_status(self, order_id):
        self.calls.append("get_order_payment_status")
        return self.data.get("payment_status", {})

    def list_order_payments(self, order_id):
        self.calls.append("list_order_payments")
        return self.data.get("payments", [])

    def lookup_price(self, product_name, weight_kg):
        self.calls.append("lookup_price")
        return self.data.get("price_lookup")


class GetCatalogueToolTests(unittest.TestCase):
    def test_returns_catalogue(self):
        fake = _FakeAdmin({"catalogue": [{"name": "Cake", "variants": []}]})
        with patch("saas.tools.tenant_admin", return_value=fake):
            tool = GetCatalogueTool()
            result = tool.execute(ToolContext("tenant-a"), {})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["products"], [{"name": "Cake", "variants": []}])


class GetBusinessRulesToolTests(unittest.TestCase):
    def test_returns_business_rules(self):
        fake = _FakeAdmin({
            "settings": {
                "currency": "KES",
                "business_rules": {"deposit_rate": 0.3, "delivery_fee": 150},
                "bot_persona": {"tone": "friendly"},
                "business_hours": {"mon": "09:00-17:00"},
                "timezone": "Africa/Nairobi",
            }
        })
        with patch("saas.tools.tenant_admin", return_value=fake):
            tool = GetBusinessRulesTool()
            result = tool.execute(ToolContext("tenant-a"), {})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["currency"], "KES")
        self.assertEqual(result.data["deposit_rate"], 0.3)
        self.assertEqual(result.data["delivery_fee"], 150)


class GetOrderDraftToolTests(unittest.TestCase):
    def test_returns_none_when_no_draft(self):
        fake = _FakeAdmin({"draft_id": None})
        with patch("saas.tools.tenant_admin", return_value=fake):
            tool = GetOrderDraftTool()
            result = tool.execute(
                ToolContext("tenant-a", customer_key="wa-123"), {}
            )
        self.assertTrue(result.ok)
        self.assertIsNone(result.data["order"])

    def test_returns_order_with_items(self):
        fake = _FakeAdmin({
            "draft_id": "draft-1",
            "order": {
                "id": "draft-1",
                "order_number": "ORD-0001",
                "status": "draft",
                "currency": "KES",
                "subtotal": 1000,
                "discount": 0,
                "total": 1000,
                "required_deposit": 500,
                "amount_paid": 0,
                "outstanding_balance": 1000,
                "payment_status": "unpaid",
                "customer_name": "John",
                "customer_phone": "0712345678",
                "fulfilment": "delivery",
            },
            "items": [{"name": "Cake", "quantity": 2}],
        })
        with patch("saas.tools.tenant_admin", return_value=fake):
            tool = GetOrderDraftTool()
            result = tool.execute(
                ToolContext("tenant-a", customer_key="wa-123"), {}
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["order"]["order_number"], "ORD-0001")
        self.assertEqual(len(result.data["order"]["items"]), 1)

    def test_requires_customer_key(self):
        tool = GetOrderDraftTool()
        result = tool.execute(ToolContext("tenant-a"), {})
        self.assertFalse(result.ok)
        self.assertIn("customer key", result.error)


class UpdateOrderDraftToolTests(unittest.TestCase):
    def test_creates_draft_with_catalogue_price(self):
        fake = _FakeAdmin({
            "draft_id": None,
            "price_lookup": {
                "product": {"id": "p-1", "name": "Cake", "description": "", "category": ""},
                "variant": {"id": "v-1", "code": "cake-2kg", "name": "Cake 2kg", "price": 1200, "sku": None, "availability": "IN_STOCK", "stock_quantity": 10},
            },
            "order": {
                "id": "order-1", "order_number": "ORD-0001", "status": "draft",
                "subtotal": 2400, "total": 2400, "required_deposit": 1200,
            },
            "items": [{"name": "Cake", "quantity": 2}],
        })
        with patch("saas.tools.tenant_admin", return_value=fake):
            tool = UpdateOrderDraftTool()
            result = tool.execute(
                ToolContext("tenant-a", customer_key="wa-123"),
                {"product_name": "Cake", "weight_kg": 2, "quantity": 2},
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["order"]["order_number"], "ORD-0001")
        self.assertEqual(fake.calls.count("create_order"), 1)
        self.assertEqual(fake.calls.count("lookup_price"), 1)
        self.assertEqual(fake.calls.count("add_order_item"), 1)

    def test_requires_weight_for_product(self):
        tool = UpdateOrderDraftTool()
        result = tool.execute(
            ToolContext("tenant-a", customer_key="wa-123"),
            {"product_name": "Cake"},
        )
        self.assertFalse(result.ok)
        self.assertIn("weight_kg", result.error)

    def test_rejects_invalid_quantity(self):
        tool = UpdateOrderDraftTool()
        result = tool.execute(
            ToolContext("tenant-a", customer_key="wa-123"),
            {"product_name": "Cake", "weight_kg": 2, "quantity": 0},
        )
        self.assertFalse(result.ok)
        self.assertIn("quantity", result.error.lower())


class CalculateQuoteToolTests(unittest.TestCase):
    def test_returns_quote_for_draft(self):
        fake = _FakeAdmin({
            "draft_id": "draft-1",
            "quote": {
                "order_number": "ORD-0001",
                "status": "draft",
                "currency": "KES",
                "items": [],
                "subtotal": 1000,
                "discount": 0,
                "delivery_fee": 0,
                "total": 1000,
                "deposit_rate": 0.5,
                "required_deposit": 500,
                "amount_paid": 0,
                "outstanding_balance": 1000,
                "payment_status": "unpaid",
            },
        })
        with patch("saas.tools.tenant_admin", return_value=fake):
            tool = CalculateQuoteTool()
            result = tool.execute(
                ToolContext("tenant-a", customer_key="wa-123"), {}
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["total"], 1000.0)
        self.assertEqual(result.data["required_deposit"], 500.0)

    def test_errors_without_draft(self):
        fake = _FakeAdmin({"draft_id": None})
        with patch("saas.tools.tenant_admin", return_value=fake):
            tool = CalculateQuoteTool()
            result = tool.execute(
                ToolContext("tenant-a", customer_key="wa-123"), {}
            )
        self.assertFalse(result.ok)
        self.assertIn("draft", result.error.lower())


class ConfirmOrderToolTests(unittest.TestCase):
    def test_confirms_draft_with_explicit_consent(self):
        fake = _FakeAdmin({
            "draft_id": "draft-1",
            "order": {
                "id": "draft-1", "status": "draft",
                "order_number": "ORD-0001",
                "total": 1000,
                "required_deposit": 500,
                "outstanding_balance": 1000,
                "currency": "KES",
                "payment_status": "unpaid",
            },
        })
        with patch("saas.tools.tenant_admin", return_value=fake):
            tool = ConfirmOrderTool()
            result = tool.execute(
                ToolContext("tenant-a", customer_key="wa-123"),
                {"confirmed": True},
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["order"]["order_number"], "ORD-0001")
        self.assertEqual(fake.calls.count("confirm_order"), 1)

    def test_rejects_without_explicit_consent(self):
        tool = ConfirmOrderTool()
        result = tool.execute(
            ToolContext("tenant-a", customer_key="wa-123"),
            {"confirmed": False},
        )
        self.assertFalse(result.ok)
        self.assertIn("consent", result.error.lower())

    def test_rejects_missing_confirmed_field(self):
        tool = ConfirmOrderTool()
        result = tool.execute(
            ToolContext("tenant-a", customer_key="wa-123"),
            {},
        )
        self.assertFalse(result.ok)
        self.assertIn("consent", result.error.lower())

    def test_errors_when_order_already_confirmed(self):
        fake = _FakeAdmin({
            "draft_id": "draft-1",
            "order": {"id": "draft-1", "status": "pending_payment", "order_number": "ORD-0001"},
        })
        with patch("saas.tools.tenant_admin", return_value=fake):
            tool = ConfirmOrderTool()
            result = tool.execute(
                ToolContext("tenant-a", customer_key="wa-123"),
                {"confirmed": True},
            )
        self.assertFalse(result.ok)
        self.assertIn("already", result.error.lower())


class GetOrderStatusToolTests(unittest.TestCase):
    def test_returns_order_status(self):
        fake = _FakeAdmin({
            "order_owner": "wa-123",
            "order": {
                "id": "order-1", "order_number": "ORD-0001",
                "status": "confirmed",
                "total": 1000,
                "required_deposit": 500,
                "amount_paid": 500,
                "outstanding_balance": 500,
                "payment_status": "awaiting_final",
            },
            "payment_status": {
                "order_number": "ORD-0001",
                "status": "confirmed",
                "total": 1000,
                "required_deposit": 500,
                "amount_paid": 500,
                "outstanding_balance": 500,
                "payment_status": "awaiting_final",
                "fully_paid": False,
                "deposit_satisfied": True,
            },
        })
        with patch("saas.tools.tenant_admin", return_value=fake):
            tool = GetOrderStatusTool()
            result = tool.execute(
                ToolContext("tenant-a", customer_wa_id="wa-123"),
                {"order_number": "ORD-0001"},
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["order_number"], "ORD-0001")
        self.assertEqual(result.data["total"], 1000.0)
        self.assertTrue(result.data["deposit_satisfied"])

    def test_requires_order_number(self):
        tool = GetOrderStatusTool()
        result = tool.execute(ToolContext("tenant-a"), {})
        self.assertFalse(result.ok)
        self.assertIn("order number", result.error.lower())


class OrderStatusPrivacyTests(unittest.TestCase):
    """A customer may only read an order that belongs to the number they messaged from."""

    STATUS = {
        "id": "order-1", "order_number": "ORD-0001", "status": "confirmed",
        "total": 1000, "required_deposit": 500, "amount_paid": 500,
        "outstanding_balance": 500, "payment_status": "awaiting_final",
    }
    PAYMENT = {
        "order_number": "ORD-0001", "status": "confirmed", "total": 1000,
        "required_deposit": 500, "amount_paid": 500, "outstanding_balance": 500,
        "payment_status": "awaiting_final", "fully_paid": False, "deposit_satisfied": True,
    }

    def _run(self, context, data=None):
        fake = _FakeAdmin(data or {
            "order_owner": "wa-owner",
            "order": self.STATUS,
            "payment_status": self.PAYMENT,
        })
        with patch("saas.tools.tenant_admin", return_value=fake):
            result = GetOrderStatusTool().execute(context, {"order_number": "ORD-0001"})
        return result, fake

    def test_the_owner_sees_their_order(self):
        result, fake = self._run(ToolContext("tenant-a", customer_wa_id="wa-owner"))
        self.assertTrue(result.ok)
        self.assertEqual(fake.last_wa_filter, "wa-owner")

    def test_a_different_customer_sees_nothing(self):
        result, fake = self._run(ToolContext("tenant-a", customer_wa_id="wa-intruder"))
        self.assertFalse(result.ok)
        self.assertIsNone(result.data)
        self.assertEqual(result.error, ORDER_NOT_FOUND_MESSAGE)
        self.assertNotIn("1000", result.error)

    def test_missing_identity_fails_closed_before_any_lookup(self):
        result, fake = self._run(ToolContext("tenant-a"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, ORDER_NOT_FOUND_MESSAGE)
        self.assertEqual(fake.calls, [], "no query may run without a verified identity")

    def test_staff_keep_the_tenant_wide_view(self):
        result, fake = self._run(ToolContext("tenant-a", is_staff=True))
        self.assertTrue(result.ok)
        self.assertIsNone(fake.last_wa_filter)

    def test_the_answer_is_identical_for_missing_and_foreign_orders(self):
        missing, _ = self._run(
            ToolContext("tenant-a", customer_wa_id="wa-owner"),
            {"order": None},
        )
        foreign, _ = self._run(ToolContext("tenant-a", customer_wa_id="wa-other"))
        self.assertEqual(missing.error, foreign.error)


class GetPaymentStatusToolTests(unittest.TestCase):
    def test_requires_staff(self):
        tool = GetPaymentStatusTool()
        result = tool.execute(
            ToolContext("tenant-a", customer_key="wa-123", is_staff=False),
            {"order_number": "ORD-0001"},
        )
        self.assertFalse(result.ok)

    def test_returns_payment_details_for_staff(self):
        fake = _FakeAdmin({
            "order": {"id": "order-1", "order_number": "ORD-0001"},
            "payment_status": {
                "order_number": "ORD-0001",
                "status": "confirmed",
                "total": 1000,
                "required_deposit": 500,
                "amount_paid": 500,
                "outstanding_balance": 500,
                "payment_status": "awaiting_final",
                "deposit_satisfied": True,
                "fully_paid": False,
            },
            "payments": [
                {"id": "pay-1", "kind": "payment", "amount": 500, "method": "mpesa", "reference": "ref-1", "status": "verified", "created_at": "2024-01-01"}
            ],
        })
        with patch("saas.tools.tenant_admin", return_value=fake):
            tool = GetPaymentStatusTool()
            result = tool.execute(
                ToolContext("tenant-a", customer_key="wa-123", is_staff=True),
                {"order_number": "ORD-0001"},
            )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["payments"]), 1)
        self.assertEqual(result.data["payments"][0]["method"], "mpesa")


class DefaultRegistryTests(unittest.TestCase):
    def test_contains_all_builtin_tools(self):
        registry = default_tool_registry()
        names = registry.names()
        expected = [
            "calculate_quote",
            "confirm_order",
            "get_business_rules",
            "get_catalogue",
            "get_order_draft",
            "get_order_status",
            "get_payment_status",
            "request_staff_handover",
            "update_order_draft",
        ]
        for name in expected:
            self.assertIn(name, names)
        self.assertEqual(len(names), len(expected))


class OrderPolicyTests(unittest.TestCase):
    def test_no_settings_returns_defaults(self):
        policy = order_policy_from_settings(None)
        self.assertEqual(policy["deposit_rate"], DEFAULT_DEPOSIT_RATE)
        self.assertEqual(policy["delivery_fee"], DEFAULT_DELIVERY_FEE_KES)
        self.assertEqual(policy["currency"], "KES")

    def test_empty_settings_returns_defaults(self):
        policy = order_policy_from_settings({})
        self.assertEqual(policy["deposit_rate"], DEFAULT_DEPOSIT_RATE)
        self.assertEqual(policy["delivery_fee"], DEFAULT_DELIVERY_FEE_KES)
        self.assertEqual(policy["currency"], "KES")

    def test_settings_without_business_rules_returns_defaults(self):
        policy = order_policy_from_settings({"currency": "KES"})
        self.assertEqual(policy["deposit_rate"], DEFAULT_DEPOSIT_RATE)
        self.assertEqual(policy["delivery_fee"], DEFAULT_DELIVERY_FEE_KES)
        self.assertEqual(policy["currency"], "KES")

    def test_business_rules_overrides_defaults(self):
        settings = {
            "business_rules": {
                "deposit_rate": "0.3",
                "delivery_fee": "150",
            },
            "currency": "KES",
        }
        policy = order_policy_from_settings(settings)
        self.assertEqual(policy["deposit_rate"], Decimal("0.3"))
        self.assertEqual(policy["delivery_fee"], Decimal("150"))
        self.assertEqual(policy["currency"], "KES")

    def test_invalid_deposit_rate_falls_back_to_default(self):
        settings = {"business_rules": {"deposit_rate": "not-a-number"}}
        policy = order_policy_from_settings(settings)
        self.assertEqual(policy["deposit_rate"], DEFAULT_DEPOSIT_RATE)

    def test_invalid_delivery_fee_falls_back_to_default(self):
        settings = {"business_rules": {"delivery_fee": "not-a-number"}}
        policy = order_policy_from_settings(settings)
        self.assertEqual(policy["delivery_fee"], DEFAULT_DELIVERY_FEE_KES)

    def test_numeric_values_are_converted_to_decimal(self):
        settings = {"business_rules": {"deposit_rate": 0.25, "delivery_fee": 100}}
        policy = order_policy_from_settings(settings)
        self.assertEqual(policy["deposit_rate"], Decimal("0.25"))
        self.assertEqual(policy["delivery_fee"], Decimal("100"))

    def test_currency_defaults_to_kes(self):
        settings = {"business_rules": {"deposit_rate": "0.5"}}
        policy = order_policy_from_settings(settings)
        self.assertEqual(policy["currency"], "KES")

    def test_currency_can_be_overridden(self):
        settings = {"currency": "USD", "business_rules": {}}
        policy = order_policy_from_settings(settings)
        self.assertEqual(policy["currency"], "USD")


if __name__ == "__main__":
    unittest.main()