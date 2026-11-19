# WhatsApp quotations (portal redesign Stage 3)

Quotations are a **deterministic platform workflow**. The AI model never prices, never
issues a quotation and is no longer offered the quotation tools.

## Which path handles a customer message

Messages from AI-active conversations only. A paused or human-handled conversation gets
no automated work at all (Stage 2 gate).

| Message | Handled by | Result |
|---|---|---|
| "talk to a person" | Stage 2 human request | AI paused, orange alert, acknowledgement |
| Discount, special price, negotiation ("best price", "too expensive", "can you do 3000") | `_handle_human_request(source="discount_request")` | AI paused for this conversation, staff handover, platform acknowledgement. Never negotiated by the bot |
| Explicit quotation request ("quote", "quotation", "proforma"), or a reply while a request is being collected (≤ 24 h) | `platform_admin/services/quotation_flow.py` | Questions, then quotation + PDF |
| Delivery enquiry ("do you deliver", "delivery to X") | `_quotation_or_delivery_turn` | Asks for the location, then a staff handover to confirm the charge. The AI is **not** paused. Never priced |
| Ordinary price question ("how much is a 1kg vanilla?") | The AI, from the catalogue | Text answer only. No quotation, no PDF |

The quotation and delivery paths require `configuration.quotations.enabled = true`.
Delivery handling is skipped when `configuration.delivery_available` is `false`.

## Quotation steps

1. **Reading the request.**
   - The catalogue matcher (`saas/quotation_intent.py`) reads product names, whole-kg sizes and quantities against this tenant's active catalogue.
   - When the wording names something the matcher cannot read, **one** strict JSON extraction call runs. Product names are accepted only if they exactly match a catalogue name; anything else becomes "unlisted".
2. **Missing details.** `evaluate_brief` resolves the request against the live catalogue. The bot asks **one** question at a time: product, then size/option, then quantity, then the customer's name (skipped when the WhatsApp profile name or the customer record has one). Corrections ("make it 3kg") update the same request.
3. **Unlisted or unavailable items.** A staff handover (`handover_requests`, product reason). The AI is not paused.
4. **Issue.** `saas.quotations.create_quotation_from_brief`:
   - catalogue prices and immutable snapshot lines;
   - tax only if `configuration.tax_rate` is set;
   - delivery never added;
   - idempotency key = the inbound WhatsApp message id;
   - a unique `Q-YYYYMMDD-XXXXXXXX` reference (checked before insert).
5. **PDF.** Rendered once from the snapshot (`platform_admin/services/quotations.py::quotation_pdf`) and stored in `quotation_documents` (migration 0044, immutable).
6. **Send.**
   - The text summary goes first, as a recorded reply row through the dispatch boundary.
   - Then the PDF document:
     - the `quotation_deliveries` row is claimed first;
     - the document message row is recorded before the send;
     - it is dispatched with the Stage 2 mode recheck.
   - `sent` is recorded only with WhatsApp's message id. Delivered/read come from Meta's status webhooks on that message row.
7. **Sending disabled** (staging). The quotation and PDF are still generated and stored. The delivery is recorded `failed / sending_disabled` and the dashboard shows **Not sent**.

## Dashboard (tenant portal, owner and staff)

- `/t/<tenant>/quotations` lists every quotation with:
  - reference, customer, WhatsApp number, issue date, total;
  - status **Generated / Sent / Not sent / Expired** (with WhatsApp's delivered/read receipt when one exists);
  - **View** and **Download** PDF.
- `/t/<tenant>/quotations/<id>/pdf` serves the stored document (`?download=1` as an attachment) with `Cache-Control: private, no-store`.
- **Resend PDF** is offered only when the last attempt definitely failed and nothing was ever sent.
  - An `uncertain` attempt is never resent automatically.
  - A resend is a staff send, so it is allowed while the AI is paused.
  - Every form carries a one-time key, so a double submit sends once.

## Customer-facing output gate (all AI replies)

`saas/output_safety.py` runs on the final text of every `BotResponseEngine` turn, for all tenants and all tool workflows. It blocks:

- raw `TOOL_CALL` / `TOOL_RESULT` text;
- tool argument JSON and internal identifiers;
- tool error text;
- an empty reply;
- an exhausted tool budget;
- false claims of having sent or prepared a document;
- promises of later work with no recorded handover.

A blocked reply is replaced with a platform fallback that offers "talk to a person", and the event is logged as `Customer output blocked … reason=…` / `AI OUTPUT BLOCKED …`.
