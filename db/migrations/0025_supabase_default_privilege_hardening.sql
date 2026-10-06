-- ============================================================================
-- Phase 3 / 0025 -- neutralise hosted-platform default privileges (Supabase)
-- ============================================================================
-- Supabase sets default privileges so every table, sequence and function created by the
-- migration role in schema public is granted ALL to anon, authenticated and service_role.
-- Row Level Security still filters rows, but TRUNCATE, TRIGGER and REFERENCES ignore it,
-- and a table without RLS (the migration ledger) would be fully writable through the Data
-- API. This migration is idempotent: it removes every privilege from anon and
-- authenticated on the platform tables and re-grants exactly the intended set. On plain
-- PostgreSQL it changes nothing.

-- The migration ledger is not tenant data and no application role may touch it.
do $$
begin
  if to_regclass('public.saas_schema_migrations') is not null then
    alter table public.saas_schema_migrations enable row level security;
    revoke all on public.saas_schema_migrations from public, anon, authenticated;
  end if;
end
$$;

-- Only this platform's own tables are touched, so unrelated objects that share the schema on
-- a hosted project keep their grants.
revoke all on
  public.tenants, public.tenant_channels, public.tenant_users, public.tenant_settings,
  public.plans, public.subscriptions, public.audit_log, public.products,
  public.product_variants, public.platform_admins, public.reserved_phone_numbers
  from anon, authenticated;
revoke all on sequence public.audit_log_id_seq from anon, authenticated;

-- Intended dashboard-session (authenticated) privileges; anon holds none. Every grant here
-- is already made by an earlier migration; they are repeated because the revoke above also
-- removed them. RLS policies still decide which rows each statement may touch.
grant select on public.tenants to authenticated;
grant update (name) on public.tenants to authenticated;
grant select on public.tenant_channels to authenticated;
grant select, insert, update, delete on public.tenant_users to authenticated;
grant select, insert, update on public.tenant_settings to authenticated;
grant select on public.plans to authenticated;
grant select on public.subscriptions to authenticated;
grant select on public.audit_log to authenticated;
grant select, insert, update, delete on public.products to authenticated;
grant select, insert, update, delete on public.product_variants to authenticated;
