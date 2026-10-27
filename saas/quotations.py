"""Tenant-scoped quotations issued from WhatsApp customer requests.

All pricing is read from the persisted, tenant-scoped catalogue; nothing here
trusts a caller-supplied price. The rows written to ``public.quotations`` and
``public.quotation_lines`` are immutable snapshots: later catalogue edits do not
change an issued quotation.

Two supporting records keep the customer workflow honest:

* ``public.quotation_briefs`` holds the request while details are still being
  collected, so a follow-up message updates the same request instead of starting
  again. ``missing_fields`` is computed here, never by the model.
* ``public.quotation_deliveries`` records every attempt to send the PDF as a
  WhatsApp document *before* the send, so an ambiguous timeout is never reported
  as success and never retried blindly.

The module is free of ``platform_admin`` imports so the ``saas`` layer can use it
without a circular dependency.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Mapping, Sequence

from .tenant_context import tenant_transaction

MONEY = Decimal("0.01")
MAX_QUANTITY = 100000
MAX_VALID_DAYS = 365
MAX_SELECTIONS = 50
MAX_BRIEF_ITEMS = 20
BRIEF_STATUSES = ("collecting", "ready", "quoted", "handed_over")
DELIVERY_STATUSES = ("pending", "sent", "failed", "uncertain")

#: A customer asking for the document rather than a new price. Deliberately narrow:
#: "how much is..." must reach the pricing flow, not the PDF sender.
PDF_REQUEST_RE = re.compile(
    r"\b(?:send|resend|whatsapp|share|forward|email|get|download|need)\b"
    r"[^.?!]{0,40}\b(?:quotation|quote)\b[^.?!]{0,20}\b(?:pdf|document|copy|file)\b"
    r"|\b(?:quotation|quote)\s+(?:pdf|document)\b"
    r"|\bpdf\s+(?:of|for)\s+(?:my|the)\s+(?:quotation|quote)\b",
    re.IGNORECASE,
)


class QuotationError(ValueError):
    """A user-correctable quotation failure. The message is safe to show."""


class QuotationConflictError(QuotationError):
    """An idempotency key already belongs to a different customer identity."""


class QuotationNeedsStaff(QuotationError):
    """The request cannot be priced from the catalogue; a human must quote it."""

    def __init__(self, message: str, *, reason: str = "custom_order") -> None:
        super().__init__(message)
        self.reason = reason


def quotations_enabled(configuration: Any) -> bool:
    """True only for the explicit ``configuration.quotations.enabled: true`` opt-in."""
    if not isinstance(configuration, dict):
        return False
    quotations = configuration.get("quotations")
    return isinstance(quotations, dict) and quotations.get("enabled") is True


def is_pdf_request(text: Any) -> bool:
    """True when the customer is asking for an existing quotation document."""
    return bool(PDF_REQUEST_RE.search(str(text or "")))


def _database_url(target: Any) -> str | None:
    """Accept either a Settings-like object or a plain database URL."""
    if target is None or isinstance(target, str):
        return target
    return getattr(target, "app_database_url", None)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _row_dict(cursor: Any, row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip((column.name for column in cursor.description), row, strict=True))


def _text(value: Any, field: str, *, max_length: int, required: bool) -> str | None:
    if value is None:
        cleaned = ""
    elif isinstance(value, str):
        cleaned = value.strip()
    else:
        cleaned = str(value).strip()
    if "\x00" in cleaned:
        raise QuotationError(f"{field} contains an invalid character.")
    if not cleaned:
        if required:
            raise QuotationError(f"{field} is required.")
        return None
    if len(cleaned) > max_length:
        raise QuotationError(f"{field} must be at most {max_length} characters.")
    return cleaned


def _valid_days(configuration: dict[str, Any], value: Any) -> int:
    """Resolve the validity period, preserving the dashboard's blank/default behaviour."""
    if value is None or str(value).strip() == "":
        try:
            days = int(configuration.get("quotation_valid_days", 14))
        except (TypeError, ValueError):
            days = 14
        if not 1 <= days <= MAX_VALID_DAYS:
            days = 14
        return days
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        raise QuotationError("Validity must be a whole number.") from None
    if not 1 <= days <= MAX_VALID_DAYS:
        raise QuotationError(f"Validity must be between 1 and {MAX_VALID_DAYS} days.")
    return days


def _tax_configuration(configuration: Any) -> tuple[Decimal | None, str]:
    configuration = configuration if isinstance(configuration, dict) else {}
    raw = configuration.get("tax_rate")
    if raw is None or str(raw).strip() == "":
        return None, "Tax is not configured for this tenant."
    try:
        rate = Decimal(str(raw))
    except InvalidOperation:
        raise QuotationError("The tenant tax rate configuration is invalid.") from None
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise QuotationError("The tenant tax rate configuration must be between 0 and 100.")
    return rate, f"Tax calculated at {rate.normalize()}%."



def _normalize_selections(selections: Iterable[Sequence[Any]]) -> list[tuple[str, int]]:
    normalized: list[tuple[str, int]] = []
    for raw_variant_id, raw_quantity in selections:
        if raw_quantity is None or str(raw_quantity).strip() in {"", "0"}:
            continue
        try:
            variant_id = str(uuid.UUID(str(raw_variant_id).strip()))
        except (ValueError, AttributeError, TypeError):
            raise QuotationError("Variant must be a valid UUID.") from None
        try:
            quantity = int(str(raw_quantity).strip())
        except (TypeError, ValueError):
            raise QuotationError("Quantity must be a whole number.") from None
        if quantity < 1:
            raise QuotationError("Quantity must be at least 1.")
        if quantity > MAX_QUANTITY:
            raise QuotationError(f"Quantity cannot exceed {MAX_QUANTITY}.")
        normalized.append((variant_id, quantity))
    if not normalized:
        raise QuotationError("Select at least one catalogue item and quantity.")
    if len({variant_id for variant_id, _ in normalized}) != len(normalized):
        raise QuotationError("Each catalogue variant may appear only once.")
    if len(normalized) > MAX_SELECTIONS:
        raise QuotationError(f"A quotation can contain at most {MAX_SELECTIONS} items.")
    return normalized


def _fetch_quotation(
    cursor: Any, quotation_id: str, *, duplicate: bool = False
) -> dict[str, Any] | None:
    cursor.execute(
        """
        select q.id, q.quotation_number, q.customer_name, q.customer_contact,
               q.issue_date, q.expiry_date, q.currency, q.subtotal, q.tax_rate,
               q.tax_amount, q.total, q.tax_treatment, q.terms, q.status,
               q.source, q.customer_wa_id, q.idempotency_key, q.created_by_user_id,
               t.name as business_name
        from public.quotations q
        join public.tenants t on t.id = q.tenant_id
        where q.id = %s and q.tenant_id = app.current_tenant_id()
        """,
        (quotation_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    quotation = _row_dict(cursor, row)
    cursor.execute(
        """
        select sku, description, availability, quantity, unit_price, subtotal
        from public.quotation_lines
        where quotation_id = %s and tenant_id = app.current_tenant_id()
        order by id
        """,
        (quotation_id,),
    )
    quotation["lines"] = [_row_dict(cursor, item) for item in cursor.fetchall()]
    quotation["id"] = str(quotation["id"])
    if quotation.get("created_by_user_id") is not None:
        quotation["created_by_user_id"] = str(quotation["created_by_user_id"])
    quotation["duplicate"] = duplicate
    return quotation


def get_quotation(
    settings: Any,
    tenant_id: str,
    quotation_id: str,
    *,
    customer_wa_id: str | None = None,
) -> dict[str, Any] | None:
    """Return one tenant-scoped quotation, or None when it does not exist.

    ``customer_wa_id`` additionally scopes the lookup to the verified sender, so a
    customer can never read another customer's quotation from the same tenant.
    """
    try:
        quotation_id = str(uuid.UUID(str(quotation_id).strip()))
    except (ValueError, AttributeError, TypeError):
        raise QuotationError("Quotation is not a valid identifier.") from None
    with tenant_transaction(tenant_id, _database_url(settings)) as cursor:
        quotation = _fetch_quotation(cursor, quotation_id)
    if quotation is None:
        return None
    if customer_wa_id is not None and quotation.get("customer_wa_id") != customer_wa_id:
        return None
    return quotation


def latest_quotation_for_customer(
    settings: Any, tenant_id: str, customer_wa_id: str
) -> dict[str, Any] | None:
    """The newest issued WhatsApp quotation addressed to this verified sender."""
    wa_id = _text(customer_wa_id, "WhatsApp identity", max_length=200, required=True)
    with tenant_transaction(tenant_id, _database_url(settings)) as cursor:
        cursor.execute(
            """
            select id from public.quotations
            where tenant_id = app.current_tenant_id()
              and source = 'whatsapp'
              and customer_wa_id = %s
              and status = 'issued'
            order by created_at desc
            limit 1
            """,
            (wa_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _fetch_quotation(cursor, str(row[0]))


def _existing_by_idempotency(
    cursor: Any, source: str, idempotency_key: str | None
) -> tuple[str, str | None] | None:
    if not idempotency_key:
        return None
    cursor.execute(
        "select id, customer_wa_id from public.quotations "
        "where tenant_id = app.current_tenant_id() and source = %s "
        "and idempotency_key = %s",
        (source, idempotency_key),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return str(row[0]), (str(row[1]) if row[1] is not None else None)


def _system_audit(cursor: Any, quotation: dict[str, Any]) -> None:
    """Append a tenant-scoped system audit event in the same transaction."""
    from psycopg.types.json import Jsonb  # noqa: PLC0415  (lazy optional dependency)

    cursor.execute(
        "insert into public.audit_log "
        "(tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata) "
        "values (app.current_tenant_id(), null, 'system', %s, %s, %s, %s)",
        (
            "quotation.issued",
            "quotation",
            str(quotation["id"]),
            Jsonb(
                {
                    "quotation_number": quotation["quotation_number"],
                    "line_count": len(quotation["lines"]),
                    "currency": quotation["currency"],
                    "total": str(quotation["total"]),
                    "source": "whatsapp",
                }
            ),
        ),
    )


def create_quotation(
    settings: Any,
    tenant_id: str,
    *,
    customer_name: Any,
    customer_contact: Any = None,
    valid_days: Any = None,
    selections: Iterable[Sequence[Any]],
    terms: Any = None,
    source: str = "dashboard",
    customer_wa_id: Any = None,
    idempotency_key: Any = None,
    created_by_user_id: Any = None,
    audit_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Create a formal, catalogue-priced quotation for one tenant.

    ``audit_hook`` is called inside the tenant transaction after the rows are
    written, so an audit failure rolls the quotation back. WhatsApp callers leave it
    as None and get a system-attributed audit row in the same transaction.
    """
    if not tenant_id:
        raise QuotationError("Tenant is required.")
    if source not in ("dashboard", "whatsapp"):
        raise QuotationError("Unsupported quotation source.")
    name = _text(customer_name, "Customer name", max_length=200, required=True)
    contact = _text(customer_contact, "Customer contact", max_length=200, required=False)
    wa_id = _text(customer_wa_id, "WhatsApp identity", max_length=200, required=False)
    retry_key = _text(idempotency_key, "Idempotency key", max_length=200, required=False)
    actor = _text(created_by_user_id, "Created by", max_length=200, required=False)
    if source == "whatsapp":
        if not wa_id:
            raise QuotationError("A WhatsApp quotation requires a customer identity.")
        if not retry_key:
            raise QuotationError("A WhatsApp quotation requires an idempotency key.")
    normalized = _normalize_selections(selections)

    issue_date = date.today()
    with tenant_transaction(tenant_id, _database_url(settings)) as cursor:
        cursor.execute(
            "select t.name, s.currency, s.configuration "
            "from public.tenants t "
            "join public.tenant_settings s on s.tenant_id = t.id "
            "where t.id = app.current_tenant_id()"
        )
        tenant_row = cursor.fetchone()
        if tenant_row is None:
            raise QuotationError("Tenant settings were not found.")
        _business_name, currency, configuration = tenant_row
        configuration = configuration if isinstance(configuration, dict) else {}
        days = _valid_days(configuration, valid_days)
        expiry_date = issue_date + timedelta(days=days)
        tax_rate, tax_treatment = _tax_configuration(configuration)
        raw_terms = configuration.get("quotation_terms") if terms is None else terms
        quotation_terms = _text(raw_terms, "Terms", max_length=4000, required=False)

        if retry_key:
            existing = _existing_by_idempotency(cursor, source, retry_key)
            if existing is not None:
                existing_id, existing_wa = existing
                if source == "whatsapp" and (existing_wa or "") != (wa_id or ""):
                    raise QuotationConflictError(
                        "This quotation request conflicts with an existing record."
                    )
                fetched = _fetch_quotation(cursor, existing_id, duplicate=True)
                if fetched is not None:
                    return fetched
                raise QuotationError("Quotation could not be read.")

        lines: list[dict[str, Any]] = []

        subtotal = Decimal("0.00")
        for variant_id, quantity in normalized:
            cursor.execute(
                """
                select p.id, p.name, p.description, v.id, v.sku, v.name, v.price,
                       v.availability, v.stock_quantity
                from public.product_variants v
                join public.products p on p.id = v.product_id and p.tenant_id = v.tenant_id
                where v.id = %s and v.tenant_id = app.current_tenant_id()
                  and v.is_active and p.is_active
                """,
                (variant_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise QuotationError("A selected catalogue item is unavailable.")
            (
                product_id,
                product_name,
                description,
                source_variant_id,
                sku,
                variant_name,
                unit_price,
                availability,
                stock_quantity,
            ) = row
            if availability == "OUT_OF_STOCK":
                raise QuotationError(f"{product_name} ({variant_name}) is out of stock.")
            if (
                availability == "IN_STOCK"
                and stock_quantity is not None
                and quantity > stock_quantity
            ):
                raise QuotationError(
                    f"{product_name} ({variant_name}) has only {stock_quantity} in stock."
                )
            line_subtotal = _money(Decimal(unit_price) * quantity)
            subtotal += line_subtotal
            line_description = f"{product_name} - {variant_name}"
            if description:
                line_description = f"{line_description}: {description}"
            lines.append(
                {
                    "source_product_id": product_id,
                    "source_variant_id": source_variant_id,
                    "sku": sku,
                    "description": line_description,
                    "availability": availability,
                    "quantity": quantity,
                    "unit_price": Decimal(unit_price),
                    "subtotal": line_subtotal,
                }
            )

        subtotal = _money(subtotal)
        tax_amount = (
            _money(subtotal * tax_rate / Decimal("100")) if tax_rate is not None else None
        )
        total = subtotal + (tax_amount or Decimal("0.00"))
        quotation_id = str(uuid.uuid4())
        quotation_number = f"Q-{issue_date:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
        cursor.execute(
            """
            insert into public.quotations
                (id, tenant_id, quotation_number, customer_name, customer_contact,
                 issue_date, expiry_date, currency, subtotal, tax_rate, tax_amount,
                 total, tax_treatment, terms, created_by_user_id, source,
                 customer_wa_id, idempotency_key)
            values
                (%s, app.current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (tenant_id, source, idempotency_key) do nothing
            returning id
            """,
            (
                quotation_id,
                quotation_number,
                name,
                contact,
                issue_date,
                expiry_date,
                currency,
                subtotal,
                tax_rate,
                tax_amount,
                total,
                tax_treatment,
                quotation_terms,
                actor,
                source,
                wa_id,
                retry_key,
            ),
        )
        inserted = cursor.fetchone()
        if inserted is None:
            existing = _existing_by_idempotency(cursor, source, retry_key)
            if existing is None:
                raise QuotationError("Quotation could not be created.")
            existing_id, existing_wa = existing
            if source == "whatsapp" and (existing_wa or "") != (wa_id or ""):
                raise QuotationConflictError(
                    "This quotation request conflicts with an existing record."
                )
            fetched = _fetch_quotation(cursor, existing_id, duplicate=True)
            if fetched is not None:
                return fetched
            raise QuotationError("Quotation could not be read after creation.")

        quotation_id = str(inserted[0])
        cursor.executemany(
            """
            insert into public.quotation_lines
                (tenant_id, quotation_id, source_product_id, source_variant_id, sku,
                 description, availability, quantity, unit_price, subtotal)
            values (app.current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    quotation_id,
                    line["source_product_id"],
                    line["source_variant_id"],
                    line["sku"],
                    line["description"],
                    line["availability"],
                    line["quantity"],
                    line["unit_price"],
                    line["subtotal"],
                )
                for line in lines
            ],
        )
        quotation = _fetch_quotation(cursor, quotation_id)
        if quotation is None:
            raise QuotationError("Quotation could not be read after creation.")
        if audit_hook is not None:
            audit_hook(quotation)
        elif source == "whatsapp":
            _system_audit(cursor, quotation)
        return quotation



# ---------------------------------------------------------------------------
# Quotation briefs: what the customer asked for while it is still a request
# ---------------------------------------------------------------------------
def _normalize_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _brief_items(brief: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    items = (brief or {}).get("items") if isinstance(brief, Mapping) else None
    return [item for item in items or [] if isinstance(item, dict)]


def _match_product(
    products: Sequence[Mapping[str, Any]], name: Any
) -> tuple[Mapping[str, Any] | None, list[str]]:
    """Return the single matching active product, or candidates when ambiguous."""
    wanted = _normalize_name(name)
    if not wanted:
        return None, []
    exact = [p for p in products if _normalize_name(p.get("name")) == wanted]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, [str(p.get("name")) for p in exact[:5]]
    partial = [
        p
        for p in products
        if wanted in _normalize_name(p.get("name"))
        or _normalize_name(p.get("name")) in wanted
    ]
    if len(partial) == 1:
        return partial[0], []
    return None, [str(p.get("name")) for p in partial[:5]]


def _weight_of(variant: Mapping[str, Any]) -> str | None:
    attributes = variant.get("attributes")
    if not isinstance(attributes, Mapping):
        return None
    raw = attributes.get("weight_kg")
    if raw is None:
        return None
    try:
        return str(int(Decimal(str(raw))))
    except (InvalidOperation, TypeError, ValueError):
        return str(raw)


def _match_variant(
    item: Mapping[str, Any], product: Mapping[str, Any]
) -> tuple[Mapping[str, Any] | None, list[str]]:
    """Resolve exactly one priced variant for a matched product."""
    variants = [v for v in product.get("variants") or [] if isinstance(v, dict)]
    if not variants:
        return None, []
    weight = item.get("weight_kg")
    if weight is not None:
        try:
            wanted = str(int(Decimal(str(weight))))
        except (InvalidOperation, TypeError, ValueError):
            return None, []
        matches = [v for v in variants if _weight_of(v) == wanted]
        if len(matches) == 1:
            return matches[0], []
        return None, [_weight_of(v) or str(v.get("name")) for v in variants]

    # No weight supplied. A weight-priced product still needs one; a product with a
    # single priced option does not.
    weights = [_weight_of(v) for v in variants]
    if any(weight_value is not None for weight_value in weights):
        if len(variants) == 1:
            return variants[0], []
        return None, [w for w in weights if w is not None]

    label = _normalize_name(item.get("variant") or item.get("variant_name"))
    if label:
        matches = [
            v
            for v in variants
            if label
            in {
                _normalize_name(v.get("name")),
                _normalize_name(v.get("code")),
                _normalize_name(v.get("sku")),
            }
        ]
        if len(matches) == 1:
            return matches[0], []
    if len(variants) == 1:
        return variants[0], []
    return None, [str(v.get("name")) for v in variants]



def evaluate_brief(
    settings: Any, tenant_id: str, brief: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Resolve a brief against the live tenant catalogue.

    Returns the material details still missing plus the exact variant each item
    resolved to. Nothing here accepts a price from the caller.
    """
    from .catalogue import CatalogueService  # noqa: PLC0415  (keep module import light)

    items = _brief_items(brief)
    if not items:
        return {
            "missing": ["items"],
            "items": [],
            "needs_staff": False,
            "reason": None,
            "message": "What would you like a quotation for?",
        }
    if len(items) > MAX_BRIEF_ITEMS:
        raise QuotationError(f"A request can contain at most {MAX_BRIEF_ITEMS} items.")

    try:
        products = CatalogueService(
            tenant_id, _database_url(settings)
        ).list_active_products()
    except Exception as error:  # noqa: BLE001 - never let a read failure invent a price
        raise QuotationError("The catalogue is unavailable right now.") from error

    missing: list[str] = []
    resolved: list[dict[str, Any]] = []
    needs_staff = False
    reason: str | None = None
    message: str | None = None

    for index, item in enumerate(items, start=1):
        product, candidates = _match_product(products, item.get("product_name"))
        field = f"items[{index}]"
        if product is None:
            if candidates:
                missing.append(f"{field}.product")
                message = (
                    f"Which item did you mean for item {index}: "
                    + ", ".join(candidates)
                    + "?"
                )
                resolved.append({"product": None, "variant": None, "quantity": None})
                continue
            needs_staff = True
            reason = "unlisted_product"
            message = (
                f"I could not match item {index} to our catalogue, so our team will "
                "price it for you."
            )
            resolved.append({"product": None, "variant": None, "quantity": None})
            continue

        variant, options = _match_variant(item, product)
        if variant is None:
            if options:
                if any(_weight_of(v) for v in product.get("variants") or []):
                    missing.append(f"{field}.weight_kg")
                    message = (
                        f"What size would you like for {product.get('name')}: "
                        + ", ".join(options)
                        + "?"
                    )
                else:
                    missing.append(f"{field}.variant")
                    message = (
                        f"Which option of {product.get('name')} would you like: "
                        + ", ".join(options)
                        + "?"
                    )
            else:
                needs_staff = True
                reason = "unavailable_variant"
                message = f"{product.get('name')} has no priced option right now."
            quantity = item.get("quantity")
            try:
                quantity_value = int(str(quantity).strip()) if quantity is not None else None
            except (TypeError, ValueError):
                quantity_value = None
            if quantity_value is None or quantity_value < 1:
                missing.append(f"{field}.quantity")
            resolved.append({"product": product, "variant": None, "quantity": None})
            continue

        quantity = item.get("quantity")
        try:
            quantity_value = int(str(quantity).strip()) if quantity is not None else None
        except (TypeError, ValueError):
            quantity_value = None
        if quantity_value is None or quantity_value < 1:
            missing.append(f"{field}.quantity")
            message = message or f"How many of {product.get('name')} do you need?"
        resolved.append(
            {"product": product, "variant": variant, "quantity": quantity_value}
        )

    if needs_staff:
        # Staff review wins: never keep asking for details that staff must price.
        missing = []
    return {
        "missing": missing,
        "items": resolved,
        "needs_staff": needs_staff,
        "reason": reason,
        "message": message,
    }



def _merge_brief(
    existing: Mapping[str, Any] | None, patch: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge the details from one message into the stored request.

    Corrections update the existing entry instead of appending a duplicate: a later
    "make it 2kg" or "two of them" changes that item. ``replace`` starts over.
    """
    if not isinstance(patch, Mapping):
        raise QuotationError("Quotation details must be an object.")
    current = {
        "items": [dict(item) for item in _brief_items(existing)],
        "notes": (existing or {}).get("notes") if isinstance(existing, Mapping) else None,
    }
    if patch.get("replace") is True:
        current["items"] = []
    if "notes" in patch:
        current["notes"] = _text(
            patch.get("notes"), "Notes", max_length=1000, required=False
        )

    raw_items = patch.get("items")
    if raw_items is not None:
        if not isinstance(raw_items, list):
            raise QuotationError("Items must be a list.")
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise QuotationError("Each item must be an object.")
            name = _text(raw.get("product_name"), "Item", max_length=200, required=False)
            option = _text(
                raw.get("variant") or raw.get("variant_name"),
                "Option",
                max_length=100,
                required=False,
            )
            weight = raw.get("weight_kg")
            if weight in (None, ""):
                weight_value = None
            else:
                try:
                    weight_value = int(Decimal(str(weight)))
                except (InvalidOperation, TypeError, ValueError):
                    raise QuotationError("Weight must be a number.") from None
                if not 1 <= weight_value <= 1000:
                    raise QuotationError("Weight must be between 1 and 1000.")

            quantity = raw.get("quantity")
            if quantity in (None, ""):
                quantity_value = None
            else:
                try:
                    quantity_value = int(str(quantity).strip())
                except (TypeError, ValueError):
                    raise QuotationError("Quantity must be a whole number.") from None
                if not 1 <= quantity_value <= MAX_QUANTITY:
                    raise QuotationError(
                        f"Quantity must be between 1 and {MAX_QUANTITY}."
                    )

            if name is None and len(current["items"]) == 1:
                target = current["items"][0]
            else:
                target = next(
                    (
                        item
                        for item in current["items"]
                        if name
                        and _normalize_name(item.get("product_name"))
                        == _normalize_name(name)
                    ),
                    None,
                )
            if target is None:
                if name is None:
                    raise QuotationError("Which item is this detail for?")
                target = {"product_name": None, "weight_kg": None, "quantity": None,
                          "variant": None}
                current["items"].append(target)
            target["product_name"] = name or target.get("product_name")
            if weight_value is not None:
                target["weight_kg"] = weight_value
            if quantity_value is not None:
                target["quantity"] = quantity_value
            if option:
                target["variant"] = option

    if not current["items"]:
        raise QuotationError("Tell me at least one item you would like quoted.")
    if len(current["items"]) > MAX_BRIEF_ITEMS:
        raise QuotationError(f"A request can contain at most {MAX_BRIEF_ITEMS} items.")
    return current


def _brief_row(cursor: Any, row: tuple[Any, ...]) -> dict[str, Any]:
    data = _row_dict(cursor, row)
    data["id"] = str(data["id"])
    data["conversation_id"] = str(data["conversation_id"])
    data["missing_fields"] = list(data.get("missing_fields") or [])
    if not isinstance(data.get("brief"), dict):
        data["brief"] = {}
    return data


def load_brief(
    settings: Any, tenant_id: str, conversation_id: str
) -> dict[str, Any] | None:
    """The conversation's current quotation request, or None."""
    if not conversation_id:
        return None
    with tenant_transaction(tenant_id, _database_url(settings)) as cursor:
        cursor.execute(
            """
            select id, conversation_id, customer_wa_id, status, brief,
                   missing_fields, created_at, updated_at
            from public.quotation_briefs
            where tenant_id = app.current_tenant_id() and conversation_id = %s
            """,
            (str(conversation_id),),
        )
        row = cursor.fetchone()
        return _brief_row(cursor, row) if row is not None else None



def update_brief(
    settings: Any,
    tenant_id: str,
    conversation_id: str,
    customer_wa_id: str | None,
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge new details into the conversation's brief and persist the result."""
    if not conversation_id:
        raise QuotationError("A conversation is required to collect a quotation.")
    existing = load_brief(settings, tenant_id, conversation_id)
    merged = _merge_brief(existing.get("brief") if existing else None, patch)
    evaluation = evaluate_brief(settings, tenant_id, merged)
    status = (
        "handed_over"
        if evaluation["needs_staff"]
        else ("ready" if not evaluation["missing"] else "collecting")
    )
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    with tenant_transaction(tenant_id, _database_url(settings)) as cursor:
        cursor.execute(
            """
            insert into public.quotation_briefs
                (tenant_id, conversation_id, customer_wa_id, status, brief,
                 missing_fields)
            values (app.current_tenant_id(), %s, %s, %s, %s, %s)
            on conflict (tenant_id, conversation_id) do update
              set status = excluded.status,
                  brief = excluded.brief,
                  missing_fields = excluded.missing_fields,
                  customer_wa_id = coalesce(
                      excluded.customer_wa_id, public.quotation_briefs.customer_wa_id
                  )
            returning id, conversation_id, customer_wa_id, status, brief,
                      missing_fields, created_at, updated_at
            """,
            (
                str(conversation_id),
                _text(
                    customer_wa_id,
                    "WhatsApp identity",
                    max_length=200,
                    required=False,
                ),
                status,
                Jsonb(merged),
                evaluation["missing"],
            ),
        )
        row = _brief_row(cursor, cursor.fetchone())
    row["evaluation"] = evaluation
    return row


def start_brief(
    settings: Any,
    tenant_id: str,
    conversation_id: str,
    customer_wa_id: str | None,
) -> dict[str, Any]:
    """Start (or restart) a quotation request for this conversation."""
    if not conversation_id:
        raise QuotationError("A conversation is required to collect a quotation.")
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    with tenant_transaction(tenant_id, _database_url(settings)) as cursor:
        cursor.execute(
            """
            insert into public.quotation_briefs
                (tenant_id, conversation_id, customer_wa_id, status, brief,
                 missing_fields)
            values (app.current_tenant_id(), %s, %s, 'collecting', %s, %s)
            on conflict (tenant_id, conversation_id) do update
              set status = 'collecting',
                  brief = excluded.brief,
                  missing_fields = excluded.missing_fields,
                  customer_wa_id = coalesce(
                      excluded.customer_wa_id, public.quotation_briefs.customer_wa_id
                  )
            returning id, conversation_id, customer_wa_id, status, brief,
                      missing_fields, created_at, updated_at
            """,
            (
                str(conversation_id),
                _text(customer_wa_id, "WhatsApp identity", max_length=200, required=False),
                Jsonb({"items": []}),
                ["items"],
            ),
        )
        return _brief_row(cursor, cursor.fetchone())


def clear_brief(
    settings: Any, tenant_id: str, conversation_id: str, *, status: str = "quoted"
) -> None:
    """Mark the request finished so the next message starts a new one."""
    if not conversation_id:
        return
    if status not in BRIEF_STATUSES:
        raise QuotationError("Unsupported quotation request status.")
    with tenant_transaction(tenant_id, _database_url(settings)) as cursor:
        cursor.execute(
            "update public.quotation_briefs set status = %s, missing_fields = '{}' "
            "where tenant_id = app.current_tenant_id() and conversation_id = %s",
            (status, str(conversation_id)),
        )


def brief_prompt_text(brief: Mapping[str, Any] | None) -> str:
    """Deterministic prompt context so the model asks only for what is missing."""
    if not brief:
        return ""
    evaluation = brief.get("evaluation") if isinstance(brief, Mapping) else None
    missing = (evaluation or {}).get("missing") or brief.get("missing_fields") or []
    if not missing:
        return (
            "\n\nQUOTATION REQUEST (trusted, server-computed):\n"
            "Every material detail has been collected. Call request_quote with "
            "use_brief=true to issue the quotation. Do not restate prices yourself."
        )
    questions = (evaluation or {}).get("message") or ""
    return (
        "\n\nQUOTATION REQUEST (trusted, server-computed):\n"
        f"Collected: {brief.get('brief')}\n"
        f"Still missing: {missing}\n"
        + (f"Ask only this next question: {questions}\n" if questions else "")
        + "Never guess a missing detail and never state a price before request_quote "
        "returns one."
    )


def create_quotation_from_brief(
    settings: Any,
    tenant_id: str,
    *,
    conversation_id: str,
    customer_wa_id: str,
    customer_name: Any,
    message_id: str,
    terms: Any = None,
) -> dict[str, Any]:
    """Issue the quotation described by the conversation's brief.

    The brief is resolved against the current catalogue and every line is priced
    from persisted variants. An unpriceable request raises ``QuotationNeedsStaff``
    and is never reduced to a guess.
    """
    brief = load_brief(settings, tenant_id, conversation_id)
    if brief is None:
        raise QuotationError("There is no quotation request in this conversation yet.")
    evaluation = evaluate_brief(settings, tenant_id, brief.get("brief"))
    if evaluation["needs_staff"]:
        raise QuotationNeedsStaff(
            evaluation.get("message") or "Our team needs to price this request.",
            reason=evaluation.get("reason") or "custom_order",
        )
    if evaluation["missing"]:
        raise QuotationError(
            "I still need: " + ", ".join(evaluation["missing"]) + "."
        )
    selections = []
    for item in evaluation["items"]:
        variant = item.get("variant") or {}
        variant_id = variant.get("id")
        quantity = item.get("quantity")
        if not variant_id or not quantity:
            raise QuotationError("A requested item could not be priced.")
        selections.append((variant_id, quantity))
    quotation = create_quotation(
        settings,
        tenant_id,
        customer_name=customer_name,
        valid_days=None,
        selections=selections,
        terms=terms,
        source="whatsapp",
        customer_wa_id=customer_wa_id,
        idempotency_key=str(message_id),
    )
    clear_brief(settings, tenant_id, conversation_id, status="quoted")
    return quotation



# ---------------------------------------------------------------------------
# Quotation PDF delivery ledger
# ---------------------------------------------------------------------------
def _delivery_row(cursor: Any, row: tuple[Any, ...]) -> dict[str, Any]:
    data = _row_dict(cursor, row)
    for key in ("id", "quotation_id", "conversation_id", "channel_id"):
        if data.get(key) is not None:
            data[key] = str(data[key])
    return data


def delivery_key(quotation_id: str, message_id: str) -> str:
    """A stable key for one attempt, bounded to the column limit."""
    return f"{quotation_id}:{message_id}"[:200]


def begin_delivery(
    settings: Any,
    tenant_id: str,
    *,
    quotation_id: str,
    conversation_id: str | None,
    channel_id: str | None,
    customer_wa_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Claim the right to send one quotation document.

    The row is written before the send. A replay finds the existing row and does
    not send again; only a *definite* failure (``failed``) may be retried. A
    ``pending`` or ``uncertain`` row means the outcome is unknown, so it is never
    resent blindly.
    """
    key = _text(idempotency_key, "Delivery key", max_length=200, required=True)
    wa_id = _text(customer_wa_id, "WhatsApp identity", max_length=200, required=True)
    quotation = get_quotation(settings, tenant_id, quotation_id, customer_wa_id=wa_id)
    if quotation is None:
        raise QuotationError("Quotation not found.")
    columns = (
        "id, quotation_id, channel_id, conversation_id, customer_wa_id, "
        "idempotency_key, media_id, provider_message_id, delivery_status, "
        "error_code, error_message"
    )
    with tenant_transaction(tenant_id, _database_url(settings)) as cursor:
        cursor.execute(
            """
            insert into public.quotation_deliveries
                (tenant_id, quotation_id, channel_id, conversation_id,
                 customer_wa_id, idempotency_key, delivery_status)
            values (app.current_tenant_id(), %s, %s, %s, %s, %s, 'pending')
            on conflict (tenant_id, quotation_id, idempotency_key) do nothing
            returning id, quotation_id, channel_id, conversation_id, customer_wa_id,
                      idempotency_key, media_id, provider_message_id,
                      delivery_status, error_code, error_message
            """,
            (
                quotation["id"],
                str(channel_id) if channel_id else None,
                str(conversation_id) if conversation_id else None,
                wa_id,
                key,
            ),
        )
        row = cursor.fetchone()
        if row is not None:
            return {"row": _delivery_row(cursor, row), "created": True, "should_send": True}
        cursor.execute(
            f"""
            select {columns}
            from public.quotation_deliveries
            where tenant_id = app.current_tenant_id() and quotation_id = %s
              and idempotency_key = %s
            """,
            (quotation["id"], key),
        )
        existing = _delivery_row(cursor, cursor.fetchone())
        if existing["delivery_status"] == "failed":
            cursor.execute(
                """
                update public.quotation_deliveries
                set delivery_status = 'pending', error_code = null, error_message = null
                where id = %s and tenant_id = app.current_tenant_id()
                  and delivery_status = 'failed'
                returning id, quotation_id, channel_id, conversation_id,
                          customer_wa_id, idempotency_key, media_id,
                          provider_message_id, delivery_status, error_code,
                          error_message
                """,
                (existing["id"],),
            )
            retried = cursor.fetchone()
            if retried is not None:
                return {
                    "row": _delivery_row(cursor, retried),
                    "created": False,
                    "should_send": True,
                }
        return {
            "row": existing,
            "created": False,
            "should_send": False,
            "status": existing["delivery_status"],
        }



def finish_delivery(
    settings: Any,
    tenant_id: str,
    delivery_id: str,
    *,
    status: str,
    media_id: str | None = None,
    provider_message_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Record the provider outcome. Only a pending/uncertain attempt is finalized."""
    if status not in DELIVERY_STATUSES:
        raise QuotationError("Unsupported delivery status.")
    columns = (
        "id, quotation_id, channel_id, conversation_id, customer_wa_id, "
        "idempotency_key, media_id, provider_message_id, delivery_status, "
        "error_code, error_message"
    )
    with tenant_transaction(tenant_id, _database_url(settings)) as cursor:
        cursor.execute(
            """
            update public.quotation_deliveries
            set delivery_status = %s,
                media_id = coalesce(%s, media_id),
                provider_message_id = coalesce(%s, provider_message_id),
                error_code = %s,
                error_message = %s
            where id = %s and tenant_id = app.current_tenant_id()
              and delivery_status in ('pending', 'uncertain')
            returning id, quotation_id, channel_id, conversation_id, customer_wa_id,
                      idempotency_key, media_id, provider_message_id,
                      delivery_status, error_code, error_message
            """,
            (
                status,
                _text(media_id, "Media id", max_length=200, required=False),
                _text(provider_message_id, "Provider message id", max_length=200,
                      required=False),
                _text(error_code, "Error code", max_length=120, required=False),
                _text(error_message, "Error message", max_length=500, required=False),
                str(delivery_id),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            # Already finalized (a replayed callback or a second finish call). Return
            # the stored truth instead of overwriting it.
            cursor.execute(
                f"""
                select {columns}
                from public.quotation_deliveries
                where id = %s and tenant_id = app.current_tenant_id()
                """,
                (str(delivery_id),),
            )
            existing = cursor.fetchone()
            return _delivery_row(cursor, existing) if existing is not None else {}
        return _delivery_row(cursor, row)


def list_deliveries(
    settings: Any, tenant_id: str, quotation_id: str
) -> list[dict[str, Any]]:
    with tenant_transaction(tenant_id, _database_url(settings)) as cursor:
        cursor.execute(
            """
            select id, quotation_id, channel_id, conversation_id, customer_wa_id,
                   idempotency_key, media_id, provider_message_id, delivery_status,
                   error_code, error_message, created_at, updated_at
            from public.quotation_deliveries
            where tenant_id = app.current_tenant_id() and quotation_id = %s
            order by created_at desc
            """,
            (str(quotation_id),),
        )
        return [_delivery_row(cursor, row) for row in cursor.fetchall()]


def _money_text(value: Any) -> str:
    try:
        return f"{Decimal(str(value)):.2f}"
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def format_customer_summary(quotation: Mapping[str, Any]) -> str:
    """A deterministic WhatsApp text summary of an issued quotation."""
    currency = str(quotation.get("currency") or "")
    parts = [f"Quotation {quotation.get('quotation_number')}"]
    for line in quotation.get("lines") or []:
        description = line.get("description") or line.get("sku") or "Item"
        parts.append(
            f"{line.get('quantity')} x {description} - {currency} "
            f"{_money_text(line.get('subtotal'))}"
        )
    parts.append(f"Subtotal: {currency} {_money_text(quotation.get('subtotal'))}")
    if quotation.get("tax_amount") is not None:
        parts.append(f"Tax: {currency} {_money_text(quotation.get('tax_amount'))}")
    parts.append(f"Total: {currency} {_money_text(quotation.get('total'))}")
    if quotation.get("expiry_date"):
        parts.append(f"Valid until: {quotation['expiry_date']}")
    if quotation.get("terms"):
        parts.append(str(quotation["terms"]))
    parts.append("This quotation does not reserve stock or create an order.")
    return "\n".join(parts)


def fallback_customer_summary(quotation: Mapping[str, Any]) -> str:
    """A minimal truthful summary that never raises, built from persisted fields."""
    number = quotation.get("quotation_number") or "your quotation"
    currency = str(quotation.get("currency") or "")
    total = _money_text(quotation.get("total"))
    return (
        f"Quotation {number} was created. Total: {currency} {total}. "
        "Please ask our team if you need the full details."
    )

