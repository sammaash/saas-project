-- ============================================================================
-- Phase 1 / 0002 -- application roles
-- ============================================================================
-- The platform must NOT use the Supabase `service_role` key for ordinary
-- tenant-scoped queries: service_role BYPASSES RLS, which would make every policy
-- in 0011 decorative. `app_backend` is a dedicated, non-owner role that IS subject
-- to RLS, so a query that forgets its tenant filter fails closed instead of
-- leaking every tenant's data.
--
-- No passwords in this file. LOGIN and credentials are environment-specific and
-- are set out of band; development uses db/local/0001_local_app_backend_login.sql

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'app_backend') then
    -- NOLOGIN here; local development adds LOGIN + a dev password separately.
    create role app_backend nologin;
  end if;

  -- `authenticated` and `anon` are created for us on Supabase; they are created
  -- locally so that the same migrations and grants behave identically in
  -- development and CI.
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;

  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin;
  end if;
end
$$;

comment on role app_backend is
  'Application role for backend and worker tenant-scoped queries. Deliberately NOT a table owner and NOT BYPASSRLS, so RLS applies to it.';

-- Defence in depth: assert that the application role cannot bypass RLS.
-- ALTER ROLE ... NOBYPASSRLS needs superuser, so on hosted PostgreSQL we warn
-- rather than fail the migration (verify manually in that case).
do $$
begin
  begin
    alter role app_backend nobypassrls;
  exception
    when insufficient_privilege then
      raise notice 'Skipped NOBYPASSRLS on app_backend (requires superuser). Verify app_backend does not hold BYPASSRLS.';
  end;
end
$$;
