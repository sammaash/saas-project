-- ============================================================================
-- Phase 1 / 0007 -- plans (platform-owned catalogue)
-- ============================================================================
-- Plans belong to the platform, not to a tenant, so this table deliberately has
-- NO tenant_id column. It is the one table in Phase 1 that is not tenant-scoped --
-- which is exactly why the structural test in tests/test_saas_schema_structure.py
-- treats `plans` (and the `tenants` root) as an explicit, documented exception.

create table if not exists public.plans (
  id             uuid primary key default gen_random_uuid(),
  code           text not null unique,
  name           text not null,
  description    text,
  monthly_price  numeric(12,2) not null default 0,
  currency       text not null default 'KES',
  is_active      boolean not null default true,

  -- Quota/config blob: message allowance, AI (token) allowance, seat limit.
  -- AI is INCLUDED in the plan and will be limited; AI metering is NOT implemented
  -- in Phase 1, so these are declared limits only.
  limits         jsonb not null default '{}'::jsonb,

  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),

  constraint plans_code_check
    check (code ~ '^[a-z0-9_]{2,40}$'),
  constraint plans_monthly_price_check
    check (monthly_price >= 0),
  constraint plans_currency_check
    check (currency ~ '^[A-Z]{3}$'),
  constraint plans_limits_is_object
    check (jsonb_typeof(limits) = 'object')
);

comment on table public.plans is
  'Platform-owned subscription plans. Not tenant-scoped: no tenant_id by design.';
comment on column public.plans.monthly_price is
  'numeric, never float: money must be exact. Currency stored explicitly alongside.';
comment on column public.plans.limits is
  'Declared quotas (messages, AI, seats). Enforcement/metering is a later phase.';

create index if not exists plans_active_idx on public.plans (is_active);

drop trigger if exists plans_set_updated_at on public.plans;
create trigger plans_set_updated_at
  before update on public.plans
  for each row execute function app.set_updated_at();
