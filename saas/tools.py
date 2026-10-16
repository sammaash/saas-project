"""Generic AI tool-execution layer for the SaaS bot.

This module provides a text-based TOOL_CALL protocol that works with any
text-only chat provider (no native function calling required). All tools run
server-side: tenant identity is resolved from ToolContext and never trusted
from model input; prices are computed from persisted catalogue data, never
from model-supplied values.

Tool protocol:
    TOOL_CALL: {"name": "get_catalogue", "arguments": {}}

The provider emits zero or more TOOL_CALL lines in its response text. The
host engine parses them, executes each tool once, and feeds results back
as structured tool-result messages so the model can compose a final reply.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .tenant_admin import tenant_admin

logger = logging.getLogger("saas.tools")

MAX_TOOL_CALLS = 5
TOOL_TIMEOUT_SECONDS = 10.0

_TOOL_CALL_PREFIX = "TOOL_CALL:"


def _extract_balanced_json(text: str, start: int) -> tuple[str, int] | None:
    """Extract a balanced-brace JSON object starting at ``start`` (the '{').

    Returns the JSON text and the index just past the closing '}', or None
    if the braces are unbalanced.
    """
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1], i + 1
    return None


@dataclass(frozen=True)
class ToolContext:
    """Server-side context for a tool call.

    tenant_id is resolved by the host (from the session/channel), never from
    model input. customer_key identifies the end customer (e.g. WhatsApp WA ID).
    is_staff marks dashboard/staff callers who may use staff-only tools.
    """

    tenant_id: str
    customer_key: str | None = None
    is_staff: bool = False

    def require_staff(self) -> None:
        if not self.is_staff:
            raise PermissionError("This tool is staff-only.")


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None


class Tool(Protocol):
    """A server-executable tool.

    name: snake-case tool name the model references in TOOL_CALL.
    description: human-readable hint for the system prompt.
    parameters: JSON Schema (draft-07 subset) describing arguments.
    execute(context, arguments): run the tool server-side, return ToolResult.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def execute(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        ...


class ToolRegistry:
    """Register and look up tools by name."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)


class GetCatalogueTool:
    name = "get_catalogue"
    description = (
        "List the tenant's active products and priced variants. Use this to "
        "answer what is for sale, prices, availability and stock."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        tenant_id = _resolve_tenant_id(context)
        try:
            with tenant_admin(tenant_id) as admin:
                products = admin.list_catalogue()
        except Exception:  # noqa: BLE001
            logger.exception("get_catalogue failed for tenant %s", tenant_id)
            return ToolResult(ok=False, error="Could not load the catalogue.")
        return ToolResult(
            ok=True,
            data={"products": products or []},
        )


class GetBusinessRulesTool:
    name = "get_business_rules"
    description = (
        "Return the tenant's configured business rules: currency, deposit rate, "
        "business hours, timezone and persona."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        tenant_id = _resolve_tenant_id(context)
        try:
            with tenant_admin(tenant_id) as admin:
                settings = admin.get_settings()
        except Exception:  # noqa: BLE001
            logger.exception("get_business_rules failed for tenant %s", tenant_id)
            return ToolResult(ok=False, error="Could not load business rules.")
        if settings is None:
            return ToolResult(ok=False, error="No business rules configured.")
        business_rules = settings.get("business_rules") or {}
        return ToolResult(
            ok=True,
            data={
                "currency": settings.get("currency"),
                "deposit_rate": business_rules.get("deposit_rate"),
                "delivery_fee": business_rules.get("delivery_fee"),
                "business_hours": settings.get("business_hours"),
                "timezone": settings.get("timezone"),
                "bot_persona": settings.get("bot_persona"),
            },
        )


class GetOrderDraftTool:
    name = "get_order_draft"
    description = (
        "Get the customer's current draft order for this conversation, if one "
        "exists. Returns the order's items, totals and status so the assistant "
        "can summarise the draft before confirmation."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        tenant_id = _resolve_tenant_id(context)
        customer_key = context.customer_key
        if not customer_key:
            return ToolResult(
                ok=False, error="No customer key available for this conversation."
            )
        try:
            with tenant_admin(tenant_id) as admin:
                order_id = admin.get_order_draft_id_by_whatsapp(customer_key)
                if order_id is None:
                    return ToolResult(
                        ok=True,
                        data={"order": None, "message": "No draft order yet."},
                    )
                order = admin.get_order(order_id)
                if order is None:
                    return ToolResult(ok=False, error="Draft order not found.")
                items = admin.list_order_items(order_id)
        except ValueError as error:
            return ToolResult(ok=False, error=str(error))
        except Exception:  # noqa: BLE001
            logger.exception("get_order_draft failed for tenant %s", tenant_id)
            return ToolResult(ok=False, error="Could not read the draft order.")
        return ToolResult(
            ok=True,
            data={
                "order": {
                    "id": str(order["id"]),
                    "order_number": order["order_number"],
                    "status": order["status"],
                    "currency": order["currency"],
                    "subtotal": _money_to_float(order["subtotal"]),
                    "discount": _money_to_float(order["discount"]),
                    "total": _money_to_float(order["total"]),
                    "required_deposit": _money_to_float(order["required_deposit"]),
                    "amount_paid": _money_to_float(order["amount_paid"]),
                    "outstanding_balance": _money_to_float(
                        order["outstanding_balance"]
                    ),
                    "payment_status": order["payment_status"],
                    "customer_name": order.get("customer_name"),
                    "customer_phone": order.get("customer_phone"),
                    "fulfilment": order.get("fulfilment"),
                    "items": items or [],
                }
            },
        )


def _require_positive_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a positive integer.") from None
    if result < 1:
        raise ValueError(f"{field} must be at least 1.")
    return result


def _require_money(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a valid monetary amount.") from None
    if result < 0:
        raise ValueError(f"{field} must not be negative.")
    return result


def _money_to_float(value: Any) -> float:
    return float(Decimal(str(value)))


def _resolve_tenant_id(context: ToolContext) -> str:
    """Return the server-resolved tenant id; model-supplied ids are ignored."""
    return str(context.tenant_id)


def _parse_json_datetime(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {value!r}")


class UpdateOrderDraftTool:
    name = "update_order_draft"
    description = (
        "Create or update the customer's draft order. Adds catalogue-validated "
        "items (product name + weight variant), sets customer details, and reads "
        "the draft back. Idempotent: calling again with the same item updates "
        "quantity in place."
    )
    parameters = {
        "type": "object",
        "properties": {
            "product_name": {"type": "string", "maxLength": 200},
            "weight_kg": {"type": "integer", "minimum": 1},
            "quantity": {"type": "integer", "minimum": 1},
            "customer_name": {"type": "string", "maxLength": 200},
            "customer_phone": {"type": "string", "maxLength": 30},
            "fulfilment": {"type": "string", "enum": ["pickup", "delivery"]},
        },
        "additionalProperties": False,
    }

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        tenant_id = _resolve_tenant_id(context)
        customer_key = context.customer_key
        if not customer_key:
            return ToolResult(
                ok=False, error="No customer key available for this conversation."
            )

        product_name = str(arguments.get("product_name", "")).strip()
        weight_kg = arguments.get("weight_kg")
        quantity = arguments.get("quantity", 1)

        if product_name:
            if weight_kg is None:
                return ToolResult(
                    ok=False,
                    error="weight_kg is required when adding a product.",
                )
            try:
                weight_kg_int = _require_positive_int(weight_kg, "weight_kg")
            except ValueError as error:
                return ToolResult(ok=False, error=str(error))
            try:
                quantity_int = _require_positive_int(quantity, "quantity")
            except ValueError as error:
                return ToolResult(ok=False, error=str(error))

        updates: dict[str, Any] = {}
        if arguments.get("customer_name"):
            updates["customer_name"] = str(arguments["customer_name"]).strip()
        if arguments.get("customer_phone"):
            updates["customer_phone"] = str(arguments["customer_phone"]).strip()
        if arguments.get("fulfilment"):
            updates["fulfilment"] = arguments["fulfilment"]

        try:
            with tenant_admin(tenant_id) as admin:
                order_id = admin.get_order_draft_id_by_whatsapp(customer_key)
                if order_id is None:
                    if not product_name:
                        return ToolResult(
                            ok=False,
                            error="No draft order to update. Specify a product to create one.",
                        )
                    order_id = admin.create_order(
                        whatsapp_wa_id=customer_key,
                    )

                if updates:
                    admin.update_order(order_id, **updates)

                if product_name:
                    lookup = admin.lookup_price(product_name, weight_kg_int)
                    if lookup is None:
                        return ToolResult(
                            ok=False,
                            error=(
                                f"Could not find '{product_name}' at "
                                f"{weight_kg_int}kg in the catalogue."
                            ),
                        )
                    variant = lookup["variant"]
                    admin.add_order_item(
                        order_id,
                        name=lookup["product"]["name"],
                        quantity=quantity_int,
                        unit_price=variant["price"],
                        variant_id=str(variant["id"]),
                        product_id=str(lookup["product"]["id"]),
                        sku=variant.get("sku"),
                    )

                order = admin.get_order(order_id)
                items = admin.list_order_items(order_id)
        except ValueError as error:
            return ToolResult(ok=False, error=str(error))
        except Exception:  # noqa: BLE001
            logger.exception("update_order_draft failed for tenant %s", tenant_id)
            return ToolResult(ok=False, error="Could not update the draft order")

        return ToolResult(
            ok=True,
            data={
                "order": {
                    "id": str(order["id"]),
                    "order_number": order["order_number"],
                    "status": order["status"],
                    "subtotal": _money_to_float(order["subtotal"]),
                    "total": _money_to_float(order["total"]),
                    "required_deposit": _money_to_float(order["required_deposit"]),
                    "items": items or [],
                },
                "message": "Draft updated." if product_name else "Draft details saved.",
            },
        )


class CalculateQuoteTool:
    name = "calculate_quote"
    description = (
        "Calculate the full quote for the current draft order: subtotal, discount, "
        "delivery fee, total, deposit rate and required deposit. All amounts are "
        "computed server-side from the persisted catalogue and order policy."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        tenant_id = _resolve_tenant_id(context)
        customer_key = context.customer_key
        if not customer_key:
            return ToolResult(
                ok=False, error="No customer key available for this conversation."
            )
        try:
            with tenant_admin(tenant_id) as admin:
                order_id = admin.get_order_draft_id_by_whatsapp(customer_key)
                if order_id is None:
                    return ToolResult(
                        ok=False, error="No draft order to quote. Add items first."
                    )
                quote = admin.quote_order(order_id)
        except ValueError as error:
            return ToolResult(ok=False, error=str(error))
        except Exception:  # noqa: BLE001
            logger.exception("calculate_quote failed for tenant %s", tenant_id)
            return ToolResult(ok=False, error="Could not calculate the quote.")
        return ToolResult(
            ok=True,
            data={
                "order_number": quote["order_number"],
                "status": quote["status"],
                "currency": quote["currency"],
                "items": quote["items"],
                "subtotal": _money_to_float(quote["subtotal"]),
                "discount": _money_to_float(quote["discount"]),
                "delivery_fee": _money_to_float(quote["delivery_fee"]),
                "total": _money_to_float(quote["total"]),
                "deposit_rate": quote["deposit_rate"],
                "required_deposit": _money_to_float(quote["required_deposit"]),
                "amount_paid": _money_to_float(quote["amount_paid"]),
                "outstanding_balance": _money_to_float(quote["outstanding_balance"]),
                "payment_status": quote["payment_status"],
            },
        )


class ConfirmOrderTool:
    name = "confirm_order"
    description = (
        "Confirm the customer's current draft order, moving it from 'draft' to "
        "'pending_payment'. Requires explicit customer confirmation. Generates "
        "an order number and sets the required deposit. Idempotent: retries with "
        "the same session do not create duplicate orders."
    )
    parameters = {
        "type": "object",
        "properties": {
            "confirmed": {"type": "boolean"},
            "customer_name": {"type": "string", "maxLength": 200},
            "customer_phone": {"type": "string", "maxLength": 30},
        },
        "required": ["confirmed"],
        "additionalProperties": False,
    }

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        if arguments.get("confirmed") is not True:
            return ToolResult(
                ok=False,
                error="Order confirmation requires explicit customer consent (confirmed=true).",
            )

        tenant_id = _resolve_tenant_id(context)
        customer_key = context.customer_key
        if not customer_key:
            return ToolResult(
                ok=False, error="No customer key available for this conversation."
            )

        try:
            with tenant_admin(tenant_id) as admin:
                order_id = admin.get_order_draft_id_by_whatsapp(customer_key)
                if order_id is None:
                    return ToolResult(
                        ok=False, error="No draft order to confirm. Add items first."
                    )
                order = admin.get_order(order_id)
                if order is None:
                    return ToolResult(ok=False, error="Draft order not found.")
                if order["status"] != "draft":
                    return ToolResult(
                        ok=False,
                        error=f"Order is already {order['status']} and cannot be re-confirmed.",
                    )

                updates: dict[str, Any] = {}
                if arguments.get("customer_name"):
                    updates["customer_name"] = str(arguments["customer_name"]).strip()
                if arguments.get("customer_phone"):
                    updates["customer_phone"] = str(arguments["customer_phone"]).strip()
                if updates:
                    admin.update_order(order_id, **updates)

                admin.confirm_order(order_id)

                order = admin.get_order(order_id)
        except ValueError as error:
            return ToolResult(ok=False, error=str(error))
        except PermissionError as error:
            return ToolResult(ok=False, error=str(error))
        except Exception:  # noqa: BLE001
            logger.exception("confirm_order failed for tenant %s", tenant_id)
            return ToolResult(ok=False, error="Could not confirm the order.")

        return ToolResult(
            ok=True,
            data={
                "order": {
                    "id": str(order["id"]),
                    "order_number": order["order_number"],
                    "status": order["status"],
                    "total": _money_to_float(order["total"]),
                    "required_deposit": _money_to_float(order["required_deposit"]),
                    "outstanding_balance": _money_to_float(
                        order["outstanding_balance"]
                    ),
                    "currency": order.get("currency"),
                    "payment_status": order.get("payment_status"),
                },
                "message": (
                    f"Order {order['order_number']} confirmed. "
                    f"Total {order.get('currency')} {order['total']}, "
                    f"deposit {order['required_deposit']} due."
                ),
            },
        )


class GetOrderStatusTool:
    name = "get_order_status"
    description = (
        "Look up an order's status and payment progress by order number. "
        "Returns status, total, deposit, amount paid and outstanding balance."
    )
    parameters = {
        "type": "object",
        "properties": {
            "order_number": {"type": "string", "maxLength": 80},
        },
        "required": ["order_number"],
        "additionalProperties": False,
    }

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        tenant_id = _resolve_tenant_id(context)
        order_number = str(arguments.get("order_number", "")).strip()
        if not order_number:
            return ToolResult(ok=False, error="Order number is required.")

        try:
            with tenant_admin(tenant_id) as admin:
                order = admin.get_order_by_number(order_number)
        except ValueError as error:
            return ToolResult(ok=False, error=str(error))
        except Exception:  # noqa: BLE001
            logger.exception("get_order_status failed for tenant %s", tenant_id)
            return ToolResult(ok=False, error="Could not look up the order.")
        if order is None:
            return ToolResult(ok=False, error="Order not found.")

        try:
            with tenant_admin(tenant_id) as admin:
                status = admin.get_order_payment_status(str(order["id"]))
        except ValueError as error:
            return ToolResult(ok=False, error=str(error))
        except Exception:  # noqa: BLE001
            logger.exception(
                "get_order_status payment lookup failed for tenant %s", tenant_id
            )
            return ToolResult(ok=False, error="Could not read payment status.")

        return ToolResult(
            ok=True,
            data={
                "order_number": status["order_number"],
                "status": status["status"],
                "total": _money_to_float(Decimal(str(status["total"]))),
                "required_deposit": _money_to_float(
                    Decimal(str(status["required_deposit"]))
                ),
                "amount_paid": _money_to_float(Decimal(str(status["amount_paid"]))),
                "outstanding_balance": _money_to_float(
                    Decimal(str(status["outstanding_balance"]))
                ),
                "payment_status": status["payment_status"],
                "fully_paid": status["fully_paid"],
                "deposit_satisfied": status["deposit_satisfied"],
            },
        )


class GetPaymentStatusTool:
    name = "get_payment_status"
    description = (
        "Return payment details for an order: total, amount paid, outstanding "
        "balance, deposit status and payment status. Staff-only for payment "
        "verification questions; customers can use get_order_status instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "order_number": {"type": "string", "maxLength": 80},
        },
        "required": ["order_number"],
        "additionalProperties": False,
    }

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        tenant_id = _resolve_tenant_id(context)
        order_number = str(arguments.get("order_number", "")).strip()
        if not order_number:
            return ToolResult(ok=False, error="Order number is required.")

        try:
            context.require_staff()
            with tenant_admin(tenant_id) as admin:
                order = admin.get_order_by_number(order_number)
        except ValueError as error:
            return ToolResult(ok=False, error=str(error))
        except PermissionError as error:
            return ToolResult(ok=False, error=str(error))
        except Exception:  # noqa: BLE001
            logger.exception("get_payment_status failed for tenant %s", tenant_id)
            return ToolResult(ok=False, error="Could not look up the order.")
        if order is None:
            return ToolResult(ok=False, error="Order not found.")

        try:
            with tenant_admin(tenant_id) as admin:
                status = admin.get_order_payment_status(str(order["id"]))
                payments = admin.list_order_payments(str(order["id"]))
        except ValueError as error:
            return ToolResult(ok=False, error=str(error))
        except Exception:  # noqa: BLE001
            logger.exception(
                "get_payment_status detail lookup failed for tenant %s", tenant_id
            )
            return ToolResult(ok=False, error="Could not read payment details.")

        return ToolResult(
            ok=True,
            data={
                "order_number": status["order_number"],
                "payment_status": status["payment_status"],
                "total": _money_to_float(Decimal(str(status["total"]))),
                "amount_paid": _money_to_float(Decimal(str(status["amount_paid"]))),
                "outstanding_balance": _money_to_float(
                    Decimal(str(status["outstanding_balance"]))
                ),
                "required_deposit": _money_to_float(
                    Decimal(str(status["required_deposit"]))
                ),
                "deposit_satisfied": status["deposit_satisfied"],
                "fully_paid": status["fully_paid"],
                "payments": [
                    {
                        "id": str(p["id"]),
                        "kind": p["kind"],
                        "amount": _money_to_float(Decimal(str(p["amount"]))),
                        "method": p.get("method"),
                        "reference": p.get("reference"),
                        "status": p["status"],
                        "recorded_at": str(p.get("created_at", "")),
                    }
                    for p in payments
                ],
            },
        )


def default_tool_registry() -> ToolRegistry:
    """A registry with every built-in tool, ready to wire into an engine."""
    registry = ToolRegistry()
    registry.register(GetCatalogueTool())
    registry.register(GetBusinessRulesTool())
    registry.register(GetOrderDraftTool())
    registry.register(UpdateOrderDraftTool())
    registry.register(CalculateQuoteTool())
    registry.register(ConfirmOrderTool())
    registry.register(GetOrderStatusTool())
    registry.register(GetPaymentStatusTool())
    return registry


def parse_tool_calls(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Extract ``TOOL_CALL: {...}`` invocations from provider output.

    Returns a list of (name, arguments) tuples in order of appearance.
    Malformed JSON or missing names are skipped (treated as plain text).
    Nested braces inside the JSON are handled correctly.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    search_from = 0
    while True:
        idx = text.find(_TOOL_CALL_PREFIX, search_from)
        if idx == -1:
            break
        json_start = idx + len(_TOOL_CALL_PREFIX)
        # Skip whitespace after the colon.
        while json_start < len(text) and text[json_start] in " \t\r\n":
            json_start += 1
        if json_start >= len(text) or text[json_start] != "{":
            search_from = idx + len(_TOOL_CALL_PREFIX)
            continue
        extracted = _extract_balanced_json(text, json_start)
        if extracted is None:
            search_from = json_start + 1
            continue
        json_text, _next = extracted
        try:
            data = json.loads(json_text)
        except (ValueError, TypeError):
            search_from = _next
            continue
        if not isinstance(data, dict):
            search_from = _next
            continue
        name = data.get("name")
        if not isinstance(name, str) or not name:
            search_from = _next
            continue
        arguments = data.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append((name, arguments))
        search_from = _next
    return calls


def execute_tool_calls(
    registry: ToolRegistry,
    context: ToolContext,
    calls: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Execute a list of tool calls and return tool-result messages.

    Each call is executed once. Unknown tools and errors are captured as
    ToolResult(ok=False) entries so the model can see what went wrong and
    respond accordingly.
    """
    results: list[dict[str, Any]] = []
    for name, arguments in calls:
        tool = registry.get(name)
        if tool is None:
            results.append(
                {
                    "tool": name,
                    "ok": False,
                    "error": f"Unknown tool: {name}.",
                }
            )
            continue
        try:
            start = time.perf_counter()
            result = tool.execute(context, arguments)
            elapsed_ms = (time.perf_counter() - start) * 1_000
            entry: dict[str, Any] = {
                "tool": name,
                "ok": result.ok,
            }
            if result.ok and result.data is not None:
                entry["data"] = result.data
            if not result.ok and result.error:
                entry["error"] = result.error
            entry["elapsed_ms"] = round(elapsed_ms, 1)
            results.append(entry)
        except Exception:  # noqa: BLE001
            logger.exception("Tool %s raised for tenant %s", name, context.tenant_id)
            results.append(
                {
                    "tool": name,
                    "ok": False,
                    "error": "Tool execution failed.",
                }
            )
    return results