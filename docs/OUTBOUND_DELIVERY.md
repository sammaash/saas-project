# Outbound WhatsApp delivery — correlation, receipts and stuck rows

Operational runbook for the customer-facing replies the webhook sends. It covers what the
platform records, how to read it when a send is uncertain, and what must **not** be done.
Menus are not involved; this is the free-text reply path plus its delivery receipts.

> The staff handover notification uses the same sender but is **not** stored in
> `public.messages`; it is tracked by `public.handover_requests` instead. See
> `docs/HUMAN_HANDOVER.md`.

## 1. What the code guarantees

| Guarantee | Enforced by |
|---|---|
| At most one outbound row per inbound message | Partial unique index `messages_outbound_reply_unique` on `(tenant_id, channel_id, reply_to_provider_message_id)` |
| A reply is recorded **before** it is sent | `webhook` route inserts the outbox row first (`platform_admin/routes/__init__.py`) |
| The provider id is attached only after a successful send | `customers.mark_message_sent` |
| A failed send keeps Meta's diagnostic fields | `customers.mark_message_failed` (`error_code`, `error_message`) |
| Receipts update, never create | `customers.update_delivery_status` |
| No automatic resend | There is no retry loop; ambiguity is recorded, not replayed |

`public.messages.reply_to_provider_message_id` holds the **inbound** message id that an
outbound row answers. It is null for inbound rows, for historical rows, and for replies
recorded through the pre-0035 fallback.

## 2. Lifecycle and failure behaviour

| Stage | Written where | If it fails |
|---|---|---|
| 1. Claim the inbound message | `whatsapp_webhook_events` (`processing`) | Duplicate delivery is acknowledged, nothing is sent |
| 2. Persist the inbound message | `messages` (inbound) | Logged; processing continues |
| 3. Record the intent to reply | `messages` (outbound, `provider_message_id` null, `delivery_status` null) | `DuplicateReplyError` → look for the existing row; other errors are logged and the reply is still attempted |
| 4. Send | Meta | `ServiceError` → `mark_message_failed`, event marked `failed` |
| 5. Record the wamid | `messages.provider_message_id`, `delivery_status='sent'` | Logged with the wamid; the row stays without a provider id |
| 6. Receipts | `messages.delivery_status` `sent → delivered → read`, or `failed` | Logged; the webhook still returns 200 |

Forward-only receipt rules: `read` is terminal; `delivered` can still become `read`;
`failed` can be superseded by `delivered`/`read`; a late `sent` never overwrites a later
state. Duplicate receipts are no-ops.

## 3. Current limitation — no correlation data on the request

Meta's status webhook reference documents `biz_opaque_callback_data` as present **only when
the message was sent with it**. This code accepts and would send it (`send_text(...,
reply_to_row_id=...)`), but **no caller supplies it**, for two reasons:

1. The Messages API reference could not be read far enough to confirm the request-side
   schema, type and length limits for the configured Graph version (`v21.0`).
2. Confirming it needs one real send, which has deliberately not been made.

Consequence: every receipt is matched by **wamid only**. An outbound row whose wamid was
never recorded cannot be resolved by a receipt and needs the manual procedure in §5.

## 4. Diagnosing a stuck row (read-only)

A row with `provider_message_id IS NULL` is *not* necessarily undelivered — it may have
been accepted by Meta and lost in our response handling. Treat it as **unknown**, not as
failed.

```sql
-- Outbound rows with no recorded provider id, older than 10 minutes.
-- Run with the operator (owner) DSN used for migrations. Neither app_backend
-- (RLS-scoped to one tenant) nor saas_platform_admin (no grant on messages) can
-- answer this across tenants.
select m.id              as outbox_id,
       m.tenant_id,
       m.conversation_id,
       m.created_at,
       m.delivery_status,
       m.error_code,
       m.reply_to_provider_message_id as inbound_wamid,
       e.status   as event_status,
       e.error_code as event_error
from public.messages m
left join public.whatsapp_webhook_events e
       on e.message_id = m.reply_to_provider_message_id
where m.direction = 'outbound'
  and m.reply_to_provider_message_id is not null
  and m.provider_message_id is null
  and m.created_at < now() - interval '10 minutes'
order by m.created_at;
```

Useful companion: the inbound transcript for that conversation, so the operator can see
what the customer asked and whether a later reply already covered it.

```sql
select direction, sender_type, message_type, delivery_status,
       provider_message_id, reply_to_provider_message_id, body_text, received_at
from public.messages
where tenant_id = :tenant_id and conversation_id = :conversation_id
order by received_at, id;
```

Do **not** run these as `app_backend` from an application shell, and note that
`saas_platform_admin` has no grant on `public.messages`; both statements need the operator
(owner) role.

## 5. Manual reconciliation (no automatic resend)

For each row:

1. **Look in WhatsApp.** Open the recipient's chat (or WhatsApp Manager) and find the
   message with the same text and timestamp (±2 minutes).
2. **If it was delivered:** set the wamid from the message details and mark it sent.
   ```sql
   update public.messages
   set provider_message_id = :wamid, delivery_status = 'sent',
       error_code = null, error_message = null
   where id = :outbox_id and provider_message_id is null;
   ```
3. **If it was not delivered and the customer has since messaged:** leave it for the
   transcript and record the truth.
   ```sql
   update public.messages
   set delivery_status = 'failed', error_code = 'unknown_delivery',
       error_message = 'No provider id recorded; message not found in WhatsApp'
   where id = :outbox_id and provider_message_id is null;
   ```
4. **If it was not delivered and the customer is still waiting:** do not edit the row to
   look successful. Send a fresh reply through the normal conversation (a new inbound
   message, or the dashboard once staff messaging exists). The new reply gets its own
   outbox row and wamid.
5. Record what was found in the incident notes. If several rows share a cause, fix that
   cause before reconciling the rest.

Never delete the row: it is the only evidence that a reply was attempted.

## 6. What not to do

- **Do not replay the inbound webhook.** The claim table will treat it as a duplicate, and
  a manual replay would bypass the one-reply guarantee.
- **Do not edit `provider_message_id` to a guessed value.** A wrong wamid silently
  mis-attributes later receipts.
- **Do not mark a row `sent` because the customer replied.** A later customer message
  proves the customer is present, not that this reply arrived.
- **Do not add a retry loop.** Non-idempotent turns (order drafts, confirmations) cannot be
  replayed safely; this is why reclamation is deferred.

## 7. Proposed change — not enabled

The minimal payload change, once a real send is authorised:

```python
# platform_admin/routes/__init__.py, in the webhook send
outbound_id = whatsapp_service.send_text(
    ...,
    reply_to_row_id=str(outbox_row["id"]),
)
```

That adds exactly one top-level field, `biz_opaque_callback_data: <outbox row uuid>`,
to the request. Preconditions before enabling:

1. One real send to a controlled number, confirming Meta accepts the field for a
   free-form text message on the configured Graph version.
2. The status webhook then carries the same value; `_apply_delivery_statuses` already
   matches on it.
3. Only then should the two comments in `routes/__init__.py` and the `send_text` docstring
   claim receipt-based reconciliation.

Rollback is removing the argument; the field is advisory and no stored data depends on it.

## 8. Code map

| Concern | Location |
|---|---|
| Inbound parsing (typed) and status parsing | `platform_admin/services/whatsapp.py` (`parse_event`, `parse_status_events`) |
| Sending, Meta error extraction | `platform_admin/services/whatsapp.py` (`send_text`, `_http_error_details`) |
| Outbox helpers | `platform_admin/services/customers.py` (`save_message`, `find_outbound_reply`, `mark_message_sent`, `mark_message_failed`, `update_delivery_status`) |
| Route orchestration | `platform_admin/routes/__init__.py` (`whatsapp_webhook`, `_apply_delivery_statuses`) |
| Schema | `db/migrations/0035_menu_state_and_reply_correlation.sql` |

