"""Workflow-based authorisation for model-requested tools (Phase 1, S2; defect D12).

Approved rules (roadmap §3.3 item 2 and §4.5; decision DP-4, shared for all tenants):

* catalogue, business rules and staff handover: always allowed;
* order-draft and pricing tools (``get_order_draft``, ``update_order_draft``,
  ``calculate_quote``): only inside an ``order`` workflow with ordering enabled;
* ``confirm_order``: additionally needs explicit confirmation recorded in state and
  live mode (never the playground);
* ``get_order_status``: only with a verified sender identity (M-1);
* ``get_payment_status``: staff only;
* quotation tools: never model-callable (quotations are issued deterministically);
* anything else: refused.

A refused call is never executed; the caller logs it and returns a refusal to the
model. Until engine v2 introduces workflow state (later stages), no turn is in an
``order`` workflow, so the order-draft and confirm tools are refused for every
tenant. No tenant has order or payment data (roadmap §4.5), so no capability in use
is lost.
"""

from __future__ import annotations

from dataclasses import dataclass

ALWAYS_ALLOWED = frozenset({"get_catalogue", "get_business_rules", "request_staff_handover"})
ORDER_WORKFLOW_TOOLS = frozenset({"get_order_draft", "update_order_draft", "calculate_quote"})
CONFIRM_TOOLS = frozenset({"confirm_order"})
IDENTITY_TOOLS = frozenset({"get_order_status"})
STAFF_TOOLS = frozenset({"get_payment_status"})
QUOTATION_TOOLS = frozenset({"update_quotation_brief", "request_quote", "request_quotation_pdf"})

MODES = ("live", "playground", "evaluation")
REFUSAL_TEXT = "This action is not available in this conversation."


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def decide(
    tool: str,
    *,
    workflow: str | None = None,
    ordering_enabled: bool = False,
    confirmed: bool = False,
    mode: str = "live",
    identity: str | None = None,
    is_staff: bool = False,
) -> Decision:
    """Whether the model may run ``tool`` in this turn, and why not."""
    name = str(tool or "")
    if mode not in MODES:
        return Decision(False, "unknown_mode")
    if name in ALWAYS_ALLOWED:
        return Decision(True, "allowed")
    if name in QUOTATION_TOOLS:
        return Decision(False, "quotation_tools_not_model_callable")
    if name in STAFF_TOOLS:
        return Decision(True, "allowed") if is_staff else Decision(False, "staff_only")
    if name in IDENTITY_TOOLS:
        if identity and str(identity).strip():
            return Decision(True, "allowed")
        return Decision(False, "identity_not_verified")
    if name in ORDER_WORKFLOW_TOOLS or name in CONFIRM_TOOLS:
        if workflow != "order":
            return Decision(False, "requires_order_workflow")
        if not ordering_enabled:
            return Decision(False, "ordering_disabled")
        if name in CONFIRM_TOOLS:
            if not confirmed:
                return Decision(False, "confirmation_missing")
            if mode != "live":
                return Decision(False, "live_mode_only")
        return Decision(True, "allowed")
    return Decision(False, "unknown_tool")


def allowed(tool: str, **kwargs) -> bool:
    return decide(tool, **kwargs).allowed
