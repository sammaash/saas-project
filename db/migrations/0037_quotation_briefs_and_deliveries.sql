-- ============================================================================
-- Phase 8 / 0037 -- WhatsApp quotation briefs and PDF delivery ledger
-- ============================================================================
-- Companion to 0036_whatsapp_quotations.sql. 0036 is immutable committed history
-- (checksum 9dd2c780deaecfad): it adds the quotation origin columns and the
-- tenant+source idempotency constraint, but creates no table. The two
-- customer-facing tables therefore arrive here, in a new file, so that every
-- environment converges without editing the ledger:
--
--   quotation_briefs      the customer's quotation request while it is still being
--                         collected. One open brief per conversation, so a follow-up
--                         message ("two of them", "make it 2kg") updates the same
--                         request instead of starting again.
--   quotation_deliveries  one row per attempt to send an issued quotation's PDF as a
--                         WhatsApp document. The row is written *before* the send and
--                         the provider ids are attached afterwards, so an ambiguous
--                         timeout is recorded as uncertain and never retried blindly.
--
-- PRIVILEGE NOTE: every migration after 0030 may grant ONLY to `app_backend`.
-- All grants below go to app_backend alone. Customer identity, conversation
-- context and delivery state must never be readable through a Supabase-exposed
-- role (anon/authenticated), so those roles are revoked explicitly.
--
-- FAIL-CLOSED GUARD: this migration deliberately refuses to accept a pre-existing
-- table instead of using `create table if not exists`. A database that already has
-- either table reached that state through a different 0036 (the variant recorded as
-- aa89f2d6be3a64a6) or by hand, and in both cases the ledger would stop describing
-- reality. Add a new migration instead of editing the ledger -- see
-- docs/MIGRATION_0036_RECONCILIATION.md.
--
-- Deploy order: migration first, then code. platform_admin.config.REQUIRED_SCHEMA
-- names both tables, so a database without this migration fails the startup check
-- with the missing table name instead of failing the first customer quotation at
-- runtime.
-- ============================================================================

do $$
begin
  if to_regclass('public.quotation_briefs') is not null
     or to_regclass('public.quotation_deliveries') is not null then
    raise exception
      'migration 0037 refuses to run: public.quotation_briefs or '
      'public.quotation_deliveries already exists. This database was migrated with a '
      'different 0036 (variant aa89f2d6be3a64a6) or the tables were created by hand. '
      'Do not rewrite the ledger: see docs/MIGRATION_0036_RECONCILIATION.md';
  end if;
end
$$;

-- ---------------------------------------------------------------------------
-- Quotation briefs: the request being collected, one per conversation.
-- ---------------------------------------------------------------------------
create table public.quotation_briefs (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references public.tenants (id) on delete cascade,
  conversation_id  uuid not null,
  customer_wa_id   text,
  -- collecting: still asking for details. ready: every material detail is known.
  -- quoted: a quotation was issued from this brief. handed_over: staff must price it.
  status           text not null default 'collecting',
  -- The collected request (items, quantities, notes). Read and written as a whole,
  -- never filtered or joined, so it is JSONB by the same rule as tenant_settings.
  brief            jsonb not null default '{}'::jsonb,
  -- Server-computed, so the model is told exactly what is still missing instead of
  -- guessing. Replaced on every write; never trusted from a caller.
  missing_fields   text[] not null default '{}'::text[],
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  constraint quotation_briefs_status_check
    check (status in ('collecting', 'ready', 'quoted', 'handed_over')),
  constraint quotation_briefs_brief_object
    check (jsonb_typeof(brief) = 'object'),
  constraint quotation_briefs_customer_wa_id_check
    check (customer_wa_id is null
           or length(btrim(customer_wa_id)) between 1 and 200),
  constraint quotation_briefs_tenant_conversation_consistency
    foreign key (conversation_id, tenant_id)
    references public.conversations (id, tenant_id) on delete cascade
);

comment on table public.quotation_briefs is
  'One in-progress customer quotation request per conversation. Cleared when the '
  'conversation starts a new request; never shared between tenants or conversations.';
comment on column public.quotation_briefs.missing_fields is
  'Server-computed material details still needed before the request can be priced.';

create index quotation_briefs_tenant_status_idx
  on public.quotation_briefs (tenant_id, status, updated_at desc);

create unique index quotation_briefs_conversation_unique
  on public.quotation_briefs (tenant_id, conversation_id);

drop trigger if exists quotation_briefs_set_updated_at on public.quotation_briefs;
create trigger quotation_briefs_set_updated_at
  before update on public.quotation_briefs
  for each row execute function app.set_updated_at();

-- ---------------------------------------------------------------------------
-- Quotation deliveries: one row per attempt to send a quotation PDF.
-- ---------------------------------------------------------------------------
create table public.quotation_deliveries (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null references public.tenants (id) on delete cascade,
  quotation_id        uuid not null,
  channel_id          uuid,
  conversation_id     uuid,
  customer_wa_id      text not null,
  -- Stable key for this delivery ("<quotation id>:<inbound wamid>"). Together with
  -- the unique constraint below it makes a replayed webhook a no-op.
  idempotency_key     text not null,
  -- Meta media object id returned by the upload, and the wamid of the document
  -- message. Both are attached only after the provider confirmed them.
  media_id            text,
  provider_message_id text,
  -- pending: recorded, send not confirmed. sent: provider accepted it.
  -- failed: definite rejection (safe to try again later). uncertain: the provider
  -- may have accepted it; never retried automatically.
  delivery_status     text not null default 'pending',
  error_code          text,
  error_message       text,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  constraint quotation_deliveries_status_check
    check (delivery_status in ('pending', 'sent', 'failed', 'uncertain')),
  constraint quotation_deliveries_customer_wa_id_check
    check (length(btrim(customer_wa_id)) between 1 and 200),
  constraint quotation_deliveries_idempotency_key_check
    check (length(btrim(idempotency_key)) between 1 and 200),
  constraint quotation_deliveries_provider_message_check
    check (provider_message_id is null
           or length(btrim(provider_message_id)) between 1 and 200),
  constraint quotation_deliveries_error_message_check
    check (error_message is null or length(error_message) <= 500),
  constraint quotation_deliveries_tenant_quotation_consistency
    foreign key (quotation_id, tenant_id)
    references public.quotations (id, tenant_id) on delete cascade,
  constraint quotation_deliveries_idempotency_unique
    unique (tenant_id, quotation_id, idempotency_key)
);

comment on table public.quotation_deliveries is
  'Outbound delivery ledger for quotation PDF documents. Written before the send so '
  'an ambiguous provider outcome can never be mistaken for a message that was never sent.';

create index quotation_deliveries_tenant_quotation_idx
  on public.quotation_deliveries (tenant_id, quotation_id, created_at desc);

create index quotation_deliveries_provider_message_idx
  on public.quotation_deliveries (tenant_id, provider_message_id)
  where provider_message_id is not null;

drop trigger if exists quotation_deliveries_set_updated_at on public.quotation_deliveries;
create trigger quotation_deliveries_set_updated_at
  before update on public.quotation_deliveries
  for each row execute function app.set_updated_at();

-- ---------------------------------------------------------------------------
-- Row-level security: enabled AND forced, with the same single predicate as every
-- other tenant-owned table.
-- ---------------------------------------------------------------------------
alter table public.quotation_briefs enable row level security;
alter table public.quotation_briefs force row level security;

drop policy if exists quotation_briefs_backend_select on public.quotation_briefs;
create policy quotation_briefs_backend_select on public.quotation_briefs
  for select to app_backend
  using (tenant_id = app.current_tenant_id());

drop policy if exists quotation_briefs_backend_all on public.quotation_briefs;
create policy quotation_briefs_backend_all on public.quotation_briefs
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

alter table public.quotation_deliveries enable row level security;
alter table public.quotation_deliveries force row level security;

drop policy if exists quotation_deliveries_backend_select on public.quotation_deliveries;
create policy quotation_deliveries_backend_select on public.quotation_deliveries
  for select to app_backend
  using (tenant_id = app.current_tenant_id());

drop policy if exists quotation_deliveries_backend_all on public.quotation_deliveries;
create policy quotation_deliveries_backend_all on public.quotation_deliveries
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

-- ---------------------------------------------------------------------------
-- Grants: app_backend ONLY (see the privilege note at the top of this file).
-- ---------------------------------------------------------------------------
grant select, insert, update, delete on public.quotation_briefs to app_backend;
grant select, insert, update, delete on public.quotation_deliveries to app_backend;

revoke all on public.quotation_briefs, public.quotation_deliveries
  from public, anon, authenticated;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on public.quotation_briefs, public.quotation_deliveries '
            'from service_role';
  end if;
end
$$;
