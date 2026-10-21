# Proactive human handover for unlisted products

## The problem this replaces

When a customer asked for something the catalogue could not confirm — a pina colada cake —
the bot answered:

> "Pina colada cake isn't listed in our catalogue. Please contact our staff on WhatsApp at
> +254 XXX XXX XXX to check if it can be arranged."

(The staff number above was the tenant's configured escalation contact; it is masked here.)

Nothing was recorded, no member of staff was told, and the customer was handed the burden of
chasing the business. This document describes what happens instead.

## The workflow

```
inbound WhatsApp message
        |
        v
  BotResponseEngine.respond()
        |
        +-- model calls request_staff_handover  --> saas.handover.create_request()
        |                                             (durable, tenant-scoped, idempotent)
        |
        +-- OR the reply quotes the tenant's own staff number  <-- deterministic safety net
        |        (platform_admin.services.handover.reply_hands_off_to_staff)
        |        --> create_request(reason="customer_request")
        |
        v
  platform_admin.services.handover.handle_turn()
        |
        +-- deliver(): WhatsApp text to the tenant's staff contact on the tenant's channel
        |      success -> status notified
        |      failure -> status failed + reason stored (visible, retryable)
        |
        v
  the platform - not the model - chooses the customer's reply
```

Three properties matter:

1. **The record is durable before anything claims success.** `handle_turn` writes the request
   first; the customer's wording is produced afterwards from the real outcome.
2. **The customer's wording is owned by the server.** The model cannot say "I've shared your
   request" for a turn the platform answered, so a failed record or notification cannot turn
   into a false claim.
3. **Staff notification failure never fails the customer.** The customer still receives a
   reply; the failure is recorded on the request and shown in the dashboard.

### Two detection paths, deliberately

* **The tool** (`request_staff_handover`) is how the model records a request with the product,
  size, flavour, quantity, date and notes. The system prompt teaches the protocol, and the
  tool description states that a price must never be quoted for it. It refuses in test mode and
  when there is no customer identity.
* **The safety net** is the guarantee. Whatever the model writes, a reply that hands the
  customer one of the tenant's *configured* staff numbers is the behaviour being replaced, so
  the turn is converted into a recorded request and the reply is replaced. The signal is tenant
  configuration (phone digits, last nine digits compared), not an English phrase, so it works
  for any language and cannot fire on ordinary replies.

## Data model

`public.handover_requests` (migration `0034_handover_requests.sql`), tenant-owned, RLS enabled
and forced, granted to `app_backend` only, exactly like `customers`/`conversations`/`messages`:

| Column | Meaning |
| --- | --- |
| `tenant_id`, `customer_id`, `conversation_id`, `wa_id`, `customer_name` | who asked, and in which thread (customer/conversation may be null if persistence failed) |
| `requested_product`, `product_key` | what they asked for, plus a normalised key |
| `details` (jsonb) | size / flavour / quantity / needed_by / notes |
| `context` (jsonb) | the last four short turns, never the whole history |
| `reason` | `unlisted_product`, `unavailable_variant`, `custom_order`, `customer_request` |
| `status` | `pending`, `notified`, `failed`, `in_progress`, `resolved` |
| `notification_target`, `notification_error`, `attempts`, `notified_at` | what happened to the staff notification |
| `dedupe_key` | the inbound provider message id |
| `resolved_at`, `created_at`, `updated_at` | timestamps (`updated_at` is trigger-maintained) |

Two unique indexes make idempotency a database property rather than a caller convention:

```sql
unique (tenant_id, dedupe_key)                                     -- Meta redelivers until it gets a 200
unique (tenant_id, wa_id, product_key) where status <> 'resolved'  -- one open request per product
```

## Staff contact is tenant configuration

Nothing about a particular business is hard-coded. The notification target is resolved per
tenant from the saved settings, most specific first:

1. `configuration.config.human_handover_number` (the "Human handover number" field)
2. `business_rules.escalation.whatsapp` (the "Escalation WhatsApp number" field)

If neither is set, the request is still recorded, `status` becomes `failed` with
"No staff contact is configured for this tenant.", the customer is told the request was
logged (not that the team was told), and the dashboard shows it. Delivery uses the tenant's own
WhatsApp channel — the same `whatsapp_service.send_text` path the bot already replies with, and
the same `credentials_ref` secret lookup. No credential is copied into the handover row.

## Customer wording

| Outcome | What the customer receives |
| --- | --- |
| recorded **and** staff notified | "Thanks for your interest in our <product>! I've shared your request with the <business> team. Our staff will get back to you soon to confirm whether we can make it and provide the actual price." |
| recorded, notification failed | "…I've **logged** your request for the <business> team. Our staff will get back to you soon…" — "shared" is never claimed |
| not recorded | "Sorry — I couldn't record your request just now, so the team could not be notified automatically. Please try again in a moment." |

No price, availability or lead time is ever stated in these replies, and the staff number is
never given to the customer.


## Dashboard

**Tenants → <client> → Handovers** (`/tenants/<id>/handovers`) lists the tenant's requests
(newest first, filterable by status) with the product, details, customer, reason, conversation
context, notification target and any delivery error. Staff can move a request to `pending`,
`in_progress` or `resolved`, and **Notify staff again** retries a failed delivery through the
tenant's active channel. Everything is scoped by `_tenant()` (platform authorization) and by
RLS, so tenant A cannot read or change tenant B's handovers — asserted by tests.

## Limitations (deliberate, documented rather than hidden)

* **Notification is synchronous.** There is no queue. A slow Meta call delays one webhook
  response, and the customer's reply waits for it. That keeps the workflow to one transaction
  boundary and one code path; a queue is the next step if delivery latency becomes a problem.
* **Meta's 24-hour window applies.** The notification is an ordinary text message to the staff
  number on the tenant's own number. If the staff member has not messaged that number within
  24 hours, Meta rejects it (re-engagement error) and the request is marked `failed` with that
  reason. A template-based notification is the correct long-term answer and is **not**
  implemented.
* **The safety net cannot name the product.** When the model defers instead of calling the
  tool, the recorded `requested_product` is the customer's message (reason
  `customer_request`), and one open request per customer is reused so repeated messages do not
  spam staff. The precise product, size and date come from the tool path.
* **Nothing in the catalogue is modified**, and no order or payment is created for an unlisted
  product; the existing order and payment safeguards are untouched.

## Deployment requirements

**Apply the migration before the code.** Nothing in this change alters an existing table, but
the new tool and the safety net both write to `handover_requests`:

```bash
python db/run_migrations.py --database-url "$SAAS_DATABASE_URL"     # applies 0034
```

If the migration is not applied, the insert fails, the tool reports failure, and an unlisted
product request ends in the apology path — worse than today's behaviour, which is why the order
matters. `WHATSAPP_SEND_ENABLED` must already be `1` (it is, because the bot replies) for staff
notifications to be attempted.

## Tests

```bash
python -m unittest discover -s tests -p "test_handover_flow.py" -v
```

34 tests against a real database, using tenant 002 ("Test Bakery") so no Cindy Bakes data is
touched: the unlisted-product handover and its customer confirmation, the exact staff
notification contents, notification failure never becoming a success claim, record failure
producing the apology, redelivery creating no duplicate, repeat questions not re-notifying,
a catalogue product keeping the normal workflow, an invented price never reaching the customer,
tenant isolation at both the storage and dashboard layers, missing staff configuration, and the
pure target/detection/wording functions.

