"""Phase 1, S2: shared, engine-independent fixes (decision DP-4: all tenants).

1. One size reader for quotation and order pricing (D11), with the mandatory
   order-pricing equivalence test over both catalogue layouts.
2. Quantity grammar: "kilo 2", number words, corrections (D8).
3. A 12-turn, character-capped history window, stored and sent (D7).
4. Workflow-based tool-use rules enforced before execution (D12).
"""

from __future__ import annotations

import contextlib
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from saas import bot as bot_module
from saas import interpretation
from saas import quotation_intent as intent
from saas import quotations as saas_quotations
from saas import tool_policy
from saas import tools as tool_module
from saas import variant_resolver
from saas.bot import BotResponseEngine, ConversationTurn, TenantBotContext
from saas.tenant_admin import TenantAdmin

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "cindy_catalogue_fixture.json").read_text(encoding="utf-8")
)


def _name_layout():
    """Cindy's real layout: the size only in the variant name."""
    return [
        {"id": p["name"], "name": p["name"], "variants": [
            {"id": f"{p['name']}|{v['name']}", "product_id": p["name"], "code": None,
             "name": v["name"], "price": Decimal(str(v.get("price", 100))), "attributes": {}}
            for v in p["variants"]]}
        for p in FIXTURE["products"]
    ]


def _attribute_layout():
    """The other layout: size only in attributes.weight_kg, names carry no size."""
    out = []
    for p in FIXTURE["products"]:
        variants = []
        for n, v in enumerate(p["variants"]):
            size = int(v["name"].split()[0])
            variants.append(
                {"id": f"{p['name']}|a{size}", "product_id": p["name"], "code": None,
                 "name": ("Small", "Medium", "Large", "XL")[n % 4],
                 "price": Decimal(str(v.get("price", 100))),
                 "attributes": {"weight_kg": size}}
            )
        out.append({"id": p["name"], "name": p["name"], "variants": variants})
    return out


# =====================================================================================
# 1. Shared size reader
# =====================================================================================
class VariantSizeReaderTests(unittest.TestCase):
    def test_attribute_wins_over_the_name(self):
        self.assertEqual(
            variant_resolver.variant_size_kg({"name": "1 kg", "attributes": {"weight_kg": 2}}),
            Decimal("2"),
        )

    def test_name_is_read_when_there_is_no_attribute(self):
        for name, expected in (("1 kg", "1"), ("2kg", "2"), ("3 KG", "3"), ("1.5 kg", "1.5"),
                               ("2 kilos", "2"), ("Vanilla 3 kilograms", "3")):
            with self.subTest(name=name):
                self.assertEqual(
                    variant_resolver.variant_size_kg({"name": name, "attributes": {}}),
                    Decimal(expected),
                )

    def test_no_readable_size(self):
        for variant in ({"name": "Large"}, {"name": None}, {"name": "10kgx"},
                        {"name": "Large", "attributes": {"weight_kg": "big"}}, None, {}):
            with self.subTest(variant=variant):
                self.assertIsNone(variant_resolver.variant_size_kg(variant))

    def test_invalid_attribute_falls_back_to_the_name(self):
        self.assertEqual(
            variant_resolver.variant_size_kg({"name": "2 kg", "attributes": {"weight_kg": "x"}}),
            Decimal("2"),
        )

    def test_fractional_sizes_are_never_truncated(self):
        # The old order-pricing regex read "1.5 kg" as 5 kg; quotations read 1.5 as 1.
        variant = {"name": "1.5 kg", "attributes": {}}
        self.assertFalse(variant_resolver.matches_weight(variant, 1))
        self.assertFalse(variant_resolver.matches_weight(variant, 5))
        self.assertEqual(saas_quotations._weight_of(variant), "1.5")  # noqa: SLF001

    def test_quotation_reader_reads_cindy_names(self):
        for product in _name_layout():
            for variant in product["variants"]:
                with self.subTest(variant=variant["name"]):
                    self.assertEqual(
                        saas_quotations._weight_of(variant),  # noqa: SLF001
                        variant["name"].split()[0],
                    )

    def test_extraction_prompt_lists_sizes_for_both_layouts(self):
        for layout in (_name_layout(), _attribute_layout()):
            prompt = intent.extraction_prompt(layout[:1])
            self.assertIn('"1kg"', prompt)


class _StubCursor:
    """Answers TenantAdmin.lookup_price's two queries (products, then variants)."""

    def __init__(self, products, variants):
        self._results = [products, variants]
        self.description = None
        self._rows = []

    def execute(self, sql, params=()):
        rows = self._results.pop(0)
        keys = list(rows[0].keys()) if rows else ["id"]
        self.description = [type("C", (), {"name": k})() for k in keys]
        self._rows = [tuple(row.get(k) for k in keys) for row in rows]

    def fetchall(self):
        return list(self._rows)


class OrderPricingEquivalenceTests(unittest.TestCase):
    """Mandatory (spec §1 row 12): quotation pricing and order pricing pick the same
    variant for every product, size and catalogue layout."""

    def _order_pick(self, product, weight):
        product_row = {"id": product["id"], "name": product["name"], "description": None,
                       "category": None, "sort_order": 0, "attributes": {}, "is_active": True}
        variant_rows = [dict(v, sort_order=n, is_active=True, sku=None, availability=None,
                             stock_quantity=None, reorder_level=None)
                        for n, v in enumerate(product["variants"])]
        admin = TenantAdmin(_StubCursor([product_row], variant_rows))
        return admin.lookup_price(product["name"], weight)

    def test_both_pricing_paths_agree_on_both_layouts(self):
        checked = 0
        for layout_name, layout in (("name", _name_layout()), ("attribute", _attribute_layout())):
            for product in layout:
                for weight in (1, 2, 3, 4):
                    with self.subTest(layout=layout_name, product=product["name"], kg=weight):
                        quoted, _options = saas_quotations._match_variant(  # noqa: SLF001
                            {"weight_kg": weight}, product
                        )
                        ordered = self._order_pick(product, weight)
                        if quoted is None:
                            # Order pricing falls back to a non-matching option.
                            self.assertFalse(
                                variant_resolver.matches_weight(
                                    next(v for v in product["variants"]
                                         if v["id"] == ordered["variant"]["id"]),
                                    weight,
                                )
                            )
                        else:
                            self.assertEqual(ordered["variant"]["id"], quoted["id"])
                            self.assertEqual(ordered["variant"]["price"], quoted["price"])
                        checked += 1
        self.assertEqual(checked, 2 * len(FIXTURE["products"]) * 4)


# =====================================================================================
# 2. Quantity grammar
# =====================================================================================
PRODUCTS = [{"name": p["name"], "variants": []} for p in FIXTURE["products"]]


def _read(text):
    m = intent.match_items(text, PRODUCTS)
    return ([(i["product_name"], i["weight_kg"], i["quantity"]) for i in m.items],
            m.loose_weight_kg, m.loose_quantity, m.unreadable_size)


class QuantityGrammarTests(unittest.TestCase):
    def test_sizes_in_either_order_and_in_words(self):
        cases = {
            "Vanilla kilo 2": [("Vanilla", 2, None)],
            "Vanilla 2 kilo": [("Vanilla", 2, None)],
            "Vanilla kg 3": [("Vanilla", 3, None)],
            "Vanilla kilo two": [("Vanilla", 2, None)],
            "two kilos of vanilla": [("Vanilla", 2, None)],
            "a kilo vanilla": [("Vanilla", 1, None)],
            "I want 3 chocolate cakes 2kg": [("Chocolate", 2, 3)],
        }
        for text, items in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_read(text)[0], items)

    def test_number_first_size_wins_and_counts_are_not_sizes(self):
        self.assertEqual(_read("Vanilla 1 kilo 2 cakes")[0], [("Vanilla", 1, 2)])
        self.assertEqual(_read("Vanilla kg 2 cakes")[0], [("Vanilla", None, 2)])

    def test_loose_sizes_and_quantities(self):
        self.assertEqual(_read("kilo 2")[1:3], (2, None))
        self.assertEqual(_read("make it two kilos")[1:3], (2, None))
        self.assertEqual(_read("make it 3")[1:3], (None, 3))

    def test_corrections(self):
        self.assertEqual(_read("Change the quantity to two")[2], 2)
        self.assertEqual(_read("qty 4")[2], 4)
        self.assertEqual(_read("change it to 3")[2], 3)
        self.assertEqual(_read("Change the Vanilla quantity to two")[0], [("Vanilla", None, 2)])

    def test_unchanged_and_unreadable_cases(self):
        self.assertTrue(_read("half a kilo vanilla")[3])
        # A fractional size stays unreadable (never priced as 1 kg or 5 kg). The count
        # read from "1.5" is pre-existing behaviour, unchanged by S2 (reported).
        self.assertEqual(_read("1.5 kg vanilla")[0][0][:2], ("Vanilla", None))
        self.assertTrue(_read("1.5 kg vanilla")[3])
        self.assertEqual(_read("2 vanilla cakes 1kg each")[0], [("Vanilla", 1, 2)])
        # Not quantities: articles in a correction, sizes counted as numbers.
        self.assertIsNone(interpretation.correction_quantity("change it to a chocolate"))
        self.assertIsNone(interpretation.correction_quantity("make it two kilos"))

    def test_grammar_helpers(self):
        self.assertEqual(interpretation.size_value("kilo 2"), (2, False))
        self.assertEqual(interpretation.size_value("half a kg"), (None, True))
        self.assertEqual(interpretation.size_value("no size here"), (None, False))
        self.assertEqual(interpretation.strip_sizes("vanilla kilo 2").split(), ["vanilla"])
        self.assertIs(intent._NUMBER_WORDS, interpretation.NUMBER_WORDS)  # noqa: SLF001


# =====================================================================================
# 3. History window
# =====================================================================================
def _ctx():
    return TenantBotContext(tenant={"id": "t", "name": "Cindy"}, settings={}, catalogue=[])


class _Capture:
    def __init__(self, replies=None):
        self.requests = []
        self.replies = list(replies or [])

    def generate(self, request):
        self.requests.append(request)
        return self.replies.pop(0) if self.replies else "ok"


class HistoryWindowTests(unittest.TestCase):
    def _sent(self, turns):
        provider = _Capture()
        with patch.object(BotResponseEngine, "_system_prompt", return_value="s"):
            BotResponseEngine(None, provider).respond(
                "t", "now", turns, channel="whatsapp", safe_test_mode=False, context=_ctx()
            )
        return [t.text for t in provider.requests[0].history]

    def _turns(self, n, size=5):
        return tuple(
            ConversationTurn("user" if i % 2 == 0 else "assistant", f"t{i}".ljust(size, "."))
            for i in range(n)
        )

    def test_constants(self):
        self.assertEqual(bot_module.MAX_HISTORY_TURNS, 12)
        self.assertEqual(bot_module.MAX_HISTORY_CHARS, 6_000)

    def test_twelve_turns_are_sent_and_the_thirteenth_oldest_is_dropped(self):
        self.assertEqual(len(self._sent(self._turns(12))), 12)
        sent = self._sent(self._turns(13))
        self.assertEqual(len(sent), 12)
        self.assertTrue(sent[0].startswith("t1."))
        self.assertTrue(sent[-1].startswith("t12"))

    def test_character_cap_drops_the_oldest_first(self):
        sent = self._sent(self._turns(12, size=900))  # 10,800 chars > 6,000
        self.assertLessEqual(sum(len(t) for t in sent), bot_module.MAX_HISTORY_CHARS)
        self.assertEqual(len(sent), 6)
        self.assertTrue(sent[-1].startswith("t11"))

    def test_the_newest_turn_is_always_kept(self):
        sent = self._sent((ConversationTurn("user", "x" * 5000),))
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(sent[0]), bot_module.MAX_HISTORY_TURN_LENGTH)

    def test_stored_history_keeps_twelve_turns(self):
        from platform_admin.services import whatsapp as whatsapp_service

        captured = {}

        class _Cur:
            def execute(self, sql, params):
                captured["history"] = params[1].obj

        @contextlib.contextmanager
        def _txn(*_args, **_kwargs):
            yield _Cur()

        history = [{"role": "user", "text": f"m{i}"} for i in range(20)]
        with patch.object(whatsapp_service, "tenant_transaction", _txn):
            whatsapp_service.save_conversation(object.__new__(_Settings), "t", "c", history)
        self.assertEqual([h["text"] for h in captured["history"]],
                         [f"m{i}" for i in range(8, 20)])


class _Settings:
    app_database_url = None


# =====================================================================================
# 4. Tool-use rules
# =====================================================================================
class ToolPolicyRuleTests(unittest.TestCase):
    def test_always_allowed(self):
        for tool in ("get_catalogue", "get_business_rules", "request_staff_handover"):
            for mode in ("live", "playground"):
                self.assertTrue(tool_policy.allowed(tool, mode=mode), (tool, mode))

    def test_order_tools_need_the_order_workflow_and_ordering_enabled(self):
        for tool in ("get_order_draft", "update_order_draft", "calculate_quote"):
            self.assertEqual(tool_policy.decide(tool).reason, "requires_order_workflow")
            self.assertEqual(tool_policy.decide(tool, workflow="order").reason,
                             "ordering_disabled")
            self.assertTrue(tool_policy.allowed(tool, workflow="order", ordering_enabled=True))

    def test_confirm_order_needs_confirmation_and_live_mode(self):
        base = {"workflow": "order", "ordering_enabled": True}
        self.assertEqual(tool_policy.decide("confirm_order").reason, "requires_order_workflow")
        self.assertEqual(tool_policy.decide("confirm_order", **base).reason,
                         "confirmation_missing")
        self.assertEqual(
            tool_policy.decide("confirm_order", confirmed=True, mode="playground", **base).reason,
            "live_mode_only",
        )
        self.assertTrue(tool_policy.allowed("confirm_order", confirmed=True, **base))

    def test_order_status_needs_a_verified_identity(self):
        self.assertFalse(tool_policy.allowed("get_order_status"))
        self.assertFalse(tool_policy.allowed("get_order_status", identity="  "))
        self.assertTrue(tool_policy.allowed("get_order_status", identity="254700000001"))

    def test_payment_status_is_staff_only(self):
        self.assertFalse(tool_policy.allowed("get_payment_status", identity="254700000001"))
        self.assertTrue(tool_policy.allowed("get_payment_status", is_staff=True))

    def test_quotation_and_unknown_tools_are_never_allowed(self):
        for tool in ("update_quotation_brief", "request_quote", "request_quotation_pdf"):
            self.assertFalse(tool_policy.allowed(tool, workflow="order", ordering_enabled=True,
                                                 confirmed=True, is_staff=True, identity="x"))
        self.assertEqual(tool_policy.decide("drop_tables").reason, "unknown_tool")
        self.assertEqual(tool_policy.decide("get_catalogue", mode="other").reason, "unknown_mode")

    def test_every_registered_tool_has_a_rule(self):
        registry = tool_module.default_tool_registry(include_quotation_tools=True)
        for name in registry.names():
            self.assertNotEqual(tool_policy.decide(name).reason, "unknown_tool", name)


class ToolPolicyEnforcementTests(unittest.TestCase):
    """At ``BotResponseEngine.respond`` level, so it applies to every engine (v1/v2)."""

    def _run(self, call, *, patch_tool, customer_key=None, safe_test_mode=False,
             expect_refusal=True):
        executed = []
        name = call["name"]
        provider = _Capture(replies=["TOOL_CALL: " + json.dumps(call), "done"])
        logs = None
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(BotResponseEngine, "_system_prompt", return_value="s")
            )
            stack.enter_context(patch.object(
                patch_tool, "execute",
                side_effect=lambda *a, **k: executed.append(name)
                or tool_module.ToolResult(ok=True, data={"x": 1}),
            ))
            if expect_refusal:
                logs = stack.enter_context(self.assertLogs("saas.bot", level="WARNING"))
            BotResponseEngine(None, provider).respond(
                "t", "SECRET-CUSTOMER-TEXT", (), channel="whatsapp",
                safe_test_mode=safe_test_mode, context=_ctx(), customer_key=customer_key,
            )
        tool_turn = json.dumps(provider.requests[1].messages[-1]) if len(provider.requests) > 1 else ""
        return executed, logs, tool_turn

    def test_order_draft_tool_is_refused_and_never_executed(self):
        executed, logs, tool_turn = self._run(
            {"name": "update_order_draft", "arguments": {"product_name": "Vanilla"}},
            customer_key="254700000009", patch_tool=tool_module.UpdateOrderDraftTool,
        )
        self.assertEqual(executed, [])
        text = "\n".join(logs.output)
        self.assertIn("TOOL REFUSED", text)
        self.assertIn("reason=requires_order_workflow", text)
        self.assertNotIn("SECRET-CUSTOMER-TEXT", text)
        self.assertNotIn("Vanilla", text, "arguments are never logged")
        self.assertIn(tool_policy.REFUSAL_TEXT, tool_turn)

    def test_payment_status_is_refused_for_customers(self):
        executed, logs, _ = self._run(
            {"name": "get_payment_status", "arguments": {}},
            customer_key="254700000009", patch_tool=tool_module.GetPaymentStatusTool,
        )
        self.assertEqual(executed, [])
        self.assertIn("reason=staff_only", "\n".join(logs.output))

    def test_quotation_tool_is_refused(self):
        executed, logs, _ = self._run(
            {"name": "request_quote", "arguments": {}}, customer_key="254700000009",
            patch_tool=tool_module.RequestQuoteTool,
        )
        self.assertEqual(executed, [])
        self.assertIn("reason=quotation_tools_not_model_callable", "\n".join(logs.output))

    def test_order_status_runs_for_a_verified_customer(self):
        executed, _, _ = self._run(
            {"name": "get_order_status", "arguments": {"order_number": "ORD-1"}},
            customer_key="254700000009", patch_tool=tool_module.GetOrderStatusTool,
            expect_refusal=False,
        )
        self.assertEqual(executed, ["get_order_status"])

    def test_order_status_is_refused_without_an_identity(self):
        executed, logs, _ = self._run(
            {"name": "get_order_status", "arguments": {"order_number": "ORD-1"}},
            safe_test_mode=True, patch_tool=tool_module.GetOrderStatusTool,
        )
        self.assertEqual(executed, [])
        self.assertIn("reason=identity_not_verified", "\n".join(logs.output))

    def test_catalogue_still_runs(self):
        executed, _, _ = self._run(
            {"name": "get_catalogue", "arguments": {}}, customer_key="254700000009",
            patch_tool=tool_module.GetCatalogueTool, expect_refusal=False,
        )
        self.assertEqual(executed, ["get_catalogue"])


if __name__ == "__main__":
    unittest.main()
