-- ============================================================================
-- LOCAL DEVELOPMENT / CI ONLY -- NEVER applied to Supabase or staging.
-- ============================================================================
-- Throwaway password for the platform API login so tests and local runs can connect as
-- saas_platform_admin. Real environments set this credential out of band.

alter role saas_platform_admin with login password 'dev_platform_admin_password';
