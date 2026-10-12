-- ============================================================================
-- LOCAL DEVELOPMENT / CI ONLY -- NEVER applied to Supabase.
-- ============================================================================
-- Mirrors the Supabase-managed service_role so compatibility tests can verify its grants.

do $$
begin
  if coalesce(current_setting('saas.allow_dev_data', true), '') <> 'on' then
    raise exception 'development-only script: refusing to create service_role without saas.allow_dev_data=on (db/run_migrations.py sets it for local databases only)';
  end if;

  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin bypassrls;
  end if;
end
$$;
