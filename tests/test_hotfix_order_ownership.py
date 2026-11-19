"""Hotfix M-1 (port of 359ba8f): customer order lookups are bound to the sender.

Covers the AI-tool path (unit + real PostgreSQL through the non-superuser app_backend
role), the engine's identity plumbing, the staff-only payment tool, and tenant isolation.
The menu path is covered by the existing test_menu_db / test_whatsapp_menu suites.
"""

from __future__ import annotations

import contextlib
import unittest
import uuid
from unittest.mock import patch

from saas import tools as tool_module
from saas.bot import BotResponseEngine, TenantBotContext
from saas.tenant_admin import tenant_admin as real_tenant_admin
from saas.tools import (
    ORDER_NOT_FOUND_MESSAGE,
    GetOrderStatusTool,
    GetPaymentStatusTool,
    ToolContext,
)

try:
    from .saas_test_support import (
        TENANT_ONE_ID, TENANT_TWO_ID, SaasDatabaseTestCase, _connect, app_backend_test_url,
    )
except ImportError:  # discovery inside the tests directory
    from saas_test_support import (  # type: ignore[no-redef]
        TENANT_ONE_ID, TENANT_TWO_ID, SaasDatabaseTestCase, _connect, app_backend_test_url,
    )

STATUS = {
    "id": "order-1", "order_number": "ORD-0001", "status": "confirmed", "total": 1000,
    "required_deposit": 500, "amount_paid": 500, "outstanding_balance": 500,
    "payment_status": "awaiting_final", "fully_paid": False, "deposit_satisfied": True,
}


class _FakeAdmin:
    def __init__(self, owner="wa-owner", order=True):
        self.owner, self.has_order, self.calls = owner, order, []

    def get_order_by_number(self, number):
        self.calls.append(("unbound", number))
        return {"id": "order-1", "order_number": number} if self.has_order else None

    def get_order_by_number_for_whatsapp(self, number, wa_id):
        self.calls.append(("bound", number, wa_id))
        if self.has_order and wa_id == self.owner:
            return {"id": "order-1", "order_number": number}
        return None

    def get_order_payment_status(self, order_id):
        return dict(STATUS)


class ToolUnitTests(unittest.TestCase):
    def _run(self, tool, context, fake=None):
        fake = fake or _FakeAdmin()

        @contextlib.contextmanager
        def _ta(*_a, **_k):
            yield fake

        with patch("saas.tools.tenant_admin", _ta):
            return tool.execute(context, {"order_number": "ORD-0001"}), fake

    def test_owner_reads_own_order_through_the_bound_query(self):
        result, fake = self._run(GetOrderStatusTool(), ToolContext("t", customer_wa_id="wa-owner"))
        self.assertTrue(result.ok)
        self.assertEqual(fake.calls[0], ("bound", "ORD-0001", "wa-owner"))

    def test_other_customer_gets_the_generic_answer_and_no_figures(self):
        result, fake = self._run(GetOrderStatusTool(), ToolContext("t", customer_wa_id="wa-x"))
        self.assertFalse(result.ok)
        self.assertIsNone(result.data)
        self.assertEqual(result.error, ORDER_NOT_FOUND_MESSAGE)
        self.assertNotIn("unbound", [c[0] for c in fake.calls])

    def test_missing_and_foreign_orders_are_indistinguishable(self):
        missing, _ = self._run(GetOrderStatusTool(), ToolContext("t", customer_wa_id="wa-owner"),
                               _FakeAdmin(order=False))
        foreign, _ = self._run(GetOrderStatusTool(), ToolContext("t", customer_wa_id="wa-x"))
        self.assertEqual(missing.error, foreign.error)

    def test_no_verified_identity_fails_closed_before_any_query(self):
        for ctx in (ToolContext("t"), ToolContext("t", customer_key="tenant-fallback"),
                    ToolContext("t", customer_wa_id="   ")):
            result, fake = self._run(GetOrderStatusTool(), ctx)
            self.assertEqual(result.error, ORDER_NOT_FOUND_MESSAGE)
            self.assertEqual(fake.calls, [])

    def test_staff_keep_the_tenant_wide_view(self):
        result, fake = self._run(GetOrderStatusTool(), ToolContext("t", is_staff=True))
        self.assertTrue(result.ok)
        self.assertEqual(fake.calls[0][0], "unbound")

    def test_payment_tool_refuses_every_customer_including_the_owner(self):
        for ctx in (ToolContext("t", customer_wa_id="wa-owner"), ToolContext("t")):
            result, fake = self._run(GetPaymentStatusTool(), ctx)
            self.assertFalse(result.ok)
            self.assertIn("staff-only", result.error)
            self.assertEqual(fake.calls, [], "no lookup before the staff check")


class _Provider:
    def generate(self, request):  # noqa: ANN001
        return "ok"


class EngineIdentityTests(unittest.TestCase):
    def _capture(self, **kwargs):
        captured, real = {}, tool_module.ToolContext

        def spy(**kw):
            captured.update(kw)
            return real(**kw)

        ctx = TenantBotContext(tenant={"id": "t1", "name": "T", "whatsapp_wa_id": "254700000001"},
                               settings={}, catalogue=[])
        engine = BotResponseEngine(None, _Provider())
        with patch.object(BotResponseEngine, "_system_prompt", return_value="s"), \
                patch("saas.bot._tools.ToolContext", side_effect=spy):
            engine.respond("t1", "hello", (), channel="whatsapp", safe_test_mode=False,
                           context=ctx, **kwargs)
        return captured

    def test_supplied_sender_is_the_verified_identity(self):
        c = self._capture(customer_key="254711111111")
        self.assertEqual(c["customer_wa_id"], "254711111111")
        self.assertFalse(c.get("is_staff", False))

    def test_tenant_fallback_is_never_a_customer_identity(self):
        c = self._capture()
        self.assertIsNone(c["customer_wa_id"])
        self.assertEqual(c["customer_key"], "254700000001")


def _sql(sql, params):
    conn = _connect()
    try:
        with conn, conn.cursor() as cursor:
            cursor.execute(sql, params)
    finally:
        conn.close()


class ToolDatabaseTests(SaasDatabaseTestCase):
    """Real PostgreSQL through the non-superuser app_backend role (RLS applies)."""

    def setUp(self):
        s = uuid.uuid4().hex[:8]
        self.wa_a, self.wa_b = f"2547{s}", f"2548{s}"
        self.own, self.other, self.t2 = f"HF-{s}-A", f"HF-{s}-B", f"HF-{s}-T2"
        _sql("insert into public.orders (tenant_id, order_number, customer_name, whatsapp_wa_id,"
             " status, currency) values (%s,%s,'A',%s,'draft','KES'),(%s,%s,'B',%s,'draft','KES'),"
             "(%s,%s,'A2',%s,'draft','KES')",
             (TENANT_ONE_ID, self.own, self.wa_a, TENANT_ONE_ID, self.other, self.wa_b,
              TENANT_TWO_ID, self.t2, self.wa_a))
        self.addCleanup(_sql, "delete from public.orders where order_number in (%s,%s,%s)",
                        (self.own, self.other, self.t2))
        url = app_backend_test_url()
        self.patch = patch("saas.tools.tenant_admin",
                           lambda tid, _url=None: real_tenant_admin(tid, url))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _status(self, tenant, number, **ctx):
        return GetOrderStatusTool().execute(ToolContext(tenant, **ctx), {"order_number": number})

    def test_authorised_customer_reads_own_order(self):
        r = self._status(TENANT_ONE_ID, self.own, customer_wa_id=self.wa_a)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.data["order_number"], self.own)

    def test_unauthorised_customer_cannot_read_another_customers_order(self):
        r = self._status(TENANT_ONE_ID, self.other, customer_wa_id=self.wa_a)
        self.assertFalse(r.ok)
        self.assertEqual(r.error, ORDER_NOT_FOUND_MESSAGE)
        self.assertIsNone(r.data)

    def test_same_sender_cannot_reach_another_tenants_order(self):
        r = self._status(TENANT_ONE_ID, self.t2, customer_wa_id=self.wa_a)
        self.assertEqual(r.error, ORDER_NOT_FOUND_MESSAGE)
        r2 = self._status(TENANT_TWO_ID, self.t2, customer_wa_id=self.wa_a)
        self.assertTrue(r2.ok, r2.error)

    def test_staff_view_is_tenant_wide_but_still_tenant_isolated(self):
        self.assertTrue(self._status(TENANT_ONE_ID, self.other, is_staff=True).ok)
        self.assertFalse(self._status(TENANT_ONE_ID, self.t2, is_staff=True).ok)

    def test_payment_tool_refuses_customers_against_real_data(self):
        r = GetPaymentStatusTool().execute(ToolContext(TENANT_ONE_ID, customer_wa_id=self.wa_a),
                                           {"order_number": self.own})
        self.assertFalse(r.ok)
        self.assertIn("staff-only", r.error)


if __name__ == "__main__":
    unittest.main()
