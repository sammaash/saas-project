-- ============================================================================
-- LOCAL DEVELOPMENT / CI ONLY -- NEVER applied to Supabase.
-- ============================================================================
-- Gives app_backend a LOGIN and a throwaway development password so the SaaS layer and
-- the isolation tests can connect as the real application role -- the one that is
-- subject to RLS.
--
-- Shared and production environments set this credential out of band; a password for a
-- real environment must never exist in a repository file.
--
-- Applied by:  python db/run_migrations.py --include-local

alter role app_backend with login password 'dev_app_backend_password';

comment on role app_backend is
  'Application role for backend/worker tenant-scoped queries (subject to RLS). Development LOGIN password applied by db/local/.';
