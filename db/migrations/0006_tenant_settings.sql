-- ============================================================================
-- Phase 1 / 0006 -- tenant_settings (per-tenant configuration)
-- ============================================================================
-- One row per tenant, so tenant_id IS the primary key.
--
-- JSONB is used ONLY for configuration written and read as a whole (tenant business
-- rules, bot persona, business hours). Anything that will be filtered, joined,
-- sorted or aggregated belongs in a real column instead -- a JSONB path cannot be
-- indexed or constrained as cheaply, and a platform-wide query over JSONB is a
-- table scan waiting to happen.

create table if not exists public.tenant_settings (
  tenant_id       uuid primary key references public.tenants (id) on delete cascade,
  timezone        text not null default 'Africa/Nairobi',
  currency        text not null default 'KES',

  -- Deposit rate, deposit deadline, delivery rate per km, personalisation prices,
  -- per-vertical config (salon services, SACCO routes). Read as a whole by the bot.
  business_rules  jsonb not null default '{}'::jsonb,

  -- Greeting, tone, escalation triggers.
  bot_persona     jsonb not null default '{}'::jsonb,

  -- Opening hours and closures.
  business_hours  jsonb not null default '{}'::jsonb,

  -- Catch-all for future non-queried configuration, so a new setting does not
  -- require a migration every time.
  configuration   jsonb not null default '{}'::jsonb,

  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),

  constraint tenant_settings_currency_check
    check (currency ~ '^[A-Z]{3}$'),
  constraint tenant_settings_business_rules_is_object
    check (jsonb_typeof(business_rules) = 'object'),
  constraint tenant_settings_bot_persona_is_object
    check (jsonb_typeof(bot_persona) = 'object'),
  constraint tenant_settings_business_hours_is_object
    check (jsonb_typeof(business_hours) = 'object'),
  constraint tenant_settings_configuration_is_object
    check (jsonb_typeof(configuration) = 'object')
);

comment on table public.tenant_settings is
  'Per-tenant configuration. JSONB columns hold whole-document configuration only.';
comment on column public.tenant_settings.timezone is
  'Display/calendar timezone for the tenant. Instants are stored in UTC.';
comment on column public.tenant_settings.currency is
  'ISO 4217 code. KES for all tenants in Phase 1; multi-currency is out of scope.';

drop trigger if exists tenant_settings_set_updated_at on public.tenant_settings;
create trigger tenant_settings_set_updated_at
  before update on public.tenant_settings
  for each row execute function app.set_updated_at();
