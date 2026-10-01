-- ============================================================================
-- Phase 1 / 0005 -- tenant_users (dashboard membership: who may act for a tenant)
-- ============================================================================
-- The unique key is (tenant_id, user_id), NOT user_id: a person may legitimately
-- belong to more than one tenant, so one-user-one-tenant is never assumed.

create table if not exists public.tenant_users (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references public.tenants (id) on delete cascade,

  -- Supabase Auth user id (auth.users.id).
  --
  -- Deliberately NO foreign key to auth.users: that table does not exist on plain
  -- PostgreSQL, so an FK would make these migrations unrunnable in local
  -- development and CI without an auth shim, and would couple the foundation to
  -- Supabase internals. Identity is resolved from verified JWT claims by
  -- app.current_user_id(). A Supabase-only FK can be added later if the extra
  -- referential guarantee is wanted.
  user_id     uuid not null,

  role        text not null,
  status      text not null default 'invited',
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),

  constraint tenant_users_role_check
    check (role in ('owner', 'staff')),
  constraint tenant_users_status_check
    check (status in ('invited', 'active', 'disabled')),
  constraint tenant_users_tenant_user_key
    unique (tenant_id, user_id)
);

comment on table public.tenant_users is
  'Membership of a Supabase Auth user in a tenant. Source of truth for RLS membership checks.';
comment on column public.tenant_users.user_id is
  'Supabase Auth user id. No FK by design so migrations run on any PostgreSQL.';
comment on column public.tenant_users.role is
  'MVP roles are owner and staff only. owner may manage the tenant; staff work orders and chats.';

-- Lookups by user (sign-in: "which tenants does this user belong to").
create index if not exists tenant_users_user_idx
  on public.tenant_users (user_id);

create index if not exists tenant_users_tenant_idx
  on public.tenant_users (tenant_id);

drop trigger if exists tenant_users_set_updated_at on public.tenant_users;
create trigger tenant_users_set_updated_at
  before update on public.tenant_users
  for each row execute function app.set_updated_at();
