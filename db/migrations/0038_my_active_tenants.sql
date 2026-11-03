-- 0038: the caller's own active tenant memberships (portal login).
--
-- app_backend, the web application role, needs one cross-tenant read at login time:
-- which tenants does this signed-in user belong to? Everything else stays inside the
-- per-tenant RLS model. SECURITY DEFINER is required because app_backend cannot read
-- other tenants' tenant_users rows; the function takes NO user id (the caller is
-- whoever the server-verified request.jwt.claims says) and returns only active
-- memberships, so it can never enumerate anyone else's memberships and an
-- unauthenticated caller (no jwt claims) gets zero rows.
--
-- Ownership follows the established pattern (0014, 0021): saas_platform_provisioner
-- is NOLOGIN/NOINHERIT/NOBYPASSRLS, holds SELECT on public.tenant_users and EXECUTE
-- on app.current_user_id(), and owns the other SECURITY DEFINER helpers. Execution is
-- restricted to app_backend; PUBLIC, anon and authenticated are revoked, matching the
-- 0019/0029 privilege hardening.

create or replace function app.my_active_tenants()
returns table (tenant_id uuid, role text)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
  select tu.tenant_id, tu.role
    from public.tenant_users tu
   where tu.user_id = app.current_user_id()
     and tu.status = 'active'
$$;

alter function app.my_active_tenants() owner to saas_platform_provisioner;
revoke all on function app.my_active_tenants() from public;
revoke execute on function app.my_active_tenants() from anon, authenticated;
grant execute on function app.my_active_tenants() to app_backend;

comment on function app.my_active_tenants() is
  'Active tenant memberships of the caller only (verified JWT sub via request.jwt.claims); zero rows when unauthenticated.';