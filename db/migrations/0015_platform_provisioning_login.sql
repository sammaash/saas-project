-- ============================================================================
-- Phase 1 / 0015 -- isolated platform API capability
-- ============================================================================
-- Provision the credential out-of-band. This role can execute the single atomic
-- provisioning function and has no direct table privileges.
alter role saas_platform_admin login noinherit nobypassrls;

comment on role saas_platform_admin is
  'Trusted platform API login. May execute app.provision_tenant only; configure credentials out-of-band.';