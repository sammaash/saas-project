# WhatsApp customer menu

Phase 1 of the Cindy Bakes customer menu. The menu is a thin layer over the services
that already exist; it does not create orders, take payments, or send WhatsApp Flows.

## Enablement

The menu is **disabled by default**. It is enabled per tenant by the existing
`tenant_settings.configuration` JSONB document:

```json
{
  "menu": {
    "enabled": true,
    "welcome_message": "Main menu:",
    "language": "en",
    "options": [
      {"id": "browse_products", "label": "Browse Products",
       "action": "browse_products", "enabled": true, "position": 1},
      {"id": "place_order", "label": "Place an Order",
       "action": "place_order", "enabled": true, "position": 2},
      {"id": "my_order", "label": "My Order",
       "action": "my_order", "enabled": true, "position": 3},
      {"id": "delivery_collection", "label": "Delivery & Collection",
       "action": "delivery_collection", "enabled": true, "position": 4},
      {"id": "faqs", "label": "FAQs",
       "action": "faqs", "enabled": true, "position": 5},
      {"id": "handover", "label": "Talk to Staff",
       "action": "handover", "enabled": true, "position": 6}
    ]
  }
}
```

`options` is optional for backward compatibility: a tenant with only
`{"menu":{"enabled":true}}` gets the six historical options above. An explicitly empty
`options` list means the menu has no rows until the operator adds some.

`configuration` is the documented catch-all for settings that are read as a whole
(`db/migrations/0006_tenant_settings.sql`), so no migration is required. The tenant's
**Rules / AI** page (`/tenants/<tenant-id>/rules`) exposes this as the
**H. WhatsApp customer menu** section with an **Enable WhatsApp Menu** checkbox and the
**Menu Builder**. The section uses the normal platform-admin authorization, tenant-scoped
RLS, CSRF, version, and audit path.

The API/helper path remains available for provisioning and migration tooling. Write it
with the existing settings path (`TenantAdmin.update_settings(configuration=...)`), but
never hand-build a partial document: that replaces the whole JSONB value and can erase
`knowledge_base`. Use the merge-safe helper:

```python
from platform_admin.services import menu

menu.set_enabled(settings, tenant_id, enabled=True)
```

No Railway variable or environment variable turns the menu on.

## Menu Builder

The Rules / AI page renders the stored options as editable rows. Each row has:

- **id**: stable lowercase identifier (`a-z`, `0-9`, `-`, `_`, at most 32 characters). Tap
  actions carry this id, so renaming a label does not invalidate an in-flight button.
  Existing valid ids are preserved exactly. A new row may leave the id blank; the server
  derives a unique id from the label (`Talk to Staff` becomes `talk_to_staff`) and adds a
  numeric suffix if needed.
- **label**: the WhatsApp row title, at most 24 characters.
- **action**: one of the fixed action types `browse_products`, `place_order`, `my_order`,
  `delivery_collection`, `faqs`, `handover`. Configuration is data, never executable code.
- **on**: whether the row is displayed and executable.
- **position**: a unique positive number from 1 to 10; rows are rendered in this order.

The form also edits the welcome message (at most 300 characters) and language tag. The
builder validates the whole submitted list before writing: blank labels, duplicate ids,
duplicate positions, unknown actions, overlong labels, and out-of-range positions are
rejected without changing stored configuration. At most ten options are accepted, matching
the native WhatsApp list limit. The browser-side preview is only a convenience; the server
renders the same preview from stored values and is authoritative.

Saving the menu section uses the existing `deep_merge` path and writes only
`configuration.menu` keys. This preserves sibling configuration, custom menu keys, and
all other sections. The menu simulator resets its conversation snapshot when the saved menu
signature changes, so a builder save is visible on the next simulator load without a
process restart.

## Menu Simulator

`/playground/menu` renders real `MenuReply` objects from
`platform_admin.services.menu` for the selected tenant. It never calls the WhatsApp
webhook, never calls a WhatsApp provider, and never sends a message. `make_context()`
forces `whatsapp_send_enabled=False`, uses synthetic `sim-<uuid>` message ids, and uses a
synthetic customer identity (`simulator-<admin-id>`) instead of a real phone number.

### Database access

- The simulator is **loopback-only by default**.
- Set `MENU_SIMULATOR_ALLOW_REMOTE_DATABASE=true` to allow the already-configured
  non-loopback `SAAS_DATABASE_URL`. The simulator uses that existing configured
  connection; it does not introduce a second connection path, credential source, or
  hardcoded production URL.
- Unset or `false` keeps loopback-only behavior. Only the literal `true` (case-insensitive)
  enables remote access; other spellings such as `1`, `yes` or `on` fail closed at startup
  with a `ConfigError`.
- The normal `/playground` AI path is unchanged.

### Production write risk

Remote mode is intended for controlled testing. If `SAAS_DATABASE_URL` points at a
production database, simulator actions may write there:

- Creating or reusing the synthetic `simulator-<admin-id>` customer and its conversation
  in the selected tenant/channel (`customers`, `conversations`). This can happen for
  read-only menu actions too because the simulator prepares the same identity used by
  staff handover.
- The **Talk to Staff** action creates a `handover_requests` row for that synthetic
  customer. With `whatsapp_send_enabled=False`, the staff notification attempt fails
  without a provider call and the row is recorded as failed/visible for the operator.

The simulator does **not** write tenant configuration/menu settings, catalogue data,
orders, messages, webhook events, or audit rows, and it never changes another tenant or a
real customer. Tenant scope still comes from the authorized dashboard tenant, and RLS
still applies. Use a dedicated test tenant and clean up synthetic rows after testing;
never enable remote mode casually against a customer-facing tenant.

## Commands and flows

| Entry | Behaviour |
|---|---|
| `menu` (case/whitespace insensitive) | Sends the configured main menu as a native list; with no saved options it shows the six historical defaults |
| `browse_products` action | Categories → product detail with configured prices/availability. Products without a category appear under **Other products**. Lists over ten entries get a **More** row |
| `place_order` action | Instructions only; the existing conversational order tools remain authoritative |
| `my_order` action | Asks for `order <number>`; looks the order up by tenant **and** sender `wa_id` |
| `delivery_collection` action | Shows configured `business_rules.delivery_fee` and `business_hours`; otherwise offers staff |
| `faqs` action | Lists `configuration.knowledge_base.faqs` (paginated with **More questions**); a tap shows the stored answer |
| `handover` action | Records and notifies through the existing handover store |

Labels are tenant-defined. Exact labels are accepted as a text fallback (for example a
configured `Browse Products`), and taps always use the stable option id. Legacy aliases
such as `browse products` and `order <number>` still work for compatibility. Bare numbers
are deliberately **not** commands: "1" remains ordinary conversation.

Disabled options are neither shown in the main menu nor executed by a tap or text command.
If a saved option is malformed or uses an unknown action, the menu fails closed with a safe
message rather than running an unexpected path.

## State and security

- Every tap carries its own context: `m1.<step>.<arg>.<expiry>` (30-minute window).
  Configured menu rows use `m1.option.<option-id>.<expiry>` and resolve the **current**
  saved option before executing it, so a disabled or deleted option is rejected. Nothing is
  persisted per conversation, so the menu works without migration 0035 and cannot leak
  state between conversations.
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
- No order creation, payment processing or WhatsApp Flows are added.
- `language` is stored metadata only; menu copy is not translated automatically. Operators
  must write the welcome message and labels in the desired language.
- The webhook uses a deliberately cheap upper-case hint before loading settings for a custom
  free-text label. A lowercase custom label still works through its interactive tap, but
  should be given an initial capital if it must also be accepted as typed text.
- The menu does not enable itself for a channel that has no active tenant configuration.
- `conversations.menu_state*` (migration 0035) is intentionally unused: the stateless ids
  make it unnecessary for phase 1.
- Pagination shows at most ten rows per page; there is no free-text page number.

## Tests

`tests/test_whatsapp_menu.py` covers command handling, configured labels, disabled and
unknown options, malformed configuration, payload limits, tap parsing, stale/expired ids,
empty and populated catalogues, uncategorised products, pagination, missing prices, order
privacy, handover success/failure, definite vs ambiguous send failures, outbound
persistence, and the disabled-by-default behaviour. All database access is faked; no test
sends a real WhatsApp message.

`tests/test_tenant_config_partial_updates.py` covers the Menu Builder: rendering defaults,
saving a custom welcome/language/order/labels, disabling a row, rejecting duplicate ids,
blank labels, unknown actions and invalid positions, tenant isolation, and simulator
refresh after a save. `tests/test_menu_simulator.py` covers the simulator's safe reply view,
configured menu handling, the loopback default, the explicit remote-database opt-in,
invalid flag values, synthetic identity, and forced send suppression.

`tests/test_menu_db.py` adds database-backed order-privacy and staff-assistance tests. The
database suites require an explicit `SAAS_TEST_DATABASE_URL` naming a disposable local
database (for example `postgresql://postgres:...@127.0.0.1:5432/saas_phase1_test`); they
never fall back to `SAAS_DATABASE_URL`. Set `SAAS_TEST_APP_DATABASE_URL` (or use the
`db/local` loopback grants) for tests that exercise `app_backend`.
`tests/test_test_database_guard.py` proves that unsafe or missing configuration is rejected
before `psycopg.connect` or `apply_migrations` can run.
