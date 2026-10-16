"""PostgreSQL integration tests for the tenant-scoped catalogue service."""

from __future__ import annotations

import os
import unittest
from decimal import Decimal
from unittest.mock import patch

try:
    from .saas_test_support import (
        TENANT_ONE_ID,
        TENANT_TWO_ID,
        SaasDatabaseTestCase,
        _connect,
        app_backend_test_url,
    )
except ImportError:
    from saas_test_support import (  # type: ignore[no-redef]
        TENANT_ONE_ID,
        TENANT_TWO_ID,
        SaasDatabaseTestCase,
        _connect,
        app_backend_test_url,
    )

EXPECTED_CINDY_PRICES = {
    "Vanilla": {1: 2500.0, 2: 4200.0, 3: 5600.0},
    "Carrot": {1: 2500.0, 2: 4200.0, 3: 5600.0},
    "Lemon": {1: 2500.0, 2: 4200.0, 3: 5600.0},
    "Orange": {1: 2500.0, 2: 4200.0, 3: 5600.0},
    "White Forest": {1: 3100.0, 2: 4700.0, 3: 6000.0},
    "Black Forest": {1: 3100.0, 2: 4700.0, 3: 6000.0},
    "Blueberry": {1: 2700.0, 2: 4550.0, 3: 5800.0},
    "Caramel": {1: 3100.0, 2: 4700.0, 3: 6000.0},
    "Rainbow": {1: 3750.0, 2: 5000.0, 3: 6700.0},
    "Red Velvet": {1: 3100.0, 2: 4700.0, 3: 6000.0},
    "Chocolate": {1: 3100.0, 2: 4700.0, 3: 6000.0},
}


class CatalogueServiceTests(SaasDatabaseTestCase):
    def test_cindy_catalogue_has_expected_products_variants_and_prices(self):
        from saas.catalogue import CatalogueService

        service = CatalogueService(TENANT_ONE_ID, app_backend_test_url())
        products = service.list_active_products()
        actual = {
            product["name"]: {
                int(variant["attributes"]["weight_kg"]): variant["price"]
                for variant in product["variants"]
            }
            for product in products
        }

        self.assertEqual(len(products), 11)
        self.assertEqual(sum(len(product["variants"]) for product in products), 33)
        self.assertEqual(actual, EXPECTED_CINDY_PRICES)

    def test_catalogue_lookups_are_tenant_scoped(self):
        from saas.catalogue import CatalogueService

        database_url = app_backend_test_url()
        cindy = CatalogueService(TENANT_ONE_ID, database_url)
        other_tenant = CatalogueService(TENANT_TWO_ID, database_url)

        vanilla = cindy.resolve_product_name("  vanilla ")
        self.assertIsNotNone(vanilla)
        self.assertEqual(vanilla["name"], "Vanilla")
        price = cindy.lookup_price("vanilla", 2)
        self.assertIsNotNone(price)
        self.assertEqual(price["unit_price"], 4200.0)
        self.assertIsNone(cindy.lookup_price("Not a product", 1))
        self.assertEqual(other_tenant.list_active_products(), [])
        self.assertIsNone(other_tenant.resolve_product_name("Vanilla"))


def _delete_committed(sql: str, params: tuple) -> None:
    """Delete fixture rows and COMMIT (``as_superuser`` rolls back by design)."""
    conn = _connect()
    with conn, conn.cursor() as cursor:
        cursor.execute(sql, params)


class TenantAdminPriceResolutionTests(SaasDatabaseTestCase):
    """The bot's catalogue path must price the weight the customer asked for.

    Regression: ``TenantAdmin.lookup_price`` compared ``str(product id)`` with the
    ``uuid.UUID`` returned for ``variant.product_id``. Both the weight match and the
    documented "first active variant" fallback therefore failed, so every catalogue
    add-to-cart was refused with "Could not find '<product>' at <n>kg".
    """

    def test_lookup_price_resolves_every_weight_variant(self):
        from saas.tenant_admin import tenant_admin

        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as admin:
            for product, prices in EXPECTED_CINDY_PRICES.items():
                for weight, price in prices.items():
                    with self.subTest(product=product, weight=weight):
                        found = admin.lookup_price(product, weight)
                        self.assertIsNotNone(
                            found, f"{product} at {weight}kg did not resolve"
                        )
                        self.assertEqual(found["product"]["name"], product)
                        self.assertEqual(float(found["variant"]["price"]), price)
                        self.assertEqual(found["variant"]["availability"], "IN_STOCK")

    def test_lookup_price_is_case_insensitive_and_scoped(self):
        from saas.tenant_admin import tenant_admin

        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as admin:
            found = admin.lookup_price("  rainbow ", 1)
            self.assertIsNotNone(found)
            self.assertEqual(found["product"]["name"], "Rainbow")
            self.assertEqual(float(found["variant"]["price"]), 3750.0)
            # An unknown product has no variant to fall back to.
            self.assertIsNone(admin.lookup_price("Not a product", 1))

        # Tenant 002 has no catalogue, so the same lookup must resolve to nothing.
        with tenant_admin(TENANT_TWO_ID, app_backend_test_url()) as admin:
            self.assertIsNone(admin.lookup_price("Rainbow", 1))

    def test_lookup_price_falls_back_to_the_variant_name(self):
        """Imported catalogues carry no ``weight_kg`` attribute, only a variant name.

        The dashboard import writes variant names such as "2 kg" and no attributes, so
        the name fallback is the path a freshly onboarded tenant depends on.
        """
        from saas.tenant_admin import tenant_admin

        with tenant_admin(TENANT_ONE_ID, app_backend_test_url()) as admin:
            product_id = admin.create_product(
                name="Import Fallback Cake", category="standard-cake"
            )
            variant_id = admin.create_variant(
                product_id, code="IMP-FALLBACK-5KG", name="5 kg", price=Decimal("9000.00")
            )
            try:
                found = admin.lookup_price("Import Fallback Cake", 5)
                self.assertIsNotNone(found)
                self.assertEqual(found["variant"]["id"], str(variant_id))
                self.assertEqual(float(found["variant"]["price"]), 9000.0)
            finally:
                admin._cursor.execute(
                    "delete from public.product_variants where id = %s", (variant_id,)
                )
                admin._cursor.execute(
                    "delete from public.products where id = %s", (product_id,)
                )

    def test_update_order_draft_tool_prices_from_the_catalogue(self):
        """End to end through the real tool and the real tenant-scoped admin."""
        from saas.tools import ToolContext, UpdateOrderDraftTool

        customer_key = "+254799000111"
        order_id = None
        try:
            with patch.dict(os.environ, {"SAAS_DATABASE_URL": app_backend_test_url() or ""}):
                result = UpdateOrderDraftTool().execute(
                    ToolContext(tenant_id=TENANT_ONE_ID, customer_key=customer_key),
                    {
                        "product_name": "Rainbow",
                        "weight_kg": 3,
                        "quantity": 2,
                        "customer_name": "Catalogue Test",
                        "fulfilment": "pickup",
                    },
                )
            self.assertTrue(result.ok, result.error)
            order = result.data["order"]
            order_id = order["id"]
            # 3 kg Rainbow is 6700.00; two of them is 13400.00 -- the price of the
            # requested weight, not the first variant (1 kg, 3750.00).
            self.assertEqual(order["total"], 13400.0)
            self.assertEqual(order["items"][0]["unit_price"], 6700.0)
        finally:
            if order_id:
                _delete_committed(
                    "delete from public.order_items where order_id = %s", (order_id,)
                )
                _delete_committed("delete from public.orders where id = %s", (order_id,))


if __name__ == "__main__":
    unittest.main()
