"""Vertical and isolation tests for the tenant-aware orders / payments service.

These tests run against a real PostgreSQL database through the generic
`tenant_admin` helpers: every statement is filtered by RLS on
`app.current_tenant_id()`, so a tenant can never read or mutate another
tenant's rows or caller-supplied tenant ids have no effect.

The statements here are pure service-layer operations; they do not invent
Cindy-specific behaviour. A bakery only gets its pricing, deposit, add-ons
and delivery rules from its tenant-scoped settings and catalogue data.
"""

from __future__ import annotations

import unittest
import uuid
from decimal import Decimal

from saas_test_support import (
    SaasDatabaseTestCase,
    TENANT_ONE_ID,
    TENANT_TWO_ID,
    app_backend_test_url,
)
from saas.tenant_admin import tenant_admin


class OrdersServiceTests(SaasDatabaseTestCase):
    """Service-layer tests: factory functions, validation, state machine."""

    def _make_order(self, **overrides):
        params = {
            "customer_name": "Test Customer",
            "customer_phone": "+254700000000",
            "fulfilment": "pickup",
        }
        params.update(overrides)
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            order_id = ta.create_order(**params)
            ta.add_order_item(
                order_id, name="Chocolate Cake 1kg",
                quantity=1, unit_price=Decimal("1000"),
            )
            return order_id

    # -- factory ---------------------------------------------------------------

    def test_create_order_draft(self):
        order_id = self._make_order()
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            row = ta.get_order(order_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "draft")
        self.assertEqual(str(row["tenant_id"]), TENANT_ONE_ID)
        self.assertEqual(row["total"], Decimal("1000.00"))

    def test_order_number_allocated_from_per_tenant_sequence(self):
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            first = ta.allocate_order_number(prefix="CB")
            second = ta.allocate_order_number(prefix="CB")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("CB-"))
        self.assertTrue(second.startswith("CB-"))

    # -- validation ------------------------------------------------------------

    def test_invalid_fulfilment_rejected(self):
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            with self.assertRaises(ValueError):
                ta.create_order(customer_name="X", fulfilment="banana")

    def test_negative_item_price_rejected(self):
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            order_id = ta.create_order(customer_name="X")
            with self.assertRaises(ValueError):
                ta.add_order_item(order_id, name="Bad", unit_price=Decimal("-1"))

    def test_outstanding_balance_matches_total_minus_paid(self):
        order_id = self._make_order()
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            row = ta.get_order(order_id)
        self.assertEqual(row["outstanding_balance"], Decimal("1000.00"))

    # -- state machine ----------------------------------------------------------

    def test_confirm_order_means_pending_payment(self):
        order_id = self._make_order()
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            ta.confirm_order(order_id)
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            row = ta.get_order(order_id)
        self.assertEqual(row["status"], "pending_payment")
        self.assertEqual(row["payment_status"], "unpaid")

    def test_confirm_incomplete_order_rejected(self):
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            order_id = ta.create_order(customer_name="Empty")
            with self.assertRaises(ValueError):
                ta.confirm_order(order_id)

    def test_confirm_order_without_customer_rejected(self):
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            order_id = ta.create_order()
            ta.add_order_item(order_id, name="Cake", unit_price=Decimal("500"))
            with self.assertRaises(ValueError):
                ta.confirm_order(order_id)

    def test_cancel_consumes_draft_only(self):
        order_id = self._make_order()
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            ta.cancel_order(order_id, "customer changed mind")
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            row = ta.get_order(order_id)
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["cancelled_at"])

    def test_cancelled_order_cannot_be_confirmed(self):
        order_id = self._make_order()
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            ta.cancel_order(order_id, "nope")
        with self.assertRaises(ValueError):
            with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
                ta.confirm_order(order_id)

    # -- payments ---------------------------------------------------------------

    def test_recorded_payment_moves_balance_only_when_verified(self):
        order_id = self._make_order()
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            ta.confirm_order(order_id)
            pid = ta.record_order_payment(
                order_id, amount=Decimal("250"), method="M-Pesa",
                reference="ABC123", recorded_by=str(uuid.uuid4()),
            )
            before = ta.get_order(order_id)
            self.assertEqual(before["amount_paid"], Decimal("0"))
            self.assertTrue(ta.verify_order_payment(pid, verified_by=str(uuid.uuid4())))
            after = ta.get_order(order_id)
        self.assertEqual(after["amount_paid"], Decimal("250.00"))
        self.assertEqual(after["payment_status"], "awaiting_final")

    def test_full_verified_payment_marks_order_paid(self):
        order_id = self._make_order()
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            ta.confirm_order(order_id)
            pid = ta.record_order_payment(
                order_id, amount=Decimal("1000"), method="M-Pesa",
                recorded_by=str(uuid.uuid4()),
            )
            ta.verify_order_payment(pid, verified_by=str(uuid.uuid4()))
            status = ta.get_order_payment_status(order_id)
        self.assertEqual(status["payment_status"], "paid")
        self.assertTrue(status["fully_paid"])

    def test_duplicate_payment_idempotency_key_returns_same_line(self):
        order_id = self._make_order()
        key = str(uuid.uuid4())
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            first = ta.record_order_payment(
                order_id, amount=Decimal("100"), idempotency_key=key,
            )
            second = ta.record_order_payment(
                order_id, amount=Decimal("100"), idempotency_key=key,
            )
        self.assertEqual(first, second)

    # -- isolation --------------------------------------------------------------

    def test_tenant_cannot_see_other_tenant_orders(self):
        order_id = self._make_order()
        with tenant_admin(TENANT_TWO_ID, app_backend_test_url()) as ta:
            self.assertIsNone(ta.get_order(order_id))

    def test_different_tenant_cannot_confirm_foreign_order(self):
        order_id = self._make_order()
        with tenant_admin(TENANT_TWO_ID, app_backend_test_url()) as ta:
            with self.assertRaises(ValueError):
                ta.confirm_order(order_id)


if __name__ == "__main__":
    unittest.main()




class OrderOwnershipLookupTests(SaasDatabaseTestCase):
    """``get_order_by_number`` can bind a lookup to the sender's WhatsApp identity.

    This is the database half of the privacy guarantee: the SQL predicate, not the tool
    layer, is what makes another customer's order unreadable.
    """

    def _make_owned_order(self, wa_id: str | None) -> str:
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            order_id = ta.create_order(customer_name="Owner", whatsapp_wa_id=wa_id)
            ta.add_order_item(
                order_id, name="Chocolate Cake 1kg",
                quantity=1, unit_price=Decimal("1000"),
            )
            return ta.get_order(order_id)["order_number"]

    def test_the_owner_can_read_their_order(self):
        number = self._make_owned_order("254700111222")
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            row = ta.get_order_by_number(number, whatsapp_wa_id="254700111222")
        self.assertIsNotNone(row)
        self.assertEqual(row["order_number"], number)

    def test_another_whatsapp_number_reads_nothing(self):
        number = self._make_owned_order("254700111333")
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            self.assertIsNone(
                ta.get_order_by_number(number, whatsapp_wa_id="254700999888")
            )

    def test_an_order_without_a_whatsapp_identity_is_not_visible_to_a_customer(self):
        number = self._make_owned_order(None)
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            self.assertIsNone(
                ta.get_order_by_number(number, whatsapp_wa_id="254700111444")
            )

    def test_staff_lookup_without_a_binding_still_works(self):
        number = self._make_owned_order("254700111555")
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            self.assertIsNotNone(ta.get_order_by_number(number))

    def test_the_customer_order_list_is_scoped_to_the_sender(self):
        # Unique per run: orders persist between local runs, so a fixed identity would
        # accumulate rows and make this assertion pass for the wrong reason.
        wa_id = "2547" + uuid.uuid4().hex[:9]
        self._make_owned_order(wa_id)
        self._make_owned_order("2547" + uuid.uuid4().hex[:9])
        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as ta:
            rows = ta.list_orders_by_whatsapp(wa_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            set(rows[0]), {"id", "order_number", "status", "total", "amount_paid", "created_at"}
        )

