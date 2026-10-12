-- ============================================================================
-- Phase 4 / 0028 -- verified WhatsApp webhook processing state
-- ============================================================================

create table if not exists public.whatsapp_webhook_events (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null references public.tenants (id) on delete cascade,
  channel_id          uuid not null references public.tenant_channels (id) on delete cascade,
  message_id          text not null,
  customer_key        text not null,
  status              text not null default 'processing',
  response_message_id text,
  error_code          text,
  received_at         timestamptz not null default now(),
  processed_at        timestamptz,
  constraint whatsapp_webhook_events_message_unique unique (message_id),
  constraint whatsapp_webhook_events_status_check
    check (status in ('processing', 'processed', 'failed', 'ignored')),
  constraint whatsapp_webhook_events_message_check
    check (length(btrim(message_id)) between 1 and 200),
  constraint whatsapp_webhook_events_customer_check
    check (length(btrim(customer_key)) between 1 and 200)
);

create index if not exists whatsapp_webhook_events_tenant_received_idx
  on public.whatsapp_webhook_events (tenant_id, received_at desc);

create table if not exists public.whatsapp_conversations (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references public.tenants (id) on delete cascade,
  customer_key  text not null,
  history       jsonb not null default '[]'::jsonb,
  updated_at    timestamptz not null default now(),
  constraint whatsapp_conversations_tenant_customer_unique
    unique (tenant_id, customer_key),
  constraint whatsapp_conversations_history_array
    check (jsonb_typeof(history) = 'array'),
  constraint whatsapp_conversations_customer_check
    check (length(btrim(customer_key)) between 1 and 200)
);

create or replace function app.resolve_whatsapp_channel(p_phone_number_id text)
returns table (tenant_id uuid, channel_id uuid, credentials_ref text)
language sql
security definer
set search_path = pg_catalog, public, app
as $$
  select c.tenant_id, c.id, c.credentials_ref
  from public.tenant_channels c
  join public.tenants t on t.id = c.tenant_id
  where c.phone_number_id = p_phone_number_id
    and c.channel_type = 'whatsapp'
    and c.status = 'active'
    and t.status = 'active'
  limit 1
$$;

revoke all on function app.resolve_whatsapp_channel(text) from public;
grant execute on function app.resolve_whatsapp_channel(text) to app_backend;

alter table public.whatsapp_webhook_events enable row level security;
alter table public.whatsapp_webhook_events force row level security;
alter table public.whatsapp_conversations enable row level security;
alter table public.whatsapp_conversations force row level security;

drop policy if exists whatsapp_webhook_events_backend_all on public.whatsapp_webhook_events;
create policy whatsapp_webhook_events_backend_all on public.whatsapp_webhook_events
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

drop policy if exists whatsapp_conversations_backend_all on public.whatsapp_conversations;
create policy whatsapp_conversations_backend_all on public.whatsapp_conversations
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

revoke all on public.whatsapp_webhook_events, public.whatsapp_conversations
  from public, anon, authenticated;
grant select, insert, update on public.whatsapp_webhook_events,
  public.whatsapp_conversations to app_backend;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on public.whatsapp_webhook_events, public.whatsapp_conversations from service_role';
  end if;
end
$$;
