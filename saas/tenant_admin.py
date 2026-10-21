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

    def lookup_price(
        self, product_name: str, weight_kg: int
    ) -> dict[str, Any] | None:
        """Resolve a catalogue price for a product name and weight variant.

        Prices are read from the persisted catalogue only -- never from model
        input. Weight is matched against the variant's ``attributes`` JSONB
        (key ``weight_kg``) or the variant name when it ends with ``kg``.
        """
        name_pattern = f"%{product_name.strip()}%"
        products = self._all(
            "select id, name, description, category, sort_order, attributes, is_active "
            "from public.products "
            "where tenant_id = app.current_tenant_id() "
            "and is_active = true "
            "and name ILIKE %s "
            "order by sort_order, name",
            (name_pattern,),
        )
        if not products:
            return None

        # Collect all variant ids for the matched products.
        product_ids = [str(p["id"]) for p in products]
        placeholders = ", ".join(["%s"] * len(product_ids))
        variants = self._all(
            f"select id, product_id, code, name, price, attributes, sort_order, is_active, "
            f"sku, availability, stock_quantity, reorder_level "
            f"from public.product_variants "
            f"where tenant_id = app.current_tenant_id() "
            f"and product_id in ({placeholders}) "
            f"and is_active = true "
            f"order by sort_order, name",
            tuple(product_ids),
        )

        weight_kg_int = int(weight_kg)
        best: dict[str, Any] | None = None

        for variant in variants:
            attributes = variant.get("attributes") or {}
            variant_weight = attributes.get("weight_kg")
            if variant_weight is None:
                # Try parsing weight from the variant name (e.g. "5 kg", "5kg").
                variant_name = str(variant.get("name", ""))
                import re as _re

                match = _re.search(r"(\d+)\s*kg\b", variant_name, _re.I)
                if match:
                    variant_weight = int(match.group(1))
            if variant_weight is not None and int(variant_weight) == weight_kg_int:
                product = next(
                    (p for p in products if str(p["id"]) == str(variant["product_id"])),
                    None,
                )
                if product is None:
                    continue
                return {
                    "product": {
                        "id": str(product["id"]),
                        "name": product["name"],
                        "description": product.get("description"),
                        "category": product.get("category"),
                    },
                    "variant": {
                        "id": str(variant["id"]),
                        "code": variant["code"],
                        "name": variant["name"],
                        "price": variant["price"],
                        "sku": variant.get("sku"),
                        "availability": variant.get("availability"),
                        "stock_quantity": variant.get("stock_quantity"),
                    },
                }

        # Fallback: return the first active variant of the best-matching product
        # when no exact weight match exists, so callers can surface a close option.
        if variants:
            product = next(
                (p for p in products if str(p["id"]) == str(variants[0]["product_id"])),
                None,
            )
            if product is not None:
                best = {
                    "product": {
                        "id": str(product["id"]),
                        "name": product["name"],
                        "description": product.get("description"),
                        "category": product.get("category"),
                    },
                    "variant": {
                        "id": str(variants[0]["id"]),
                        "code": variants[0]["code"],
                        "name": variants[0]["name"],
                        "price": variants[0]["price"],
                        "sku": variants[0].get("sku"),
                        "availability": variants[0].get("availability"),
                        "stock_quantity": variants[0].get("stock_quantity"),
                    },
                }

        return best

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

    # -- orders ----------------------------------------------------------------
    # Generic tenant-aware order lifecycle. All money is stored as numeric and
    # computed server-side; the LLM is never authoritative for amounts or
    # status. Order acceptance, deposit verification and full payment are
    # distinct states tracked on the order row and the order_payments ledger.
    ORDER_STATUSES = (
        "draft", "pending_payment", "confirmed", "in_progress",
        "ready", "completed", "cancelled",
    )
    ORDER_PAYMENT_STATUSES = ("unpaid", "awaiting_final", "paid", "overdue", "refunded")
    ORDER_ITEM_TYPES = ("product", "add_on", "delivery", "discount")
    PAYMENT_KINDS = ("payment", "refund")
    PAYMENT_ENTRY_STATUSES = ("recorded", "verified", "rejected")

    def allocate_order_number(self, prefix: str = "ORD") -> str:
        """Claim the next collision-safe order number for this tenant.

        The UPDATE ... RETURNING runs inside the caller's tenant transaction,
        so concurrent callers cannot receive the same number; the unique
        (tenant_id, order_number) constraint on orders backstops it.
        """
        cleaned = (prefix or "ORD").strip().upper()
        if not cleaned.isalnum() or len(cleaned) > 10:
            raise ValueError("Prefix must be 1-10 letters/digits.")
        self._cursor.execute(
            "insert into public.tenant_order_sequences (tenant_id, prefix, next_value) "
            "values (app.current_tenant_id(), %s, 2) "
            "on conflict (tenant_id) do update set "
            "prefix = excluded.prefix, "
            "next_value = public.tenant_order_sequences.next_value + 1 "
            "returning prefix, next_value - 1",
            (cleaned,),
        )
        row = self._cursor.fetchone()
        return f"{row[0]}-{int(row[1]):04d}"

    def create_order(
        self,
        *,
        order_number: str | None = None,
        number_prefix: str = "ORD",
        idempotency_key: str | None = None,
        customer_name: str | None = None,
        customer_phone: str | None = None,
        whatsapp_wa_id: str | None = None,
        currency: str = "KES",
        fulfilment: str | None = None,
        required_date: str | None = None,
        delivery_location: str | None = None,
        delivery_distance_km: Decimal | None = None,
        created_by_user_id: str | None = None,
    ) -> str:
        """Create a draft order. Totals start at zero and are built by items."""
        number = (order_number or "").strip() or self.allocate_order_number(number_prefix)
        if fulfilment is not None and fulfilment not in ("pickup", "delivery"):
            raise ValueError("Fulfilment must be 'pickup' or 'delivery'.")
        self._cursor.execute(
            "insert into public.orders (tenant_id, order_number, idempotency_key, "
            "customer_name, customer_phone, whatsapp_wa_id, status, currency, "
            "fulfilment, required_date, delivery_location, delivery_distance_km, "
            "created_by_user_id) "
            "values (app.current_tenant_id(), %s, %s, %s, %s, %s, 'draft', %s, "
            "%s, %s::date, %s, %s, %s) returning id",
            (
                number, idempotency_key, customer_name, customer_phone,
                whatsapp_wa_id, currency, fulfilment, required_date,
                delivery_location, delivery_distance_km, created_by_user_id,
            ),
        )
        return str(self._cursor.fetchone()[0])

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        return self._one(
            "select * from public.orders "
            "where id = %s and tenant_id = app.current_tenant_id()",
            (order_id,),
        )

    def get_order_draft_id_by_whatsapp(self, whatsapp_wa_id: str) -> str | None:
        """Return the id of the most recent draft order for this WhatsApp user."""
        row = self._one(
            "select id from public.orders "
            "where whatsapp_wa_id = %s and tenant_id = app.current_tenant_id() "
            "and status = 'draft' "
            "order by created_at desc limit 1",
            (whatsapp_wa_id,),
        )
        return str(row["id"]) if row else None

    def get_order_by_number(self, order_number: str) -> dict[str, Any] | None:
        return self._one(
            "select * from public.orders "
            "where order_number = %s and tenant_id = app.current_tenant_id()",
            (order_number,),
        )

    def get_order_by_number_for_whatsapp(
        self, order_number: str, whatsapp_wa_id: str
    ) -> dict[str, Any] | None:
        """An order only when it belongs to this WhatsApp identity in this tenant.

        The customer-facing menu must never confirm that an order number exists for
        somebody else: a missing number and another customer's number return the same
        ``None``. RLS confines the statement to the tenant; the wa_id predicate confines
        it to the sender.
        """
        if not (whatsapp_wa_id or "").strip():
            return None
        return self._one(
            "select * from public.orders "
            "where order_number = %s and whatsapp_wa_id = %s "
            "and tenant_id = app.current_tenant_id()",
            (order_number, whatsapp_wa_id),
        )

    def update_order(self, order_id: str, **values: Any) -> bool:
        """Update draft details. Column names come from a whitelist, never input."""
        allowed = (
            "customer_name", "customer_phone", "whatsapp_wa_id", "currency",
            "fulfilment", "required_date", "delivery_location",
            "delivery_distance_km", "delivery_needs_human_confirmation",
            "payment_deadline",
        )
        unknown = set(values) - set(allowed)
        if unknown:
            raise ValueError(f"Unsupported fields: {sorted(unknown)}")
        columns = [name for name in allowed if name in values]
        if not columns:
            raise ValueError("No fields to update.")
        if "fulfilment" in values and values["fulfilment"] not in ("pickup", "delivery"):
            raise ValueError("Fulfilment must be 'pickup' or 'delivery'.")
        self._cursor.execute(
            f"update public.orders set {', '.join(f'{c} = %s' for c in columns)} "
            "where id = %s and tenant_id = app.current_tenant_id() "
            "and status = 'draft'",
            (*[values[c] for c in columns], order_id),
        )
        return self._cursor.rowcount == 1

    def add_order_item(
        self,
        order_id: str,
        *,
        item_type: str = "product",
        product_id: str | None = None,
        variant_id: str | None = None,
        sku: str | None = None,
        name: str,
        quantity: int = 1,
        unit_price: Decimal = Decimal("0"),
        attributes: dict[str, Any] | None = None,
    ) -> int:
        """Append a priced line to a draft order and rebuild its totals."""
        if item_type not in self.ORDER_ITEM_TYPES:
            raise ValueError(f"Unknown item type: {item_type!r}.")
        if not name or not name.strip():
            raise ValueError("Item name is required.")
        if int(quantity) < 1:
            raise ValueError("Quantity must be at least 1.")
        if Decimal(str(unit_price)) < 0:
            raise ValueError("Unit price must not be negative.")
        self._cursor.execute(
            "insert into public.order_items (tenant_id, order_id, item_type, "
            "product_id, variant_id, sku, name, quantity, unit_price, line_total, attributes) "
            "select app.current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s * %s, %s "
            "from public.orders "
            "where id = %s and tenant_id = app.current_tenant_id() "
            "and status = 'draft' "
            "returning id",
            (
                order_id, item_type, product_id, variant_id, sku, name.strip(),
                int(quantity), Decimal(str(unit_price)), int(quantity),
                Decimal(str(unit_price)), _jsonb(attributes or {}),
                order_id,
            ),
        )
        row = self._cursor.fetchone()
        if row is None:
            raise ValueError("Order not found or is not a draft.")
        self._rebuild_order_totals(order_id)
        return int(row[0])

    def list_order_items(self, order_id: str) -> list[dict[str, Any]]:
        return self._all(
            "select id, item_type, product_id, variant_id, sku, name, quantity, "
            "unit_price, line_total, attributes from public.order_items "
            "where order_id = %s and tenant_id = app.current_tenant_id() "
            "order by id",
            (order_id,),
        )

    def quote_order(self, order_id: str) -> dict[str, Any]:
        """Return the server-calculated quote for the customer's final review."""
        order = self.get_order(order_id)
        if order is None:
            raise ValueError("Order not found.")
        return {
            "order_id": str(order["id"]),
            "order_number": order["order_number"],
            "status": order["status"],
            "currency": order["currency"],
            "items": self.list_order_items(order_id),
            "subtotal": order["subtotal"],
            "discount": order["discount"],
            "delivery_fee": order["delivery_fee"],
            "total": order["total"],
            "deposit_rate": order["deposit_rate"],
            "required_deposit": order["required_deposit"],
            "amount_paid": order["amount_paid"],
            "outstanding_balance": order["outstanding_balance"],
            "payment_status": order["payment_status"],
        }

    def confirm_order(self, order_id: str) -> dict[str, Any]:
        """Accept the complete final quote: draft -> pending_payment."""
        order = self.get_order(order_id)
        if order is None:
            raise ValueError("Order not found.")
        if order["status"] == "pending_payment":
            return self.quote_order(order_id)
        if order["status"] != "draft":
            raise ValueError(f"Cannot confirm order in status {order['status']!r}.")
        if Decimal(str(order["total"])) <= 0:
            raise ValueError("An order must have a non-zero total before confirmation.")
        if not (order.get("customer_name") or order.get("customer_phone")):
            raise ValueError("Customer name or phone is required before confirmation.")
        self._cursor.execute(
            "update public.orders set status = 'pending_payment', "
            "payment_status = 'unpaid', confirmed_at = now() "
            "where id = %s and tenant_id = app.current_tenant_id() "
            "and status = 'draft'",
            (order_id,),
        )
        if self._cursor.rowcount != 1:
            raise ValueError("Order could not be confirmed.")
        return self.quote_order(order_id)

    def cancel_order(self, order_id: str, reason: str) -> bool:
        """Cancel a draft (or unpaid pending) order. Paid orders are protected."""
        if not reason or not reason.strip():
            raise ValueError("A cancellation reason is required.")
        order = self.get_order(order_id)
        if order is None:
            raise ValueError("Order not found.")
        if order["status"] not in ("draft", "pending_payment"):
            raise ValueError(f"Order cannot be cancelled from status {order['status']!r}.")
        if Decimal(str(order["amount_paid"])) > 0 or order["payment_status"] == "paid":
            raise ValueError("Paid orders cannot be cancelled; record a refund instead.")
        self._cursor.execute(
            "update public.orders set status = 'cancelled', cancelled_at = now(), "
            "cancellation_reason = %s "
            "where id = %s and tenant_id = app.current_tenant_id() "
            "and status in ('draft', 'pending_payment')",
            (reason.strip(), order_id),
        )
        return self._cursor.rowcount == 1

    def _rebuild_order_totals(self, order_id: str) -> None:
        """Recompute subtotal/total/deposit/balance from persisted item lines."""
        self._cursor.execute(
            "update public.orders o set "
            "subtotal = coalesce(("
            "  select sum(line_total) from public.order_items i "
            "  where i.tenant_id = o.tenant_id and i.order_id = o.id "
            "  and i.item_type in ('product', 'add_on', 'delivery')"
            "), 0), "
            "discount = coalesce(("
            "  select sum(line_total) from public.order_items i "
            "  where i.tenant_id = o.tenant_id and i.order_id = o.id "
            "  and i.item_type = 'discount'"
            "), 0) "
            "where o.id = %s and o.tenant_id = app.current_tenant_id()",
            (order_id,),
        )
        self._cursor.execute(
            "update public.orders set "
            "total = greatest(subtotal - discount, 0), "
            "delivery_fee = coalesce(("
            "  select sum(line_total) from public.order_items "
            "  where tenant_id = app.current_tenant_id() and order_id = %s "
            "  and item_type = 'delivery'"
            "), 0), "
            "required_deposit = greatest(subtotal - discount, 0) "
            "* coalesce(deposit_rate, 0) "
            "where id = %s and tenant_id = app.current_tenant_id()",
            (order_id, order_id),
        )
        self._cursor.execute(
            "update public.orders set "
            "outstanding_balance = greatest(total - amount_paid, 0) "
            "where id = %s and tenant_id = app.current_tenant_id()",
            (order_id,),
        )

    # -- order payments: append-friendly ledger. Verified rows only move
    # balances, via _reconcile_order_payment(). Only staff verify, never LLM.

    def record_order_payment(
        self,
        order_id: str,
        *,
        kind: str = "payment",
        amount: Decimal,
        method: str | None = None,
        reference: str | None = None,
        idempotency_key: str | None = None,
        recorded_by: str | None = None,
        note: str | None = None,
    ) -> int:
        if kind not in self.PAYMENT_KINDS:
            raise ValueError(f"Unknown payment kind: {kind!r}.")
        if Decimal(str(amount)) <= 0:
            raise ValueError("Payment amount must be greater than zero.")
        order = self.get_order(order_id)
        if order is None:
            raise ValueError("Order not found.")
        if order["status"] == "cancelled":
            raise ValueError("Cannot record a payment on a cancelled order.")
        self._cursor.execute(
            "insert into public.order_payments (tenant_id, order_id, kind, amount, "
            "method, reference, idempotency_key, status, recorded_by, note) "
            "values (app.current_tenant_id(), %s, %s, %s, %s, %s, %s, "
            "'recorded', %s, %s) "
            "on conflict (tenant_id, idempotency_key) where idempotency_key is not null "
            "do nothing returning id",
            (
                order_id, kind, Decimal(str(amount)), method, reference,
                idempotency_key, recorded_by, note,
            ),
        )
        row = self._cursor.fetchone()
        if row is None:
            existing = self._one(
                "select id from public.order_payments "
                "where order_id = %s and tenant_id = app.current_tenant_id() "
                "and idempotency_key = %s",
                (order_id, idempotency_key),
            )
            if existing is None:
                raise ValueError("Payment could not be recorded.")
            return int(existing["id"])
        self._reconcile_order_payment(order_id)
        return int(row[0])

    def verify_order_payment(
        self, payment_id: int, *, verified_by: str, status: str = "verified"
    ) -> bool:
        """Staff-only: verify (or reject) a recorded payment line."""
        if status not in ("verified", "rejected"):
            raise ValueError("Status must be 'verified' or 'rejected'.")
        if not verified_by:
            raise ValueError("A verifying staff user is required.")
        self._cursor.execute(
            "update public.order_payments set status = %s, verified_by = %s, "
            "verified_at = case when %s = 'verified' then now() else null end "
            "where id = %s and tenant_id = app.current_tenant_id() "
            "and status = 'recorded'",
            (status, verified_by, status, payment_id),
        )
        if self._cursor.rowcount != 1:
            return False
        row = self._one(
            "select order_id from public.order_payments "
            "where id = %s and tenant_id = app.current_tenant_id()",
            (payment_id,),
        )
        if row:
            self._reconcile_order_payment(str(row["order_id"]))
        return True

    def list_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        return self._all(
            "select id, kind, amount, method, reference, idempotency_key, status, "
            "recorded_by, verified_by, verified_at, note, created_at "
            "from public.order_payments "
            "where order_id = %s and tenant_id = app.current_tenant_id() "
            "order by id",
            (order_id,),
        )

    def get_order_payment_status(self, order_id: str) -> dict[str, Any]:
        order = self.get_order(order_id)
        if order is None:
            raise ValueError("Order not found.")
        return {
            "order_id": str(order["id"]),
            "order_number": order["order_number"],
            "status": order["status"],
            "total": order["total"],
            "required_deposit": order["required_deposit"],
            "amount_paid": order["amount_paid"],
            "outstanding_balance": order["outstanding_balance"],
            "payment_status": order["payment_status"],
            "payment_deadline": order["payment_deadline"],
            "deposit_satisfied": Decimal(str(order["amount_paid"]))
            >= Decimal(str(order["required_deposit"])),
            "fully_paid": Decimal(str(order["amount_paid"]))
            >= Decimal(str(order["total"])),
        }

    def _reconcile_order_payment(self, order_id: str) -> None:
        """Derive balances from verified ledger rows only. Never LLM input."""
        self._cursor.execute(
            "update public.orders o set "
            "amount_paid = coalesce(("
            "  select sum(case when kind = 'payment' then amount else -amount end) "
            "  from public.order_payments p "
            "  where p.tenant_id = o.tenant_id and p.order_id = o.id "
            "  and p.status = 'verified'"
            "), 0) "
            "where o.id = %s and o.tenant_id = app.current_tenant_id()",
            (order_id,),
        )
        self._cursor.execute(
            "update public.orders set "
            "outstanding_balance = greatest(total - amount_paid, 0), "
            "payment_status = case "
            "when amount_paid >= total and total > 0 then 'paid' "
            "when amount_paid > 0 then 'awaiting_final' "
            "else payment_status end "
            "where id = %s and tenant_id = app.current_tenant_id() "
            "and payment_status <> 'refunded'",
            (order_id,),
        )

    # -- audit ---------------------------------------------------------------

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._all(
            "select id, actor_user_id, actor_type, action, entity_type, entity_id, metadata, "
            "created_at from public.audit_log where tenant_id = app.current_tenant_id() "
            "order by created_at desc, id desc limit %s",
            (max(1, min(int(limit), 500)),),
        )
