# Samma Inbox — Phase 1 notes

Mobile-first shared customer conversation inbox at `/t/<tenant_id>/inbox`
(and `/t/<tenant_id>/inbox/<conversation_id>`). Read-only against real tenant data;
no demo fixtures are used anywhere.

## What is implemented (live data)

- Conversation list with customer name/number, last-message preview, timestamp,
  conversation status and last-sender badge — backed by
  `platform_admin/services/customers.py:inbox_threads` (single tenant-scoped query,
  RLS `tenant_id = app.current_tenant_id()` on every table).
- Conversation panel: chronological messages with **customer / AI assistant / staff /
  system** distinction (`messages.sender_type`), timestamps (`received_at`) and
  delivery status (`delivery_status`) — all real columns from migration 0033.
- Search (name, number, preview text) and status filters (all/open/closed/archived).
- Customer profile drawer and lead-qualification presentation card.
- Authorization: `member_required` per request; cross-tenant conversation ids → 404.

## Implemented in Phase 2 (local; migration 0039 not yet applied)

- **Persistent AI control** — `conversations.ai_mode` (`ai_active`/`paused`/`human`) and
  `needs_attention`, transition matrix and audit table `conversation_control_events`
  (migration `0039_conversation_ai_control.sql`, local file only until Gate 3).
- **Authoritative dispatch boundary** — `platform_admin/services/outbound_dispatch.py`:
  every automated customer-facing send (AI text, menu replies, quotation PDFs) runs
  inside one transaction that holds the conversation row lock, rechecks `ai_mode`,
  calls the provider, and finalizes the outbound row before committing. Human
  takeover and dispatch are totally ordered per conversation; a takeover that
  commits first suppresses the send; a send already in flight completes before the
  takeover commits (never recalled). No automatic resend of ambiguous outcomes.
- **Staff replies** — `POST /t/<tenant_id>/inbox/<conversation_id>/reply` (CSRF,
  `member_required`): rejected with 503 before any write while
  `WHATSAPP_SEND_ENABLED=0`; otherwise `customers.add_staff_reply` (mode -> human +
  audit + outbound row in one transaction) followed by the dispatch boundary.
- **Real controls** — Pause AI / Take over / Resume AI / needs-attention toggle with
  audit events, 409 on lock contention, PRG + flash after commit.

## Gate 3 rollout plan (staging) — order, behaviour, recovery

**Order (strict):**
1. Apply `0039_conversation_ai_control.sql` to the isolated staging database via the
   guarded runner (`db/run_migrations.py`, the same path used for 0001–0038).
   Ledger becomes 39 rows with `0039` recorded and checksummed.
2. Verify read-only: `conversations.ai_mode`/`needs_attention` exist,
   `conversation_control_events` exists with RLS + `app_backend`-only grants,
   ledger = 39 with the expected checksum.
3. Deploy the approved commit (controlled deploy path, pinned SHA).
4. Smoke: app boots (startup schema guard passes), `/t/login` loads, one controlled
   fictitious login attempt behaves generically, `WHATSAPP_SEND_ENABLED=0` unchanged.

**Behaviour during rollout (why the order matters):**
- *Old code + new schema (between steps 1 and 3):* safe. `ai_mode='ai_active'` on
  every conversation means the old webhook behaves exactly as before; the new
  columns and audit table are simply unused.
- *New code + old schema (deploy before migrate):* **fail closed.** The startup
  schema guard (`REQUIRED_SCHEMA` / `verify_startup_database`) refuses to boot the
  app; if startup checks are bypassed, the dispatch boundary and control endpoints
  raise 503 — no customer-facing send can occur without the enforcement schema.
  There is **no legacy fallback** in this release.

**Recovery if migration succeeds but deployment fails:**
- Nothing to undo: the migration is purely additive with behaviour-preserving
  defaults (`ai_active` / `false`), so the currently deployed application keeps
  working unchanged (its documented pre-0039 behaviour).
- Investigate, fix forward, and redeploy the same approved commit. Do **not** roll
  back the migration — the immutable forward-migration convention applies; any
  schema correction ships as a new compensating migration (e.g. `0040_…`) under
  separate review.

**Rollback migration:** intentionally none (immutable-forward convention).

## Prototype-only (visible UI, deliberately not wired)

- **Conversation control** (Pause AI / Take over / Resume AI / Needs attention) —
  disabled buttons. Per-conversation AI state is **not persisted** today.
- **Composer / Send** — disabled. No message is sent or simulated.

## Not connected / unsupported by current data

- Unread indicators (no read/seen state on `messages` or `conversations`).
- AI-active vs paused state (no column).
- Lead scores, priorities, qualification states, signals (no scoring source).

## Required backend work (needs separate approval)

### 1. Migration 0039 (proposed — NOT written/applied)

```sql
alter table public.conversations
  add column ai_mode text not null default 'ai_active'
    check (ai_mode in ('ai_active', 'paused', 'human')),
  add column needs_attention boolean not null default false;

create table public.conversation_control_events (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.tenants (id) on delete cascade,
  conversation_id uuid not null,
  actor_user_id uuid not null,
  action text not null check (action in ('ai_paused','ai_resumed','human_takeover','needs_attention','attention_cleared')),
  created_at timestamptz not null default now(),
  foreign key (conversation_id, tenant_id) references public.conversations (id, tenant_id) on delete cascade
);
-- plus RLS (tenant_id = app.current_tenant_id()) and app_backend grants
```

### 2. Send-side AI enforcement (race-safe)

- In the inbound webhook flow (`platform_admin/routes/__init__.py`, the
  `BotResponseEngine.respond(...)` call around L817 and the `whatsapp.send_text`
  call around L938): re-read `conversations.ai_mode` **inside the send transaction**
  (`select ... for update` on the conversation row) and skip the automated send when
  the mode is `paused` or `human` — the check must be the last step before
  `whatsapp._post_message`, not at the start of the request.
- A human outbound send must set `ai_mode='human'` in the same transaction that
  records the message, so a queued AI reply cannot overwrite a human takeover.
- `whatsapp.send_text` stays hard-gated by `WHATSAPP_SEND_ENABLED` and gains an
  explicit `sender_type` ('staff' vs 'assistant') for the outbound row.

### 3. Human composer send path (future)

`POST /t/<tenant_id>/inbox/<conversation_id>/reply` (CSRF, `member_required`,
ownership re-check) → insert outbound `messages` row (direction='outbound',
sender_type='staff') → `whatsapp.send_text` (gated) → update `delivery_status`.
Never simulate success when the send gate is off.

### 4. Lead-qualification data contract (future engine)

```json
{
  "score": 0-100 | null,
  "priority": "high" | "medium" | "low" | null,
  "qualification": "unqualified" | "qualifying" | "qualified" | "disqualified" | null,
  "signals": [{"code": "…", "label": "…", "weight": 0-100}],
  "updated_at": "ISO-8601 | null",
  "source": "engine name/version"
}
```

Storage proposal: `customers.metadata.lead` (jsonb, schema-validated in the
service layer) until a scoring engine exists. The UI renders only stored values;
absent values display "Not scored"/"Not available".