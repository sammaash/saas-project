-- ============================================================================
-- Phase 1 / 0003 -- tenants (the root of the tenancy model)
-- ============================================================================
-- `tenants` is the one table that is not "tenant-owned": its primary key IS the
-- tenant identity, which is why it has no tenant_id column. Every other Phase 1
-- table points at this row.

create table if not exists public.tenants (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  slug        text not null unique,
  status      text not null default 'active',
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),

  -- text + CHECK rather than a native enum: adding a status later is then an
  -- ordinary migration instead of ALTER TYPE gymnastics.
  constraint tenants_status_check
    check (status in ('active', 'suspended', 'inactive')),
  constraint tenants_name_check
    check (length(btrim(name)) between 1 and 200),
  -- Lowercase, url-safe, 3-40 characters: the slug appears in URLs and support
  -- conversations, so the shape is a real business constraint worth enforcing.
  constraint tenants_slug_check
    check (slug ~ '^[a-z0-9][a-z0-9-]{2,39}$')
);

comment on table public.tenants is
  'Tenant (business) root record. Tenant-owned tables reference this row.';
comment on column public.tenants.status is
  'active | suspended | inactive. Suspension is a billing/abuse state, not a data state.';

create index if not exists tenants_status_idx on public.tenants (status);

drop trigger if exists tenants_set_updated_at on public.tenants;
create trigger tenants_set_updated_at
  before update on public.tenants
  for each row execute function app.set_updated_at();
