-- ============================================================================
-- Phase 3 / 0019 -- platform administrators (separate from tenant membership)
-- ============================================================================
-- A platform administrator may operate on every tenant. That authority is NOT a
-- tenant_users role: it has no tenant_id and never appears in tenant policies.
--
-- user_id is the verified Supabase Auth `sub`. It is inserted by an operator
-- (db/bootstrap_platform_admin.py) after the UUID has been verified against a real
-- sign-in; it is never invented and no application role may write this table.

create table if not exists public.platform_admins (
  user_id     uuid primary key,
  status      text not null default 'active',
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint platform_admins_status_check check (status in ('active', 'disabled'))
);

comment on table public.platform_admins is
  'Platform administrators (verified Auth user ids). Not tenant-scoped; no application role may read or write it directly.';

drop trigger if exists platform_admins_set_updated_at on public.platform_admins;
create trigger platform_admins_set_updated_at
  before update on public.platform_admins
  for each row execute function app.set_updated_at();

-- RLS enabled with no policies: every application role is denied. FORCE is not applied
-- because app.is_platform_admin() reads the table as its owner (same documented pattern
-- as tenant_users in 0010).
alter table public.platform_admins enable row level security;
revoke all on public.platform_admins from public, anon, authenticated, app_backend;

create or replace function app.is_platform_admin()
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
  select exists (
    select 1
      from public.platform_admins pa
     where pa.user_id = app.current_user_id()
       and pa.status = 'active'
  );
$$;

comment on function app.is_platform_admin() is
  'True when the verified identity in request.jwt.claims is an active platform administrator.';

revoke all on function app.is_platform_admin() from public;
revoke execute on function app.is_platform_admin() from app_backend, authenticated, anon;
grant execute on function app.is_platform_admin() to saas_platform_admin, saas_platform_provisioner;

-- Raises unless the caller is a platform administrator. Every platform function starts
-- with this call so authorization is enforced in the database, not only in the web layer.
create or replace function app.require_platform_admin()
returns void
language plpgsql
stable
set search_path = pg_catalog, public, pg_temp
as $$
begin
  if not app.is_platform_admin() then
    raise exception using errcode = '42501', message = 'platform administrator required';
  end if;
end;
$$;

revoke all on function app.require_platform_admin() from public;
revoke execute on function app.require_platform_admin() from app_backend, authenticated, anon;
grant execute on function app.require_platform_admin() to saas_platform_admin, saas_platform_provisioner;
