-- ============================================================================
-- Phase 4 / 0027 -- re-apply hosted-platform privilege hardening
-- ============================================================================
-- This hardening is repeated by later migrations whenever new public objects are added,
-- because hosted Supabase default privileges can grant them to broad roles.

do $$
begin
  if to_regclass('public.saas_schema_migrations') is not null then
    alter table public.saas_schema_migrations enable row level security;
    revoke all on public.saas_schema_migrations from public, anon, authenticated;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
      execute 'revoke all on public.saas_schema_migrations from service_role';
    end if;
  end if;
end
$$;

revoke all on
  public.tenants, public.tenant_channels, public.tenant_users, public.tenant_settings,
  public.plans, public.subscriptions, public.audit_log, public.products,
  public.product_variants, public.platform_admins, public.reserved_phone_numbers,
  public.catalogue_import_batches, public.quotations, public.quotation_lines
  from anon, authenticated;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on
      public.tenants, public.tenant_channels, public.tenant_users, public.tenant_settings,
      public.plans, public.subscriptions, public.audit_log, public.products,
      public.product_variants, public.platform_admins, public.reserved_phone_numbers,
      public.catalogue_import_batches, public.quotations, public.quotation_lines
      from service_role';
    execute 'revoke all on sequence public.audit_log_id_seq,
      public.quotation_lines_id_seq from service_role';
  end if;
end
$$;

revoke all on sequence public.audit_log_id_seq, public.quotation_lines_id_seq
  from anon, authenticated;

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
grant select on public.quotations, public.quotation_lines to authenticated;
