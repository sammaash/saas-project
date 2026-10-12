"""Tenant-scoped quotation issuing with immutable catalogue and tax snapshots."""

from __future__ import annotations

import io
import uuid
from html import escape
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Sequence

from saas.tenant_context import tenant_transaction

from ..config import Settings
from . import audit, validation as v

MONEY = Decimal("0.01")
MAX_QUANTITY = 100000


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _row_dict(cursor: Any, row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip((column.name for column in cursor.description), row, strict=True))


def list_quotations(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            select id, quotation_number, customer_name, issue_date, expiry_date, currency,
                   total,
                   case when status = 'issued' and expiry_date < current_date
                        then 'expired' else status end as status
            from public.quotations
            where tenant_id = app.current_tenant_id()
            order by created_at desc
            """
        )
        return [_row_dict(cursor, row) for row in cursor.fetchall()]


def quotation_form_context(settings: Settings, tenant_id: str) -> dict[str, Any]:
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            "select currency, configuration from public.tenant_settings "
            "where tenant_id = app.current_tenant_id()"
        )
        settings_row = cursor.fetchone()
        if settings_row is None:
            raise v.ServiceError("Tenant settings were not found.", 404)
        currency, configuration = settings_row
        cursor.execute(
            """
            select p.name as product_name, v.id as variant_id, v.sku, v.name as variant_name,
                   v.price, v.availability, v.stock_quantity
            from public.products p
            join public.product_variants v
              on v.product_id = p.id and v.tenant_id = p.tenant_id
            where p.tenant_id = app.current_tenant_id() and p.is_active and v.is_active
            order by p.sort_order, p.name, v.sort_order, v.name
            """
        )
        items = [_row_dict(cursor, row) for row in cursor.fetchall()]
    configuration = configuration if isinstance(configuration, dict) else {}
    valid_days = configuration.get("quotation_valid_days", 14)
    try:
        valid_days = int(valid_days)
    except (TypeError, ValueError):
        valid_days = 14
    if not 1 <= valid_days <= 365:
        valid_days = 14
    terms = configuration.get("quotation_terms")
    terms = terms if isinstance(terms, str) else ""
    return {
        "currency": currency,
        "items": items,
        "valid_days": valid_days,
        "terms": terms,
    }


def _tax_configuration(configuration: Any) -> tuple[Decimal | None, str]:
    configuration = configuration if isinstance(configuration, dict) else {}
    raw = configuration.get("tax_rate")
    if raw is None or str(raw).strip() == "":
        return None, "Tax is not configured for this tenant."
    try:
        rate = Decimal(str(raw))
    except InvalidOperation:
        raise v.ServiceError("The tenant tax rate configuration is invalid.") from None
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise v.ServiceError("The tenant tax rate configuration must be between 0 and 100.")
    return rate, f"Tax calculated at {rate.normalize()}%."


def create_quotation(
    settings: Settings,
    admin_id: str,
    tenant_id: str,
    customer_name: Any,
    customer_contact: Any,
    valid_days: Any,
    selections: Sequence[tuple[Any, Any]],
    terms: Any = None,
) -> str:
    customer = v.text(customer_name, "Customer name", max_length=200)
    contact = v.text(
        customer_contact, "Customer contact", max_length=200, required=False
    )
    days = v.integer(valid_days, "Validity", minimum=1, default=14)
    if days > 365:
        raise v.ServiceError("Validity must be between 1 and 365 days.")
    normalized: list[tuple[str, int]] = []
    for raw_variant_id, raw_quantity in selections:
        if raw_quantity is None or str(raw_quantity).strip() in {"", "0"}:
            continue
        variant_id = v.uuid_value(raw_variant_id, "Variant")
        quantity = v.integer(raw_quantity, "Quantity", minimum=1)
        if quantity > MAX_QUANTITY:
            raise v.ServiceError(f"Quantity cannot exceed {MAX_QUANTITY}.")
        normalized.append((variant_id, quantity))
    if not normalized:
        raise v.ServiceError("Select at least one catalogue item and quantity.")
    if len({variant_id for variant_id, _ in normalized}) != len(normalized):
        raise v.ServiceError("Each catalogue variant may appear only once.")

    issue_date = date.today()
    expiry_date = issue_date + timedelta(days=days)
    quotation_id = str(uuid.uuid4())
    quotation_number = f"Q-{issue_date:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
    with v.translated(), tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            select t.name, s.currency, s.configuration
            from public.tenants t
            join public.tenant_settings s on s.tenant_id = t.id
            where t.id = app.current_tenant_id()
            """
        )
        tenant_row = cursor.fetchone()
        if tenant_row is None:
            raise v.ServiceError("Tenant settings were not found.", 404)
        _business_name, currency, configuration = tenant_row
        tax_rate, tax_treatment = _tax_configuration(configuration)
        configuration = configuration if isinstance(configuration, dict) else {}
        quotation_terms = v.text(
            terms if terms is not None else configuration.get("quotation_terms"),
            "Terms",
            max_length=4000,
            required=False,
        )

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
                raise v.ServiceError("A selected catalogue item is unavailable.", 409)
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
                raise v.ServiceError(f"{product_name} ({variant_name}) is out of stock.", 409)
            if (
                availability == "IN_STOCK"
                and stock_quantity is not None
                and quantity > stock_quantity
            ):
                raise v.ServiceError(
                    f"{product_name} ({variant_name}) has only {stock_quantity} in stock.",
                    409,
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
        cursor.execute(
            """
            insert into public.quotations
                (id, tenant_id, quotation_number, customer_name, customer_contact,
                 issue_date, expiry_date, currency, subtotal, tax_rate, tax_amount,
                 total, tax_treatment, terms, created_by_user_id)
            values
                (%s, app.current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s)
            """,
            (
                quotation_id,
                quotation_number,
                customer,
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
                admin_id,
            ),
        )
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
        audit.record(
            settings,
            admin_id,
            tenant_id,
            "quotation.issued",
            "quotation",
            quotation_id,
            {
                "quotation_number": quotation_number,
                "line_count": len(lines),
                "currency": currency,
                "total": str(total),
            },
        )
    return quotation_id


def get_quotation(
    settings: Settings, tenant_id: str, quotation_id: str
) -> dict[str, Any]:
    quotation_id = v.uuid_value(quotation_id, "Quotation")
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            select q.id, q.quotation_number, q.customer_name, q.customer_contact,
                   q.issue_date, q.expiry_date, q.currency, q.subtotal, q.tax_rate,
                   q.tax_amount, q.total, q.tax_treatment, q.terms,
                   case when q.status = 'issued' and q.expiry_date < current_date
                        then 'expired' else q.status end as status,
                   t.name as business_name
            from public.quotations q
            join public.tenants t on t.id = q.tenant_id
            where q.id = %s and q.tenant_id = app.current_tenant_id()
            """,
            (quotation_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise v.ServiceError("Quotation not found.", 404)
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
        return quotation


def quotation_pdf(quotation: dict[str, Any]) -> bytes:
    from reportlab.lib.pagesizes import A4  # noqa: PLC0415
    from reportlab.lib.styles import getSampleStyleSheet  # noqa: PLC0415
    from reportlab.lib.units import mm  # noqa: PLC0415
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # noqa: PLC0415
    from reportlab.lib import colors  # noqa: PLC0415

    output = io.BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm
    )
    styles = getSampleStyleSheet()
    story: list[Any] = [
        Paragraph(escape(str(quotation["business_name"])), styles["Title"]),
        Paragraph(
            f"Quotation {escape(str(quotation['quotation_number']))}", styles["Heading2"]
        ),
        Paragraph(
            f"Customer: {escape(str(quotation['customer_name']))}", styles["BodyText"]
        ),
    ]
    if quotation.get("customer_contact"):
        story.append(
            Paragraph(
                f"Contact: {escape(str(quotation['customer_contact']))}",
                styles["BodyText"],
            )
        )
    story.extend(
        [
            Paragraph(
                f"Issued: {quotation['issue_date']} &nbsp;&nbsp; "
                f"Expires: {quotation['expiry_date']}",
                styles["BodyText"],
            ),
            Spacer(1, 8),
        ]
    )
    data = [["SKU", "Description", "Availability", "Qty", "Unit", "Subtotal"]]
    for line in quotation["lines"]:
        data.append(
            [
                str(line["sku"]),
                Paragraph(escape(str(line["description"])), styles["BodyText"]),
                str(line["availability"]).replace("_", " ").title(),
                str(line["quantity"]),
                f"{quotation['currency']} {line['unit_price']:.2f}",
                f"{quotation['currency']} {line['subtotal']:.2f}",
            ]
        )
    table = Table(data, colWidths=[27 * mm, 55 * mm, 28 * mm, 12 * mm, 25 * mm, 28 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.extend(
        [
            table,
            Spacer(1, 10),
            Paragraph(
                f"Subtotal: {quotation['currency']} {quotation['subtotal']:.2f}",
                styles["BodyText"],
            ),
            Paragraph(str(quotation["tax_treatment"]), styles["BodyText"]),
        ]
    )
    if quotation["tax_amount"] is not None:
        story.append(
            Paragraph(
                f"Tax: {quotation['currency']} {quotation['tax_amount']:.2f}",
                styles["BodyText"],
            )
        )
    story.append(
        Paragraph(
            f"<b>Total: {quotation['currency']} {quotation['total']:.2f}</b>",
            styles["BodyText"],
        )
    )
    if quotation.get("terms"):
        story.extend(
            [
                Spacer(1, 8),
                Paragraph("Terms", styles["Heading3"]),
                Paragraph(escape(str(quotation["terms"])), styles["BodyText"]),
            ]
        )
    story.append(
        Paragraph(
            "This quotation does not reserve stock, create an order or request payment.",
            styles["Italic"],
        )
    )
    document.build(story)
    return output.getvalue()
