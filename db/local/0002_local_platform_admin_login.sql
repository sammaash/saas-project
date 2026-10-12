-- ============================================================================
-- LOCAL DEVELOPMENT / CI ONLY -- NEVER applied to Supabase or staging.
-- ============================================================================
-- Throwaway password for the platform API login so tests and local runs can connect as
-- saas_platform_admin. Real environments set this credential out of band.

do $$ begin if coalesce(current_setting('saas.allow_dev_data', true), '') <> 'on' then raise exception 'development-only script: refusing to set a known password without saas.allow_dev_data=on (db/run_migrations.py sets it for local databases only)'; end if; end $$;

alter role saas_platform_admin with login password 'dev_platform_admin_password';
