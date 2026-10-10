-- ============================================================================
-- Phase 1 / 0015 -- isolated platform API capability
-- ============================================================================
-- Provision the credential out-of-band. This role can execute the single atomic
-- provisioning function and has no direct table privileges.
alter role saas_platform_admin login noinherit;

-- ALTER ROLE ... NOBYPASSRLS needs superuser, so on hosted PostgreSQL (Supabase) we warn
-- rather than fail the migration (verify manually in that case). Same pattern as 0002.
do $$
begin
  alter role saas_platform_admin nobypassrls;
exception
  when insufficient_privilege then
    raise notice 'Skipped NOBYPASSRLS on saas_platform_admin (requires superuser). Verify saas_platform_admin does not hold BYPASSRLS.';
end
$$;

comment on role saas_platform_admin is
  'Trusted platform API login. May execute app.provision_tenant only; configure credentials out-of-band.';