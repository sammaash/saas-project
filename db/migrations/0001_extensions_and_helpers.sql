-- ============================================================================
-- Phase 1 / 0001 -- extensions, the `app` schema, and context helpers
-- ============================================================================
-- These migrations are Supabase-compatible AND run on plain PostgreSQL (local
-- development and CI) with no shims: they never reference the `auth` schema, and
-- identity is read from the JWT claims setting that Supabase's PostgREST applies
-- to every request.

-- gen_random_uuid() is core in PostgreSQL 13+, so pgcrypto is only a
-- compatibility safety net for older servers.
create extension if not exists pgcrypto;

create schema if not exists app;

comment on schema app is
  'Multi-tenant SaaS helpers (Phase 1). Application roles receive only explicit grants.';

-- ----------------------------------------------------------------------------
-- updated_at maintenance.
-- Done by trigger so that no application code path can forget it. The existing
-- SQLite code sets updated_at by hand at each call site, which is exactly how a
-- timestamp column drifts.
-- ----------------------------------------------------------------------------
create or replace function app.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

comment on function app.set_updated_at() is
  'Trigger function: stamps updated_at on every UPDATE.';

-- ----------------------------------------------------------------------------
-- Tenant context -- established server-side ONLY.
--
-- The backend runs, inside a transaction and before any tenant-scoped query:
--
--     select set_config('app.tenant_id', <tenant uuid as text>, true)
--
-- The third argument `true` makes the setting transaction-local (the SQL
-- equivalent of SET LOCAL). That matters because connections are pooled: a
-- session-level SET would leak one tenant's context into the next request on the
-- same connection, which is a silent cross-tenant data-write bug.
--
-- FAIL-CLOSED: with no context set this returns NULL. Every RLS predicate that
-- compares tenant_id to NULL evaluates to NULL, which matches ZERO rows -- never
-- "all rows". There is no code path where missing context widens access.
-- ----------------------------------------------------------------------------
create or replace function app.current_tenant_id()
returns uuid
language sql
stable
as $$
  select nullif(current_setting('app.tenant_id', true), '')::uuid;
$$;

comment on function app.current_tenant_id() is
  'Tenant id for the current transaction, or NULL when no context is set (fails closed).';

-- ----------------------------------------------------------------------------
-- Authenticated end-user id.
--
-- Read from request.jwt.claims (set by PostgREST after it verifies the Supabase
-- JWT) instead of auth.uid(), so these migrations behave identically on Supabase,
-- locally and in CI without an `auth` schema shim.
-- ----------------------------------------------------------------------------
create or replace function app.current_user_id()
returns uuid
language sql
stable
as $$
  select (nullif(current_setting('request.jwt.claims', true), '')::json ->> 'sub')::uuid;
$$;

comment on function app.current_user_id() is
  'Supabase Auth user id taken from verified JWT claims, or NULL when unauthenticated.';
