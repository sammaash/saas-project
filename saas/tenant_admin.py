"""Tenant-scoped administration data access (products, settings, members, channel reads).

Every operation runs through ``tenant_transaction`` as ``app_backend`` with the tenant
context set by the server, so RLS confines all reads and writes to that one tenant. The
caller never supplies a tenant id inside a row: every statement filters on
``app.current_tenant_id()`` and every insert takes the tenant from it.

Channels are read-only here and platform-attributed audit events are not written here:
both go through the ``app.platform_*`` functions (see ``saas.platform``).
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from typing import Any, Iterator

from .tenant_context import tenant_transaction

SETTINGS_JSON_COLUMNS = ("business_rules", "bot_persona", "business_hours", "configuration")
PRODUCT_FIELDS = ("name", "description", "category", "sort_order", "attributes", "is_active")
VARIANT_FIELDS = (
    "code", "name", "price", "attributes", "sort_order", "is_active",
    "sku", "availability", "stock_quantity", "reorder_level",
)


def _jsonb(value: Any) -> Any:
    from psycopg.types.json import Jsonb  # noqa: PLC0415  (lazy optional dependency)

    return Jsonb(value)


@contextlib.contextmanager
def tenant_admin(tenant_id: str, url: str | None = None) -> Iterator["TenantAdmin"]:
    with tenant_transaction(tenant_id, url) as cursor:
        yield TenantAdmin(cursor)


class TenantAdmin:
    def __init__(self, cursor: Any):
        self._cursor = cursor

    # -- helpers -------------------------------------------------------------
    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        self._cursor.execute(sql, params)
        columns = [column.name for column in self._cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in self._cursor.fetchall()]

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _assignments(values: dict[str, Any], allowed: tuple[str, ...]) -> tuple[str, list[Any]]:
        """Build ``col = %s`` pairs from a whitelist; column names never come from input."""
        unknown = set(values) - set(allowed)
        if unknown:
            raise ValueError(f"Unsupported fields: {sorted(unknown)}")
        columns = [name for name in allowed if name in values]
        if not columns:
            raise ValueError("No fields to update.")
        params = [_jsonb(values[c]) if c == "attributes" else values[c] for c in columns]
        return ", ".join(f"{c} = %s" for c in columns), params

    # -- tenant --------------------------------------------------------------
    def get_tenant(self) -> dict[str, Any] | None:
        return self._one(
            "select id, name, slug, status, created_at, updated_at from public.tenants "
            "where id = app.current_tenant_id()"
        )

    def rename(self, name: str) -> None:
        self._cursor.execute(
            "update public.tenants set name = %s where id = app.current_tenant_id()", (name,)
        )

    # -- settings ------------------------------------------------------------
    def get_settings(self) -> dict[str, Any] | None:
        return self._one(
            "select timezone, currency, business_rules, bot_persona, business_hours, "
            "configuration, catalogue_version, updated_at from public.tenant_settings "
            "where tenant_id = app.current_tenant_id()"
        )

    def update_settings(
        self,
        timezone: str | None = None,
        currency: str | None = None,
        **documents: dict[str, Any],
    ) -> None:
        unknown = set(documents) - set(SETTINGS_JSON_COLUMNS)
        if unknown:
            raise ValueError(f"Unsupported settings documents: {sorted(unknown)}")
        sets, params = [], []
        if timezone is not None:
            sets.append("timezone = %s")
            params.append(timezone)
        if currency is not None:
            sets.append("currency = %s")
            params.append(currency)
        for column in SETTINGS_JSON_COLUMNS:
            if column in documents:
                sets.append(f"{column} = %s")
                params.append(_jsonb(documents[column]))
        if not sets:
            return
        self._cursor.execute(
            f"update public.tenant_settings set {', '.join(sets)} "
            "where tenant_id = app.current_tenant_id()",
            tuple(params),
        )

    # -- catalogue -----------------------------------------------------------
    def list_catalogue(self) -> list[dict[str, Any]]:
        products = self._all(
            "select id, name, description, category, sort_order, attributes, is_active "
            "from public.products where tenant_id = app.current_tenant_id() "
            "order by sort_order, name"
        )
        variants = self._all(
            "select id, product_id, code, name, price, attributes, sort_order, is_active, "
            "sku, availability, stock_quantity, reorder_level "
            "from public.product_variants where tenant_id = app.current_tenant_id() "
            "order by sort_order, name"
        )
        by_product: dict[Any, list[dict[str, Any]]] = {}
        for variant in variants:
            by_product.setdefault(variant["product_id"], []).append(variant)
        for product in products:
            product["variants"] = by_product.get(product["id"], [])
        return products

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        return self._one(
            "select id, name, description, category, sort_order, attributes, is_active "
            "from public.products where id = %s and tenant_id = app.current_tenant_id()",
            (product_id,),
        )

    def get_variant(self, variant_id: str) -> dict[str, Any] | None:
        return self._one(
            "select id, product_id, code, name, price, attributes, sort_order, is_active, "
            "sku, availability, stock_quantity, reorder_level "
            "from public.product_variants where id = %s and tenant_id = app.current_tenant_id()",
            (variant_id,),
        )

    def create_product(
        self,
        name: str,
        description: str | None = None,
        category: str | None = None,
        sort_order: int = 0,
        attributes: dict[str, Any] | None = None,
        is_active: bool = True,
    ) -> str:
        self._cursor.execute(
            "insert into public.products "
            "(tenant_id, name, description, category, sort_order, attributes, is_active) "
            "values (app.current_tenant_id(), %s, %s, %s, %s, %s, %s) returning id",
            (name, description, category, sort_order, _jsonb(attributes or {}), is_active),
        )
        return str(self._cursor.fetchone()[0])

    def update_product(self, product_id: str, **values: Any) -> bool:
        assignments, params = self._assignments(values, PRODUCT_FIELDS)
        self._cursor.execute(
            f"update public.products set {assignments} "
            "where id = %s and tenant_id = app.current_tenant_id()",
            (*params, product_id),
        )
        return self._cursor.rowcount == 1

    def create_variant(
        self,
        product_id: str,
        code: str,
        name: str,
        price: Decimal,
        attributes: dict[str, Any] | None = None,
        sort_order: int = 0,
        is_active: bool = True,
        sku: str | None = None,
        availability: str = "IN_STOCK",
        stock_quantity: int | None = None,
        reorder_level: int | None = None,
    ) -> str:
        # tenant_id comes from context; the composite FK rejects another tenant's product.
        self._cursor.execute(
            "insert into public.product_variants "
            "(tenant_id, product_id, code, name, price, attributes, sort_order, is_active, "
            "sku, availability, stock_quantity, reorder_level) "
            "values (app.current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "returning id",
            (
                product_id, code, name, price, _jsonb(attributes or {}), sort_order,
                is_active, sku or code, availability, stock_quantity, reorder_level,
            ),
        )
        return str(self._cursor.fetchone()[0])

    def update_variant(self, variant_id: str, **values: Any) -> bool:
        assignments, params = self._assignments(values, VARIANT_FIELDS)
        self._cursor.execute(
            f"update public.product_variants set {assignments} "
            "where id = %s and tenant_id = app.current_tenant_id()",
            (*params, variant_id),
        )
        return self._cursor.rowcount == 1

    # -- members -------------------------------------------------------------
    def list_members(self) -> list[dict[str, Any]]:
        return self._all(
            "select id, user_id, role, status, created_at from public.tenant_users "
            "where tenant_id = app.current_tenant_id() order by created_at"
        )

    def add_member(self, user_id: str, role: str, status: str = "active") -> str:
        self._cursor.execute(
            "insert into public.tenant_users (tenant_id, user_id, role, status) "
            "values (app.current_tenant_id(), %s, %s, %s) returning id",
            (user_id, role, status),
        )
        return str(self._cursor.fetchone()[0])

    # -- channels ------------------------------------------------------------
    def list_channels(self) -> list[dict[str, Any]]:
        return self._all(
            "select id, channel_type, phone_number_id, waba_id, display_phone_number, status, "
            "is_default, credentials_ref, updated_at from public.tenant_channels "
            "where tenant_id = app.current_tenant_id() order by is_default desc, created_at"
        )

    # -- subscription / plans ------------------------------------------------
    def list_plans(self) -> list[dict[str, Any]]:
        return self._all(
            "select id, code, name, monthly_price, currency, is_active from public.plans "
            "order by monthly_price, code"
        )

    def get_live_subscription(self) -> dict[str, Any] | None:
        return self._one(
            "select s.id, s.plan_id, p.code as plan_code, p.name as plan_name, s.status, "
            "s.billing_method, s.current_period_end, s.trial_ends_at "
            "from public.subscriptions s join public.plans p on p.id = s.plan_id "
            "where s.tenant_id = app.current_tenant_id() "
            "and s.status in ('trial', 'active', 'past_due', 'suspended')"
        )

    def set_subscription(self, plan_id: str, status: str) -> None:
        """Update the live subscription, or create one when none exists."""
        live = self.get_live_subscription()
        if live:
            self._cursor.execute(
                "update public.subscriptions set plan_id = %s, status = %s "
                "where id = %s and tenant_id = app.current_tenant_id()",
                (plan_id, status, live["id"]),
            )
        else:
            self._cursor.execute(
                "insert into public.subscriptions (tenant_id, plan_id, status, billing_method) "
                "values (app.current_tenant_id(), %s, %s, 'manual')",
                (plan_id, status),
            )

    # -- audit ---------------------------------------------------------------
    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._all(
            "select id, actor_user_id, actor_type, action, entity_type, entity_id, metadata, "
            "created_at from public.audit_log where tenant_id = app.current_tenant_id() "
            "order by created_at desc, id desc limit %s",
            (max(1, min(int(limit), 500)),),
        )
