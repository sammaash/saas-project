-- ============================================================================
-- Phase 7 / 0034 -- human handover requests
-- ============================================================================
-- When a customer asks for something the catalogue cannot answer (an unlisted
-- product, or a variant the business has not priced), the bot must not invent a
-- price or send the customer off to chase staff. It records a durable request and
-- the platform notifies the tenant's own staff contact.
--
-- Additive: one new tenant-owned table. No existing table is altered and no row
-- is modified, so this is safe to apply to a live database.
--
-- PRIVILEGE NOTE: every migration after 0030 may grant ONLY to `app_backend`.
-- All grants below go to app_backend alone; the dashboard and bot both connect as
-- app_backend (saas/db.py::connect), so nothing else is needed. Customer identity
-- and conversation context live here, so no Supabase-exposed role may read it.
-- ============================================================================

create table if not exists public.handover_requests (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null references public.tenants (id) on delete cascade,
  -- Customer/conversation references are optional: a request can still be recorded
  -- when the surrounding persistence failed, which is exactly when staff need it.
  customer_id         uuid,
  conversation_id     uuid,
  wa_id               text not null,
  customer_name       text,
  requested_product   text not null,
  -- Normalised requested_product, so the "same unresolved request" rule is enforced
  -- by the database rather than by a caller remembering to check.
  product_key         text not null,
  details             jsonb not null default '{}'::jsonb,
  context             jsonb not null default '[]'::jsonb,
  reason              text not null default 'unlisted_product',
  status              text not null default 'pending',
  notification_target text,
  notification_error  text,
  attempts            integer not null default 0,
  notified_at         timestamptz,
  resolved_at         timestamptz,
  -- The inbound provider message id (or a server-derived key). Meta redelivers the
  -- same event until it receives a 200, so this is the idempotency backstop.
  dedupe_key          text not null,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  constraint handover_requests_status_check
    check (status in ('pending', 'notified', 'in_progress', 'resolved', 'failed')),
  constraint handover_requests_reason_check
    check (reason in ('unlisted_product', 'unavailable_variant', 'custom_order',
                      'customer_request')),
  constraint handover_requests_wa_id_check
    check (length(btrim(wa_id)) between 1 and 200),
  constraint handover_requests_product_check
    check (length(btrim(requested_product)) between 1 and 200),
  constraint handover_requests_product_key_check
    check (length(btrim(product_key)) between 1 and 200),
  constraint handover_requests_dedupe_key_check
    check (length(btrim(dedupe_key)) between 1 and 200),
  constraint handover_requests_customer_name_check
    check (customer_name is null or length(btrim(customer_name)) between 1 and 200),
  constraint handover_requests_attempts_check check (attempts >= 0),
  constraint handover_requests_id_tenant_key unique (id, tenant_id),
  constraint handover_requests_customer_fkey
    foreign key (customer_id, tenant_id)
    references public.customers (id, tenant_id) on delete cascade,
  constraint handover_requests_conversation_fkey
    foreign key (conversation_id, tenant_id)
    references public.conversations (id, tenant_id) on delete cascade
);

comment on table public.handover_requests is
  'Tenant-scoped human-handover requests: a customer asked for something the catalogue could not confirm. Staff contact details are configuration, never stored here.';
comment on column public.handover_requests.requested_product is
  'What the customer asked for, as written (unlisted product, custom order, or their own words).';
comment on column public.handover_requests.context is
  'Recent conversation turns needed to answer the request. Trimmed, never the full history.';
comment on column public.handover_requests.status is
  'pending (recorded, not yet delivered), notified (staff told), failed (delivery failed, retryable), in_progress/resolved (staff-driven).';
comment on column public.handover_requests.notification_target is
  'The tenant''s own staff contact number the notification was addressed to. Not a credential.';

create index if not exists handover_requests_tenant_status_idx
  on public.handover_requests (tenant_id, status, created_at desc);
create index if not exists handover_requests_tenant_customer_idx
  on public.handover_requests (tenant_id, wa_id, created_at desc);

-- Idempotency for a redelivered provider event.
create unique index if not exists handover_requests_tenant_dedupe_unique
  on public.handover_requests (tenant_id, dedupe_key);

-- One open request per customer per product: asking again must not spam staff.
create unique index if not exists handover_requests_open_product_unique
  on public.handover_requests (tenant_id, wa_id, product_key)
  where status <> 'resolved';

drop trigger if exists handover_requests_set_updated_at on public.handover_requests;
create trigger handover_requests_set_updated_at
  before update on public.handover_requests
  for each row execute function app.set_updated_at();


-- ---------------------------------------------------------------------------
-- Row-level security: enabled AND forced, with the same single predicate as every
-- other tenant-owned table.
-- ---------------------------------------------------------------------------
alter table public.handover_requests enable row level security;
alter table public.handover_requests force row level security;

drop policy if exists handover_requests_backend_select on public.handover_requests;
create policy handover_requests_backend_select on public.handover_requests
  for select to app_backend
  using (tenant_id = app.current_tenant_id());

drop policy if exists handover_requests_backend_all on public.handover_requests;
create policy handover_requests_backend_all on public.handover_requests
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());


-- ---------------------------------------------------------------------------
-- Grants: app_backend ONLY (see the privilege note at the top of this file).
-- ---------------------------------------------------------------------------
grant select on public.handover_requests to app_backend;
grant select, insert, update, delete on public.handover_requests to app_backend;

revoke all on public.handover_requests from public, anon, authenticated;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on public.handover_requests from service_role';
  end if;
end
$$;

