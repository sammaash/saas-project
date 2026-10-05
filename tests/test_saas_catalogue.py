"""PostgreSQL integration tests for the tenant-scoped catalogue service."""

from __future__ import annotations

import unittest

try:
    from .saas_test_support import (
        TENANT_ONE_ID,
        TENANT_TWO_ID,
        SaasDatabaseTestCase,
        app_backend_test_url,
    )
except ImportError:
    from saas_test_support import (  # type: ignore[no-redef]
        TENANT_ONE_ID,
        TENANT_TWO_ID,
        SaasDatabaseTestCase,
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


if __name__ == "__main__":
    unittest.main()
