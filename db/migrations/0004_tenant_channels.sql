-- ============================================================================
-- Phase 1 / 0004 -- tenant_channels (communication channels per tenant)
-- ============================================================================
-- Phase 1 models WhatsApp only. phone_number_id is the value WhatsApp Cloud API
-- puts in `metadata.phone_number_id` on every inbound webhook, so it is the
-- tenant routing key for the whole platform and is therefore globally unique.

create table if not exists public.tenant_channels (
  id                    uuid primary key default gen_random_uuid(),
  tenant_id             uuid not null references public.tenants (id) on delete cascade,
  channel_type          text not null default 'whatsapp',
  phone_number_id       text not null unique,
  waba_id               text,
  display_phone_number  text,
  status                text not null default 'pending',
  is_default            boolean not null default true,

  -- A *reference* to a secret held in a secret store (never the secret itself).
  -- Storing a WhatsApp access token in a shared multi-tenant table would put
  -- every tenant's credentials in every database dump.
  credentials_ref       text,

  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),

  constraint tenant_channels_type_check
    check (channel_type in ('whatsapp')),
  constraint tenant_channels_status_check
    check (status in ('pending', 'active', 'disabled')),
  constraint tenant_channels_phone_number_id_check
    check (length(btrim(phone_number_id)) between 3 and 64)
);

comment on table public.tenant_channels is
  'Per-tenant communication channels. phone_number_id is the inbound WhatsApp routing key.';
comment on column public.tenant_channels.phone_number_id is
  'Globally unique WhatsApp Cloud API phone number id. Inbound webhooks are resolved to a tenant through this value.';
comment on column public.tenant_channels.credentials_ref is
  'Identifier of the credential in the secret store. NEVER the access token itself.';
comment on column public.tenant_channels.display_phone_number is
  'Customer-visible number in E.164 (+254...). Stored as text because of the leading plus.';

create index if not exists tenant_channels_tenant_idx
  on public.tenant_channels (tenant_id);

-- At most one default channel per tenant; a partial unique index keeps that a
-- database guarantee rather than a convention.
create unique index if not exists tenant_channels_one_default_per_tenant_idx
  on public.tenant_channels (tenant_id)
  where is_default;

drop trigger if exists tenant_channels_set_updated_at on public.tenant_channels;
create trigger tenant_channels_set_updated_at
  before update on public.tenant_channels
  for each row execute function app.set_updated_at();
