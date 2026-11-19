"""Tenant-scoped quotation issuing with immutable catalogue and tax snapshots."""

from __future__ import annotations

import io
import uuid
from html import escape
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Sequence

import logging

from saas import quotations as saas_quotations
from saas.tenant_context import tenant_transaction

from ..config import Settings
from . import audit, validation as v

logger = logging.getLogger(__name__)

MONEY = Decimal("0.01")
MAX_QUANTITY = 100000


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _row_dict(cursor: Any, row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip((column.name for column in cursor.description), row, strict=True))


def list_quotations(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    """Every quotation of this tenant, newest first, with its dashboard lifecycle.

    ``lifecycle`` is Generated / Sent / Not sent / Expired (approved Stage 3 decision
    Q4). "Sent" is shown only when WhatsApp confirmed a message id for the document.
    """
    with tenant_transaction(tenant_id, settings.app_database_url) as cursor:
        cursor.execute(
            """
            select q.id, q.quotation_number, q.customer_name, q.customer_contact,
                   q.customer_wa_id, q.source, q.issue_date, q.expiry_date, q.currency,
                   q.total, q.created_at,
                   case when q.status = 'issued' and q.expiry_date < current_date
                        then 'expired' else q.status end as status,
                   exists (select 1 from public.quotation_documents d
                            where d.quotation_id = q.id
                              and d.tenant_id = q.tenant_id) as has_document
            from public.quotations q
            where q.tenant_id = app.current_tenant_id()
            order by q.created_at desc
            """
        )
        rows = [_row_dict(cursor, row) for row in cursor.fetchall()]
    overview = saas_quotations.delivery_overview(settings, tenant_id) if rows else {}
    for row in rows:
        row["id"] = str(row["id"])
        delivery = overview.get(row["id"]) or {}
        row["delivery"] = delivery
        row["lifecycle"] = saas_quotations.lifecycle_status(
            expiry_date=row["expiry_date"], delivery_status=delivery.get("delivery_status")
        )
        row["lifecycle_label"] = saas_quotations.LIFECYCLE_LABELS[row["lifecycle"]]
    return rows


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
    """Issue a dashboard quotation through the shared pricing engine.

    ``saas.quotations.create_quotation`` is the single implementation of catalogue
    pricing, stock checks, tax and snapshot lines. This wrapper keeps the console's
    contract: ``ServiceError`` messages and statuses, and a platform audit event written
    inside the same tenant transaction (an audit failure rolls the quotation back).
    """
    customer = v.text(customer_name, "Customer name", max_length=200)
    contact = v.text(customer_contact, "Customer contact", max_length=200, required=False)
    days = v.integer(valid_days, "Validity", minimum=1, default=14)
    if days > 365:
        raise v.ServiceError("Validity must be between 1 and 365 days.")

    def _audit(quotation: dict[str, Any]) -> None:
        audit.record(
            settings,
            admin_id,
            tenant_id,
            "quotation.issued",
            "quotation",
            str(quotation["id"]),
            {
                "quotation_number": quotation["quotation_number"],
                "line_count": len(quotation["lines"]),
                "currency": quotation["currency"],
                "total": str(quotation["total"]),
            },
        )

    try:
        with v.translated():
            quotation = saas_quotations.create_quotation(
                settings,
                tenant_id,
                customer_name=customer,
                customer_contact=contact,
                valid_days=days,
                selections=selections,
                terms=terms,
                source="dashboard",
                created_by_user_id=admin_id,
                audit_hook=_audit,
            )
    except saas_quotations.QuotationError as error:
        message = str(error)
        status = 409 if (
            "unavailable" in message or "out of stock" in message or "in stock" in message
        ) else 400
        raise v.ServiceError(message, status) from None
    try:
        store_pdf(settings, tenant_id, quotation)
    except Exception:  # noqa: BLE001 - the quotation exists; the PDF is rebuilt on demand
        logger.exception("Quotation PDF storage failed tenant=%s", tenant_id)
    return str(quotation["id"])


def get_quotation(
    settings: Settings, tenant_id: str, quotation_id: str
) -> dict[str, Any]:
    quotation_id = v.uuid_value(quotation_id, "Quotation")
    quotation = saas_quotations.get_quotation(settings, tenant_id, quotation_id)
    if quotation is None:
        raise v.ServiceError("Quotation not found.", 404)
    if quotation.get("status") == "issued" and quotation["expiry_date"] < date.today():
        quotation["status"] = "expired"
    return quotation


def store_pdf(settings: Settings, tenant_id: str, quotation: dict[str, Any]) -> bytes:
    """Render the quotation snapshot once and store it (immutable, migration 0044).

    Returns the stored bytes. When a document already exists it is returned unchanged,
    so the PDF a customer received is exactly the PDF staff later download.
    """
    existing = saas_quotations.load_document(settings, tenant_id, str(quotation["id"]))
    if existing is not None:
        return existing
    stored = saas_quotations.store_document(
        settings, tenant_id, str(quotation["id"]), quotation_pdf(quotation)
    )
    return stored["content"]


def document_for(settings: Settings, tenant_id: str, quotation_id: str) -> tuple[dict[str, Any], bytes]:
    """The quotation and its PDF for a member download (stored copy preferred).

    Quotations issued before migration 0044 have no stored document: theirs is rendered
    from the immutable snapshot and stored on first access.
    """
    quotation = get_quotation(settings, tenant_id, quotation_id)
    return quotation, store_pdf(settings, tenant_id, quotation)


def _pdf_money(currency: Any, value: Any) -> str:
    return f"{currency} {Decimal(str(value)):,.2f}"


def quotation_pdf(quotation: dict[str, Any]) -> bytes:
    """A professional one-page quotation built only from the persisted snapshot."""
    from reportlab.lib import colors  # noqa: PLC0415
    from reportlab.lib.enums import TA_RIGHT  # noqa: PLC0415
    from reportlab.lib.pagesizes import A4  # noqa: PLC0415
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: PLC0415
    from reportlab.lib.units import mm  # noqa: PLC0415
    from reportlab.platypus import (  # noqa: PLC0415
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    output = io.BytesIO()
    number = str(quotation.get("quotation_number") or "")
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Quotation {number}",
        author=str(quotation.get("business_name") or ""),
    )
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    small = ParagraphStyle("small", parent=body, fontSize=8.5, leading=11,
                           textColor=colors.HexColor("#444444"))
    right = ParagraphStyle("right", parent=body, alignment=TA_RIGHT)
    title = ParagraphStyle("qtitle", parent=styles["Title"], alignment=TA_RIGHT,
                           fontSize=20, spaceAfter=0, textColor=colors.HexColor("#1F4E78"))
    accent = colors.HexColor("#1F4E78")
    currency = quotation.get("currency") or ""

    business_lines = [f"<b>{escape(str(quotation.get('business_name') or ''))}</b>"]
    if quotation.get("business_phone"):
        business_lines.append(f"WhatsApp: {escape(str(quotation['business_phone']))}")
    header = Table(
        [[Paragraph("<br/>".join(business_lines), body), Paragraph("QUOTATION", title)]],
        colWidths=[100 * mm, 74 * mm],
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LINEBELOW", (0, 0), (-1, 0), 1.2, accent),
                                ("BOTTOMPADDING", (0, 0), (-1, 0), 8)]))

    customer_lines = [f"<b>{escape(str(quotation.get('customer_name') or ''))}</b>"]
    contact = quotation.get("customer_contact") or quotation.get("customer_wa_id")
    if contact:
        customer_lines.append(escape(str(contact)))
    meta = Table(
        [
            [Paragraph("<b>Quotation for</b><br/>" + "<br/>".join(customer_lines), body),
             Paragraph(
                 f"<b>Reference:</b> {escape(number)}<br/>"
                 f"<b>Date issued:</b> {escape(str(quotation.get('issue_date')))}<br/>"
                 f"<b>Valid until:</b> {escape(str(quotation.get('expiry_date')))}",
                 right,
             )]
        ],
        colWidths=[100 * mm, 74 * mm],
    )
    meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("TOPPADDING", (0, 0), (-1, -1), 10)]))

    rows: list[list[Any]] = [["Item", "Qty", "Unit price", "Amount"]]
    for line in quotation.get("lines") or []:
        description = escape(str(line.get("description") or ""))
        availability = str(line.get("availability") or "")
        if availability == "MADE_TO_ORDER":
            description += "<br/><font size=8 color='#555555'>Made to order</font>"
        rows.append(
            [
                Paragraph(description, body),
                str(line.get("quantity")),
                _pdf_money(currency, line.get("unit_price")),
                _pdf_money(currency, line.get("subtotal")),
            ]
        )
    items = Table(rows, colWidths=[92 * mm, 14 * mm, 34 * mm, 34 * mm], repeatRows=1)
    items.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), accent),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F8FB")]),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )

    totals: list[list[Any]] = [["Subtotal", _pdf_money(currency, quotation.get("subtotal"))]]
    if quotation.get("tax_amount") is not None:
        rate = quotation.get("tax_rate")
        label = f"Tax ({Decimal(str(rate)).normalize()}%)" if rate is not None else "Tax"
        totals.append([label, _pdf_money(currency, quotation.get("tax_amount"))])
    totals.append(["Total", _pdf_money(currency, quotation.get("total"))])
    totals_table = Table(totals, colWidths=[40 * mm, 34 * mm], hAlign="RIGHT")
    totals_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("LINEABOVE", (0, -1), (-1, -1), 1, accent),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    notes = [
        f"This quotation is valid until {escape(str(quotation.get('expiry_date')))}.",
        escape(saas_quotations.DELIVERY_NOTE),
        "Prices are taken from our catalogue on the date of issue.",
        "This quotation does not reserve stock, create an order or request payment.",
    ]
    story: list[Any] = [
        header,
        meta,
        Spacer(1, 10),
        items,
        Spacer(1, 6),
        totals_table,
        Spacer(1, 14),
        Paragraph("<b>Notes</b>", body),
        Paragraph("<br/>".join(notes), small),
    ]
    if quotation.get("terms"):
        story.extend(
            [
                Spacer(1, 8),
                Paragraph("<b>Terms</b>", body),
                Paragraph(escape(str(quotation["terms"])).replace("\n", "<br/>"), small),
            ]
        )
    document.build(story)
    return output.getvalue()
