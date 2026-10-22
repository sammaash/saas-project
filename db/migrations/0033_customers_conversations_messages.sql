-- ============================================================================
-- Phase 6 / 0033 -- tenant-scoped customers, conversations and messages
-- ============================================================================
-- Additive: creates three new tenant-owned tables and links orders to customers
-- via a nullable foreign key. No existing table is altered destructively and no
-- existing row is modified, so the migration is safe to apply to a live database.
-- whatsapp_conversations and whatsapp_webhook_events are intentionally left in
-- place; see DATABASE_PLAN.md for which model is authoritative during rollout.
--
-- PRIVILEGE NOTE: every migration after 0030 may grant ONLY to `app_backend`.
-- All grants below go to app_backend alone. The dashboard and bot connect as
-- app_backend (saas/db.py::connect), so nothing else is needed.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Customers: one row per WhatsApp identity within a tenant.
-- wa_id is the stable WhatsApp identity (the `from` field on inbound webhooks).
-- display name and phone are optional and may change; they are never unique keys.
-- ---------------------------------------------------------------------------
create table if not exists public.customers (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null references public.tenants (id) on delete cascade,
  wa_id               text not null,
  name                text,
  phone_normalized    text,
  email               text,
  metadata            jsonb not null default '{}'::jsonb,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  last_interaction_at timestamptz,
  constraint customers_wa_id_check
    check (length(btrim(wa_id)) between 1 and 200),
  constraint customers_phone_normalized_check
    check (phone_normalized is null or length(btrim(phone_normalized)) between 1 and 50),
  constraint customers_email_check
    check (email is null or length(btrim(email)) between 1 and 254),
  -- Composite key so child rows (orders, conversations) can prove tenant consistency via FK.
  constraint customers_id_tenant_key unique (id, tenant_id)
);

comment on table public.customers is
  'Tenant-scoped customer identity. wa_id is the stable WhatsApp identity; name/phone/email are optional and may change.';
comment on column public.customers.wa_id is
  'Stable WhatsApp identity (message "from" field). Treated as opaque; never logged in full.';
comment on column public.customers.metadata is
  'Small tenant-owned operational metadata only. No secrets, no full message content.';

create index if not exists customers_tenant_idx
  on public.customers (tenant_id);
create unique index if not exists customers_tenant_wa_id_unique
  on public.customers (tenant_id, wa_id);

drop trigger if exists customers_set_updated_at on public.customers;
create trigger customers_set_updated_at
  before update on public.customers
  for each row execute function app.set_updated_at();


-- ---------------------------------------------------------------------------
-- Conversations: one customer may have many conversations over time.
-- status in (open, closed, archived). An inbound message reuses the most recent
-- open conversation for the customer/channel, otherwise a new one is created.
-- ---------------------------------------------------------------------------
create table if not exists public.conversations (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null references public.tenants (id) on delete cascade,
  customer_id         uuid not null references public.customers (id) on delete cascade,
  channel_id          uuid not null references public.tenant_channels (id) on delete cascade,
  provider_conversation_id text,
  status              text not null default 'open',
  assigned_to         uuid,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  last_message_at     timestamptz,
  closed_at           timestamptz,
  constraint conversations_status_check
    check (status in ('open', 'closed', 'archived')),
  constraint conversations_provider_conversation_check
    check (provider_conversation_id is null or length(btrim(provider_conversation_id)) between 1 and 200),
  -- Composite key so child rows (messages) can prove tenant consistency via FK.
  constraint conversations_id_tenant_key unique (id, tenant_id)
);

comment on table public.conversations is
  'Tenant-scoped conversation lifecycle. open/closed/archived; one customer may have many conversations.';
comment on column public.conversations.provider_conversation_id is
  'Provider-side conversation identifier where one exists (WhatsApp has no stable ID today).';

create index if not exists conversations_tenant_idx
  on public.conversations (tenant_id);
create index if not exists conversations_customer_idx
  on public.conversations (tenant_id, customer_id, updated_at desc);
create index if not exists conversations_channel_idx
  on public.conversations (tenant_id, channel_id);

drop trigger if exists conversations_set_updated_at on public.conversations;
create trigger conversations_set_updated_at
  before update on public.conversations
  for each row execute function app.set_updated_at();


-- ---------------------------------------------------------------------------
-- Messages: the durable, chronological record of inbound and outbound messages.
-- provider_message_id is deduplicated within (tenant_id, channel_id).
-- body_text may be null (media / interactive messages); payload carries the
-- structured provider payload with secrets redacted.
-- ---------------------------------------------------------------------------
create table if not exists public.messages (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          uuid not null references public.tenants (id) on delete cascade,
  channel_id         uuid not null references public.tenant_channels (id) on delete cascade,
  conversation_id    uuid not null references public.conversations (id) on delete cascade,
  direction          text not null,
  sender_type        text not null,
  provider_message_id text,
  message_type       text not null default 'text',
  body_text          text,
  payload            jsonb,
  provider_timestamp timestamptz,
  received_at        timestamptz not null default now(),
  delivery_status    text,
  error_code         text,
  error_message      text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  constraint messages_direction_check
    check (direction in ('inbound', 'outbound')),
  constraint messages_sender_type_check
    check (sender_type in ('customer', 'assistant', 'staff', 'system')),
  constraint messages_message_type_check
    check (message_type in ('text', 'image', 'document', 'audio', 'video', 'interactive', 'location', 'contacts', 'other')),
  constraint messages_delivery_status_check
    check (delivery_status is null or delivery_status in ('sent', 'delivered', 'read', 'failed')),
  constraint messages_provider_message_check
    check (provider_message_id is null or length(btrim(provider_message_id)) between 1 and 200),
  -- Tenant consistency: a message's conversation must belong to the same tenant
  -- (enforced with a composite FK; CHECK constraints cannot contain subqueries).
  constraint messages_tenant_conversation_consistency
    foreign key (conversation_id, tenant_id)
    references public.conversations (id, tenant_id) on delete cascade
);

comment on table public.messages is
  'Durable tenant-scoped message history. provider_message_id is deduplicated per tenant/channel.';
comment on column public.messages.body_text is
  'Message text only when the message has a textual body; null for media/interactive messages.';
comment on column public.messages.payload is
  'Structured provider payload, redacted of secrets and tokens.';
comment on column public.messages.error_message is
  'Provider error text for diagnosis only; never contains secrets or tokens.';

create index if not exists messages_tenant_idx
  on public.messages (tenant_id);
create index if not exists messages_conversation_idx
  on public.messages (tenant_id, conversation_id, received_at asc, id asc);
create index if not exists messages_channel_idx
  on public.messages (tenant_id, channel_id, received_at desc);

-- Provider message deduplication within the tenant/channel scope. Multiple NULL
-- provider_message_id values are allowed (PostgreSQL unique semantics); only a
-- repeated non-null provider id is rejected -- that is the idempotency backstop.
alter table public.messages
  add constraint messages_provider_message_unique
  unique (tenant_id, channel_id, provider_message_id);

drop trigger if exists messages_set_updated_at on public.messages;
create trigger messages_set_updated_at
  before update on public.messages
  for each row execute function app.set_updated_at();


-- ---------------------------------------------------------------------------
-- Orders: safely link to customers without touching historical snapshots.
-- Historical orders keep their customer_name / customer_phone snapshots and
-- remain readable even when no customer record exists. No backfill: customer
-- identity is not fabricated for orders placed before this migration.
-- ---------------------------------------------------------------------------
alter table public.orders
  add column if not exists customer_id uuid;

alter table public.orders
  add constraint orders_customer_tenant_fkey
  foreign key (customer_id, tenant_id)
  references public.customers (id, tenant_id);

create index if not exists orders_customer_idx
  on public.orders (tenant_id, customer_id, created_at desc)
  where customer_id is not null;


-- ---------------------------------------------------------------------------
-- Row-level security: enabled AND forced on every new table, exactly like the
-- existing tenant-owned tables. The sole predicate is app.current_tenant_id().
-- ---------------------------------------------------------------------------
alter table public.customers enable row level security;
alter table public.customers force row level security;
alter table public.conversations enable row level security;
alter table public.conversations force row level security;
alter table public.messages enable row level security;
alter table public.messages force row level security;

drop policy if exists customers_backend_select on public.customers;
create policy customers_backend_select on public.customers
  for select to app_backend
  using (tenant_id = app.current_tenant_id());
drop policy if exists customers_backend_all on public.customers;
create policy customers_backend_all on public.customers
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

drop policy if exists conversations_backend_select on public.conversations;
create policy conversations_backend_select on public.conversations
  for select to app_backend
  using (tenant_id = app.current_tenant_id());
drop policy if exists conversations_backend_all on public.conversations;
create policy conversations_backend_all on public.conversations
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

drop policy if exists messages_backend_select on public.messages;
create policy messages_backend_select on public.messages
  for select to app_backend
  using (tenant_id = app.current_tenant_id());
drop policy if exists messages_backend_all on public.messages;
create policy messages_backend_all on public.messages
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

-- ---------------------------------------------------------------------------
-- Grants: app_backend ONLY (see privilege note at top of file).
-- ---------------------------------------------------------------------------
grant select on public.customers to app_backend;
grant select, insert, update, delete on public.customers to app_backend;

grant select on public.conversations to app_backend;
grant select, insert, update, delete on public.conversations to app_backend;

grant select on public.messages to app_backend;
grant select, insert, update, delete on public.messages to app_backend;

-- belt and braces: no application role other than app_backend touches these tables
revoke all on public.customers, public.conversations, public.messages
  from public, anon, authenticated;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on public.customers, public.conversations, public.messages from service_role';
  end if;
end
$$;
