"""Tenant-scoped catalogue data access for the SaaS foundation.

This is a database service only. It does not provide an HTTP application or
WhatsApp integration; callers must pass a trusted tenant ID from their runtime.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .tenant_context import tenant_transaction


class CatalogueService:
    """Read active products and variants within one explicit tenant context."""

    def __init__(self, tenant_id: str, database_url: str | None = None):
        if not tenant_id:
            raise ValueError("tenant_id is required for catalogue access.")
        self.tenant_id = str(tenant_id)
        self.database_url = database_url

    @staticmethod
    def _row_dict(cursor: Any, row: tuple[Any, ...]) -> dict[str, Any]:
        columns = [column.name for column in cursor.description]
        data = dict(zip(columns, row, strict=True))
        for name, value in data.items():
            if isinstance(value, Decimal):
                data[name] = float(value)
        return data

    def _fetchall(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with tenant_transaction(self.tenant_id, self.database_url) as cursor:
            cursor.execute(sql, parameters)
            rows = cursor.fetchall()
            return [self._row_dict(cursor, row) for row in rows]

    def list_active_products(self) -> list[dict[str, Any]]:
        """Return active products with active variants for this tenant only."""
        rows = self._fetchall(
            """
            select p.id as product_id, p.name as product_name, p.description, p.category,
                   p.attributes as product_attributes, p.sort_order as product_sort_order,
                   v.id as variant_id, v.code as variant_code, v.name as variant_name,
                   v.price, v.attributes as variant_attributes, v.sort_order as variant_sort_order
            from public.products p
            join public.product_variants v
              on v.product_id = p.id and v.tenant_id = p.tenant_id
            where p.tenant_id = app.current_tenant_id()
              and p.is_active and v.is_active
            order by p.sort_order, p.name, v.sort_order, v.name
            """
        )
        products: dict[str, dict[str, Any]] = {}
        for row in rows:
            product = products.setdefault(row["product_id"], {
                "id": row["product_id"], "name": row["product_name"],
                "description": row["description"], "category": row["category"],
                "attributes": row["product_attributes"], "variants": [],
            })
            product["variants"].append({
                "id": row["variant_id"], "code": row["variant_code"],
                "name": row["variant_name"], "price": row["price"],
                "attributes": row["variant_attributes"],
            })
        return list(products.values())

    def resolve_product_name(self, name: str) -> dict[str, Any] | None:
        """Resolve an active product case-insensitively within this tenant."""
        if not str(name).strip():
            return None
        rows = self._fetchall(
            """
            select id, name, description, category, attributes
            from public.products
            where tenant_id = app.current_tenant_id()
              and is_active
              and name_normalized = lower(btrim(%s))
            """,
            (str(name),),
        )
        return rows[0] if rows else None

    def resolve_variant(self, product_id: str, weight_kg: int | float) -> dict[str, Any] | None:
        """Resolve an active weight variant for a tenant-owned product."""
        rows = self._fetchall(
            """
            select id, product_id, code, name, price, attributes
            from public.product_variants
            where tenant_id = app.current_tenant_id()
              and product_id = %s
              and is_active
              and attributes ->> 'weight_kg' = %s
            """,
            (str(product_id), str(weight_kg)),
        )
        return rows[0] if rows else None

    def lookup_price(self, product_name: str, weight_kg: int | float) -> dict[str, Any] | None:
        """Resolve an active product and priced variant for this tenant."""
        product = self.resolve_product_name(product_name)
        if product is None:
            return None
        variant = self.resolve_variant(product["id"], weight_kg)
        if variant is None:
            return None
        return {"product": product, "variant": variant, "unit_price": variant["price"]}
