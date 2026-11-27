"""Regression tests reproducing audit defects D1-D13 on Cindy Bakes' REAL catalogue shape.

Proposed Phase 1 acceptance tests (CINDY_BAKES_CHATBOT_STATE_QUOTATION_AUDIT.md).
Every assertion describes the CORRECT behaviour, so on production 8406072 these tests
are EXPECTED TO FAIL (that is the reproduction). Phase 1 is accepted only when all pass
on both catalogue layouts.

Catalogue: cindy_catalogue_fixture.json, a read-only capture of production on 19 Nov 2026
(11 products x 3 variants named "1 kg"/"2 kg"/"3 kg", attributes {}, MADE_TO_ORDER,
codes CINDY-<PRODUCT>-<N>KG). It is loaded into the disposable test tenant; seed rows are
deactivated during each test and restored afterwards.

Webhook-level tests use the real route, services, RLS and database. Only WhatsApp HTTP
and the model are faked (Stage3Base). When Phase 1 introduces engine v2, the tenant
fixture also sets configuration.conversation_engine = "v2" (see ENGINE_OVERRIDE).
"""

from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from saas import bot as bot_module
from saas import quotation_intent as intent
from saas import quotations as saas_quotations
from saas import tools as tool_module
from saas.bot import BotResponseEngine, ConversationTurn, TenantBotContext

try:
    from .test_stage3_quotations import TENANT, Stage3Base
except ImportError:  # discovery inside the tests directory
    from test_stage3_quotations import TENANT, Stage3Base  # type: ignore[no-redef]

FIXTURE = json.loads((Path(__file__).with_name("cindy_catalogue_fixture.json"))
                     .read_text(encoding="utf-8"))
NAMES = [p["name"] for p in FIXTURE["products"]]
#: Phase 1 sets this to {"conversation_engine": "v2"} so the same tests target the new engine.
ENGINE_OVERRIDE: dict = {"conversation_engine": "v2"}
SETTINGS = {"business_rules": {}, "bot_persona": {}, "business_hours": {},
            "configuration": {}, "timezone": "Africa/Nairobi", "currency": "KES"}


def _weight_attributes(variant_name: str, layout: str) -> dict:
    """'real' = production shape (empty); 'fixture' = the shape old tests assumed."""
    if layout == "real":
        return {}
    return {"weight_kg": int(variant_name.split()[0])}


class CindyCatalogueBase(Stage3Base):
    layout = "real"

    def setUp(self):
        super().setUp()
        if ENGINE_OVERRIDE:
            self._enable_quotations(**ENGINE_OVERRIDE)
        prev_p = self._sql("select id::text, is_active from public.products where tenant_id=%s",
                           (TENANT,))
        prev_v = self._sql("select id::text, is_active from public.product_variants "
                           "where tenant_id=%s", (TENANT,))
        self.restore.append(("select 1", ()))  # keep ordering explicit
        for pid, active in prev_p:
            self.restore.append(("update public.products set is_active=%s where id=%s",
                                 (active, pid)))
        for vid, active in prev_v:
            self.restore.append(("update public.product_variants set is_active=%s where id=%s",
                                 (active, vid)))
        self._sql("update public.product_variants set is_active=false where tenant_id=%s",
                  (TENANT,))
        self._sql("update public.products set is_active=false where tenant_id=%s", (TENANT,))
        tag = uuid.uuid4().hex[:6]
        for product in FIXTURE["products"]:
            existing = self._sql("select id::text from public.products where tenant_id=%s and "
                                 "name_normalized = lower(%s)", (TENANT, product["name"]))
            if existing:
                pid = existing[0][0]
                self._sql("update public.products set is_active=true, category=%s where id=%s",
                          (product["category"], pid))
            else:
                pid = self._sql("insert into public.products (tenant_id, name, category) "
                                "values (%s,%s,%s) returning id::text",
                                (TENANT, product["name"], product["category"]))[0][0]
                self.restore.append(("delete from public.products where id=%s", (pid,)))
            for v in product["variants"]:
                vid = self._sql(
                    "insert into public.product_variants (tenant_id, product_id, code, name, "
                    "price, sku, attributes, availability) values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)"
                    " returning id::text",
                    (TENANT, pid, f"{v['code']}-{tag}", v["name"], v["price"],
                     f"{v['code']}-{tag}", json.dumps(_weight_attributes(v["name"], self.layout)),
                     v["availability"]))[0][0]
                self.restore.append(("delete from public.product_variants where id=%s", (vid,)))
        self.addCleanup(self._sql, "delete from public.quotation_briefs where conversation_id in "
                        "(select c.id from public.conversations c join public.customers cu on "
                        "cu.id=c.customer_id where cu.wa_id=%s)", (self.wa,))

    # helpers -----------------------------------------------------------------
    def _items(self):
        brief = self._brief()
        return [] if not brief else list((brief[1] or {}).get("items") or [])

    def _last_text(self):
        texts = [t for t in self._texts() if t]
        return texts[-1] if texts else ""

    def _staff_unlisted(self):
        return self._sql("select count(*) from public.handover_requests where wa_id=%s and "
                         "reason='unlisted_product'", (self.wa,))[0][0]


class D1_SizeAnswerResolvesVariant(CindyCatalogueBase):
    def test_d1_typed_size_completes_the_quotation(self):
        self._post("Please send me a quotation for 2 Black Forest cakes")
        for answer in ("2 kg",):
            self._post(answer)
        self.assertEqual(len(self._quotations()), 1,
                         f"D1: size answer ignored on real catalogue; bot said {self._texts()}")

    def test_d1_size_question_is_never_repeated(self):
        self._post("Please send me a quotation for 2 Black Forest cakes")
        for answer in ("2kgs", "2 kg", "2"):
            self._post(answer)
        asked = [t for t in self._texts() if t and "Which option of Black Forest" in t]
        self.assertLessEqual(len(asked), 1, f"D1: question repeated {len(asked)} times")


class D2_ProductReplacement(CindyCatalogueBase):
    def test_d2_switching_product_replaces_instead_of_appending(self):
        self._post("Please send me a quotation for 2 Black Forest cakes")
        self._post("Actually make it Vanilla")
        names = [i.get("product_name") for i in self._items()]
        self.assertEqual(names, ["Vanilla"], f"D2: items became {names}")


class D3_NumberReadAgainstPendingQuestion(CindyCatalogueBase):
    def test_d3_bare_number_answering_the_size_question_keeps_quantity(self):
        self._post("Please send me a quotation for 2 Black Forest cakes")
        self._post("1")  # answers "Which option ... 1 kg, 2 kg or 3 kg?"
        items = self._items()
        self.assertTrue(items, "brief missing")
        self.assertEqual(items[0].get("quantity"), 2, f"D3: quantity overwritten: {items}")


class D4_QuoteFragmentsAreNotUnknownProducts(CindyCatalogueBase):
    def test_d4_follow_up_fragment_is_not_handed_over_as_unlisted(self):
        self.provider.replies = [json.dumps({"items": [], "unlisted": ["Just one, get me a quote"]})]
        self._post("Quote for 3 Vanilla cakes, 1 kg each")
        self._post("Just one, get me a quote")
        self.assertEqual(self._staff_unlisted(), 0,
                         "D4: a quotation follow-up was handed to staff as an unlisted product")

    def test_d4_such_cakes_is_resolved_from_context(self):
        self.provider.replies = [json.dumps({"items": [], "unlisted": ["3 such cakes"]})]
        self._post("Please send me a quotation for a 2 kg Vanilla cake")
        self._post("I wanted a quotation of 3 such cakes")
        self.assertEqual(self._staff_unlisted(), 0)


class D5_ContextSurvivesHandoverOrFailure(CindyCatalogueBase):
    def test_d5_valid_items_survive_a_failed_turn(self):
        # S0 amendment (owner decision DP-2): the first message omits the size, so the
        # request is still open when the failed turn arrives (a complete first message is
        # correctly quoted at once, as D1/D13 require).
        self.provider.replies = [json.dumps({"items": [], "unlisted": ["Just one, get me a quote"]})]
        self._post("Quote for 3 Vanilla cakes")
        self._post("Just one, get me a quote")
        brief = self._brief()
        status = brief[0] if brief else None
        names = [i.get("product_name") for i in self._items()]
        self.assertIn(status, ("collecting", "ready"),
                      f"D5: request closed as {status!r}; the next message starts from nothing")
        self.assertIn("Vanilla", names, "D5: collected details were lost after a handover")
        self._post("2 kg")
        self.assertEqual(len(self._quotations()), 1,
                         "D5: the preserved request did not complete after the failed turn")


class D6_AiSeesTheOpenRequest(unittest.TestCase):
    def test_d6_system_prompt_contains_the_open_brief(self):
        ctx = TenantBotContext(tenant={"id": "t", "name": "Cindy"}, settings=SETTINGS,
                               catalogue=[])
        brief = {"items": [{"product_name": "Black Forest", "quantity": 2, "weight_kg": None}],
                 "missing": ["items[1].variant"]}
        prompt = BotResponseEngine._system_prompt(ctx, safe_test_mode=False, brief=brief)
        self.assertIn("Black Forest", prompt, "D6: the AI never sees the open quotation")


class D7_HistoryIsComplete(unittest.TestCase):
    def test_d7_model_receives_at_least_twelve_turns(self):
        seen = {}

        class _P:
            def generate(self, request):
                seen["history"] = request.history
                return "ok"

        turns = tuple(ConversationTurn("user" if i % 2 == 0 else "assistant", f"turn-{i}")
                      for i in range(12))
        ctx = TenantBotContext(tenant={"id": "t", "name": "Cindy"}, settings={}, catalogue=[])
        with patch.object(BotResponseEngine, "_system_prompt", return_value="s"):
            BotResponseEngine(None, _P()).respond("t", "1", turns, channel="whatsapp",
                                                  safe_test_mode=False, context=ctx)
        texts = [t.text for t in seen["history"]]
        self.assertIn("turn-0", texts, f"D7: model saw only {len(texts)} turns")


class D8_Corrections(unittest.TestCase):
    PRODUCTS = [{"name": p["name"], "variants": [
        {"id": f"{p['name']}-{v['name']}", "name": v["name"], "attributes": {}}
        for v in p["variants"]]} for p in FIXTURE["products"]]

    def test_d8_unit_after_number_is_read(self):
        found = intent.match_items("Vanilla kilo 2", self.PRODUCTS).items
        self.assertEqual([(i["product_name"], i["weight_kg"]) for i in found], [("Vanilla", 2)])

    def test_d8_number_word_inside_a_correction_is_read(self):
        m = intent.match_items("Change the quantity to two", self.PRODUCTS)
        self.assertEqual(m.loose_quantity, 2, "D8: correction quantity not parsed")


class D9_MenuDoesNotWipeTheRequest(CindyCatalogueBase):
    def test_d9_typing_quotation_mid_request_keeps_items(self):
        self._enable_quotations(**{"menu": {"enabled": True}})
        self._post("Please send me a quotation for 2 Black Forest cakes")
        self._post("Quotation")
        names = [i.get("product_name") for i in self._items()]
        self.assertIn("Black Forest", names, "D9: menu alias wiped the collected items")


class D10_AllProductsListed(CindyCatalogueBase):
    def test_d10_first_question_lists_every_product(self):
        self._post("I would like a quotation please")
        text = self._last_text()
        missing = [n for n in NAMES if n not in text]
        self.assertEqual(missing, [], f"D10: omitted {missing}")


class D11_OneSizeReader(unittest.TestCase):
    def test_d11_quotation_pricing_reads_size_from_cindy_variant_names(self):
        for product in FIXTURE["products"]:
            for v in product["variants"]:
                weight = saas_quotations._weight_of({"name": v["name"], "attributes": {}})  # noqa: SLF001
                self.assertEqual(str(weight), v["name"].split()[0],
                                 f"D11: quotations cannot read '{v['name']}' "
                                 "while order pricing can")


class D12_OrderToolsNeedTheOrderWorkflow(unittest.TestCase):
    def test_d12_confirm_order_is_not_executable_from_a_customer_turn(self):
        calls = []

        class _P:
            replies = ['TOOL_CALL: {"name": "confirm_order", "arguments": {}}', "ok"]

            def generate(self, request):
                return self.replies.pop(0) if self.replies else "ok"

        ctx = TenantBotContext(tenant={"id": "t", "name": "Cindy"}, settings={}, catalogue=[])
        with patch.object(BotResponseEngine, "_system_prompt", return_value="s"), \
                patch.object(tool_module.ConfirmOrderTool, "execute",
                             side_effect=lambda *a, **k: calls.append(1) or
                             tool_module.ToolResult(ok=False, error="x")):
            BotResponseEngine(None, _P()).respond("t", "yes", (), channel="whatsapp",
                                                  safe_test_mode=False, context=ctx,
                                                  customer_key="254700000009")
        self.assertEqual(calls, [], "D12: order tool executed outside an order workflow")


class D13_LayoutParity(CindyCatalogueBase):
    """The same conversation must behave identically on both catalogue layouts."""

    def _run_conversation(self):
        self._post("Please send me a quotation for 2 Black Forest cakes")
        self._post("2 kg")
        return len(self._quotations())

    def test_d13_real_layout_matches_fixture_layout(self):
        self.assertEqual(self._run_conversation(), 1, "D13: real layout diverges from fixture layout")


class D13_FixtureLayoutControl(D13_LayoutParity):
    """Control: the shape older tests assumed. Expected to PASS on production today."""
    layout = "fixture"


if __name__ == "__main__":
    unittest.main()
