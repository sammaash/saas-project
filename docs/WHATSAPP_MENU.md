# WhatsApp customer menu

Phase 1 of the Cindy Bakes customer menu. The menu is a thin layer over the services
that already exist; it does not create orders, take payments, or send WhatsApp Flows.

## Enablement

The menu is **disabled by default**. It is enabled per tenant by the existing
`tenant_settings.configuration` JSONB document:

```json
{
  "menu": {
    "enabled": true
  }
}
```

`configuration` is the documented catch-all for settings that are read as a whole
(`db/migrations/0006_tenant_settings.sql`), so no migration is required. Write it with
the existing settings path (`TenantAdmin.update_settings(configuration=...)`), but never
hand-build a partial document: that replaces the whole JSONB value and can erase
`knowledge_base`. Use the merge-safe helper:

```python
from platform_admin.services import menu

menu.set_enabled(settings, tenant_id, enabled=True)
```

No Railway variable or environment variable turns the menu on.

## Quotations: a second, independent opt-in

`Request a Quotation` is offered only when the tenant has **also** enabled quotations:

```json
{
  "quotations": {
    "enabled": true
  }
}
```

Section **H. WhatsApp quotations** of the dashboard's rules tab writes exactly that key
through the partial-save endpoint (`POST /tenants/<id>/rules/quotations`), so saving the
checkbox never rewrites the other settings. The form and the JSON patch accept the usual
checkbox spellings (`1` / `true` / `on` / `yes` and `0` / `false` / `off` / `no`) and store
a real boolean; any other value is refused with `400` and nothing is written. Only a stored
JSON `true` enables the feature, so a hand-edited `"yes"`, `1` or nested object counts as
off rather than half-enabling it.

The two flags are independent. A tenant that enables quotations but not the menu still
gets quotations for spontaneous requests — the bot's tool protocol is gated on the
quotation flag alone — it just never sees the menu option. A tenant that enables the menu
but not quotations gets the six original options, because the quotation entry could only
refuse the tap.

No environment variable or Railway variable turns quotations on, and the toggle is
tenant-scoped: enabling it for one tenant says nothing about any other tenant.

## Commands and flows

| Entry | Behaviour |
|---|---|
| `menu` (case/whitespace insensitive) | Sends the main menu as a native list: six options, seven once quotations are enabled |
| `Browse Products` | Categories → product detail with configured prices/availability. Products without a category appear under **Other products**. Lists over ten entries get a **More** row |
| `Place an Order` | Instructions only; the existing conversational order tools remain authoritative |
| `My Order` | Asks for `order <number>`; looks the order up by tenant **and** sender `wa_id` |
| `Request a Quotation` | Opens a quotation request for this conversation (item, size/option, quantity); priced only from the tenant catalogue. Present only with the quotation opt-in above |
| `Delivery & Collection` | Shows configured `business_rules.delivery_fee` and `business_hours`; otherwise offers staff |
| `FAQs` | Lists `configuration.knowledge_base.faqs` (paginated with **More questions**); a tap shows the stored answer |
| `Talk to Staff` | Records and notifies through the existing handover store |

Exact option names are accepted as a text fallback (for example `browse products`).
Bare numbers are deliberately **not** commands: "1" remains ordinary conversation.

## Quotation requests

`Request a Quotation`, or the text forms `quote` / `quotation` / `get a quote`, opens a
*request* for the conversation: one open row in `public.quotation_briefs` per
`(tenant, conversation)`, restarted only once the previous one is finished.

- **Collection.** The bot asks for the item, the size or option and the quantity, and
  merges corrections into the same request rather than opening a second one.
- **Pricing.** Only the tenant's saved catalogue is used. A missing detail produces a
  targeted question; an item outside the catalogue, or a request that cannot be priced
  (a custom cake, a negotiated discount), is handed to staff with the details collected
  so far instead of inventing a number.
- **Issue.** The quotation is stored in `public.quotations` / `public.quotation_lines`
  with `source = 'whatsapp'` and no `created_by_user_id`, keyed by the inbound message id,
  so a webhook replay returns the same quotation instead of a second one. Its validity
  comes from the tenant's `quotation_valid_days` (default 14).
- **Document.** When the customer asks for the PDF ("send me the quotation PDF"), the
  file is rendered from the persisted quotation and its saved line snapshots and sent as
  a WhatsApp document. The attempt is claimed in `public.quotation_deliveries` **before**
  anything is uploaded: `sent` and `uncertain` attempts are never repeated, and only a
  definite `failed` attempt is retried on replay.
- **Fail closed.** The quotation tools refuse when quotations are disabled, when outbound
  sending is off, or in test mode. The customer is told staff will help instead of being
  promised a document that cannot be delivered.

Quotations need migrations `0036` (the WhatsApp columns on `quotations`) and `0037`
(`quotation_briefs`, `quotation_deliveries`); the menu's own state does not.

## State and security

- Every tap carries its own context: `m1.<step>.<arg>.<expiry>` (30-minute window).
  Nothing is persisted per conversation, so the menu works without migration 0035 and
  cannot leak state between conversations.
- Category and FAQ ids also carry a short digest of the item they were built from, so a
  catalogue or FAQ edit makes an old tap detectably stale instead of silently selecting a
  different item.
- The tenant always comes from the resolved WhatsApp channel; product/category/FAQ
  arguments are re-validated against that tenant's current data.
- Order status is scoped by tenant **and** the sender's `wa_id`. A missing identity fails
  closed, and another customer's order number returns the same answer as an unknown one.
  No customer name, phone number, address or history is returned.
- Interactive payloads and option names are untrusted input; unknown ids are ignored and
  expired ids produce a "reply MENU" explanation.

## Delivery and fallback

The main menu and product/FAQ lists use native list messages (maximum 10 rows); details
and confirmations use reply buttons (maximum 3). A body over 1024 characters is sent as
text so it is not truncated.

Send outcomes are classified before any fallback:

- **Definite rejection** (HTTP 4xx, builder error, sending disabled): Meta did not accept
  the interactive message, so the same reply is sent as text. This cannot duplicate it.
- **Ambiguous outcome** (timeout, connection reset, HTTP 5xx/408, malformed success body):
  the message may already have been accepted. The menu does **not** resend it as text and
  records the inbound event as `failed` with `menu_delivery_uncertain`. This is an unknown
  outcome, not a success or a definite failure.
- **Both methods definitely fail**: the event is `failed` and the outbound row is marked
  failed; it is never recorded as processed.

## Message history and receipts

Menu replies use the existing `customer_service.save_message` path: an outbound row is
created before sending and its `provider_message_id` is set after a confirmed send, so
delivery receipts match it like any other outbound message. The menu does not create a
parallel send path.

If migration `0035` (the inbound `reply_to_provider_message_id` correlation column) is not
applied, the outbound row is still recorded, but the reply is not linked to the inbound
message and the at-most-one-reply backstop is unavailable. The menu remains safe to run on
the 0034 baseline; do not apply 0035 as part of enabling the menu.

## Known limitations

- `0035` is not required, but without it menu replies have no inbound correlation.
- Quotations do need `0036` **and** `0037`; the opt-in checkbox saves fine without them, but
  the request would then fail at the first write. Apply both migrations before enabling
  quotations.
- No order creation, payment processing or WhatsApp Flows are added.
- The menu does not enable itself for a channel that has no active tenant configuration.
- `conversations.menu_state*` (migration 0035) is intentionally unused: the stateless ids
  make it unnecessary for phase 1.
- Pagination shows at most ten rows per page; there is no free-text page number.
- Quotation PDFs are rendered per request and never stored: a resend re-renders the same
  document from the saved snapshots, and a `uncertain` delivery is not resent on purpose.

## Tests

`tests/test_whatsapp_menu.py` covers command handling, payload limits, tap parsing,
stale/expired ids, empty and populated catalogues, uncategorised products, pagination,
missing prices, order privacy, handover success/failure, definite vs ambiguous send
failures, outbound persistence, the disabled-by-default behaviour, and the quotation row
appearing only for opted-in tenants. All database access is faked; no test sends a real
WhatsApp message.

`tests/test_whatsapp_quotations.py` covers the quotation journey end to end at the service
and tool layers against a real database: brief collection and corrections, server-side
pricing, feature gates, PDF delivery, webhook replay, definite and ambiguous send failures,
and tenant/customer isolation.

`tests/test_tenant_config_partial_updates.py` covers the admin toggle itself: default off,
save and read agreement, opt-in-only writes, an unchecked box writing `false`, other
sections leaving the flag alone, unknown values rejected, non-administrators refused, and
cross-tenant isolation.

`tests/test_menu_db.py` adds database-backed order-privacy and staff-assistance tests. The
database suites require an explicit `SAAS_TEST_DATABASE_URL` naming a disposable local
database (for example `postgresql://postgres:...@127.0.0.1:5432/saas_phase1_test`); they
never fall back to `SAAS_DATABASE_URL`. Set `SAAS_TEST_APP_DATABASE_URL` (or use the
`db/local` loopback grants) for tests that exercise `app_backend`.
`tests/test_test_database_guard.py` proves that unsafe or missing configuration is rejected
before `psycopg.connect` or `apply_migrations` can run.
