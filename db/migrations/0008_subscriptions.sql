-- ============================================================================
-- Phase 1 / 0008 -- subscriptions (tenant billing state)
-- ============================================================================
-- Phase 1 supports MANUAL / invoice billing (the first ~20 tenants). Automated
-- M-Pesa STK billing is explicitly out of scope: billing_method allows the future
-- value, but no charging code exists and no STK push is implemented.

create table if not exists public.subscriptions (
  id                    uuid primary key default gen_random_uuid(),
  tenant_id             uuid not null references public.tenants (id) on delete cascade,
  plan_id               uuid not null references public.plans (id),
  status                text not null default 'trial',
  billing_method        text not null default 'manual',
  current_period_start  date,
  current_period_end    date,
  trial_ends_at         timestamptz,
  next_billing_at       timestamptz,
  cancelled_at          timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),

  constraint subscriptions_status_check
    check (status in ('trial', 'active', 'past_due', 'suspended', 'cancelled', 'expired')),
  constraint subscriptions_billing_method_check
    check (billing_method in ('manual', 'mpesa_stk')),
  constraint subscriptions_period_order_check
    check (
      current_period_start is null
      or current_period_end is null
      or current_period_end >= current_period_start
    )
);

comment on table public.subscriptions is
  'Tenant subscription state. Manual/invoice billing in MVP; STK billing is a later phase.';
comment on column public.subscriptions.billing_method is
  'manual (MVP) or mpesa_stk (reserved for later). No charging logic exists in Phase 1.';
comment on column public.subscriptions.status is
  'trial | active | past_due | suspended | cancelled | expired.';

create index if not exists subscriptions_tenant_idx
  on public.subscriptions (tenant_id);

-- Billing work queue: only live subscriptions can become due.
create index if not exists subscriptions_next_billing_idx
  on public.subscriptions (next_billing_at)
  where status in ('trial', 'active', 'past_due');

-- One live subscription per tenant, enforced by the database rather than by
-- convention. Terminal states (cancelled, expired) are excluded so history is kept
-- while a tenant can only ever have one current subscription.
create unique index if not exists subscriptions_one_live_per_tenant_idx
  on public.subscriptions (tenant_id)
  where status in ('trial', 'active', 'past_due', 'suspended');

drop trigger if exists subscriptions_set_updated_at on public.subscriptions;
create trigger subscriptions_set_updated_at
  before update on public.subscriptions
  for each row execute function app.set_updated_at();
