-- ============================================================================
-- 0042 -- platform-side tenant onboarding and member management
-- ============================================================================
-- Adds what the admin dashboard needs to onboard a business owner and manage tenant
-- members without hand-editing the database:
--
--   * public.platform_user_directory: the email and display name of every Supabase Auth
--     account the platform has invited or linked. tenant_users stores only the Auth UUID
--     (0005), so without this the dashboard could not search members by email or name.
--     It is platform-only: RLS on, no tenant policy, no grant to any client-facing role.
--   * app.platform_* functions to list, add, update and remove memberships across tenants,
--     to record a directory entry and to store a tenant's contact details.
--
-- Every function follows the 0021 contract: SECURITY DEFINER, owned by the NOLOGIN
-- saas_platform_provisioner, app.require_platform_admin() first, executable only by
-- saas_platform_admin, pinned search_path, no dynamic SQL. Every change writes an
-- audit_log row in the same transaction. The final-active-owner trigger (0014) still
-- applies to every update and delete made here.
--
-- Membership roles stay owner and staff (0005 check constraint): this migration does not
-- widen the role model, and it never touches public.platform_admins.

-- ---------------------------------------------------------------------------
-- Directory of Auth accounts known to the platform (no secrets, no passwords)
-- ---------------------------------------------------------------------------
create table if not exists public.platform_user_directory (
  user_id     uuid primary key,
  email       text not null,
  full_name   text,
  invited_at  timestamptz,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint platform_user_directory_email_key unique (email),
  constraint platform_user_directory_email_check
    check (email = lower(email) and length(email) between 3 and 320 and position('@' in email) > 1),
  constraint platform_user_directory_full_name_check
    check (full_name is null or length(full_name) between 1 and 200)
);

comment on table public.platform_user_directory is
  'Email and display name of Supabase Auth accounts invited or linked by a platform administrator. Platform-only.';

drop trigger if exists platform_user_directory_set_updated_at on public.platform_user_directory;
create trigger platform_user_directory_set_updated_at
  before update on public.platform_user_directory
  for each row execute function app.set_updated_at();

alter table public.platform_user_directory enable row level security;
alter table public.platform_user_directory force row level security;

revoke all on public.platform_user_directory from public, anon, authenticated, app_backend;
do $$
begin
  -- Supabase default privileges grant every new public table to service_role (see 0041).
  if exists (select 1 from pg_catalog.pg_roles where rolname = 'service_role') then
    execute 'revoke all on public.platform_user_directory from service_role';
  end if;
end
$$;

grant select, insert, update on public.platform_user_directory to saas_platform_provisioner;

drop policy if exists platform_user_directory_provisioner_all on public.platform_user_directory;
create policy platform_user_directory_provisioner_all on public.platform_user_directory
  for all to saas_platform_provisioner using (true) with check (true);

-- ---------------------------------------------------------------------------
-- Provisioner privileges for membership changes and tenant contact details
-- ---------------------------------------------------------------------------
grant update (role, status) on public.tenant_users to saas_platform_provisioner;
grant delete on public.tenant_users to saas_platform_provisioner;

drop policy if exists tenant_users_platform_provisioner_update on public.tenant_users;
create policy tenant_users_platform_provisioner_update on public.tenant_users
  for update to saas_platform_provisioner using (true) with check (true);
drop policy if exists tenant_users_platform_provisioner_delete on public.tenant_users;
create policy tenant_users_platform_provisioner_delete on public.tenant_users
  for delete to saas_platform_provisioner using (true);

grant update (configuration) on public.tenant_settings to saas_platform_provisioner;
drop policy if exists tenant_settings_platform_provisioner_update on public.tenant_settings;
create policy tenant_settings_platform_provisioner_update on public.tenant_settings
  for update to saas_platform_provisioner using (true) with check (true);

-- ---------------------------------------------------------------------------
-- Functions
-- ---------------------------------------------------------------------------
create or replace function app.platform_upsert_user_directory(
  p_user_id uuid,
  p_email text,
  p_full_name text default null,
  p_invited boolean default false
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
begin
  perform app.require_platform_admin();
  if p_user_id is null or p_email is null then
    raise exception using errcode = '22004', message = 'user id and email are required';
  end if;

  insert into public.platform_user_directory (user_id, email, full_name, invited_at)
  values (p_user_id, lower(btrim(p_email)), nullif(btrim(p_full_name), ''),
          case when p_invited then now() end)
  on conflict (user_id) do update
     set email = excluded.email,
         full_name = coalesce(excluded.full_name, public.platform_user_directory.full_name),
         invited_at = coalesce(excluded.invited_at, public.platform_user_directory.invited_at);
end;
$$;

create or replace function app.platform_list_members(
  p_tenant_id uuid default null,
  p_role text default null,
  p_status text default null,
  p_search text default null
)
returns table (
  membership_id uuid,
  tenant_id uuid,
  tenant_name text,
  tenant_slug text,
  tenant_status text,
  user_id uuid,
  role text,
  status text,
  created_at timestamptz,
  updated_at timestamptz,
  email text,
  full_name text,
  invited_at timestamptz
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  needle text := nullif(lower(btrim(coalesce(p_search, ''))), '');
begin
  perform app.require_platform_admin();

  return query
  select tu.id, t.id, t.name, t.slug, t.status, tu.user_id, tu.role, tu.status,
         tu.created_at, tu.updated_at, d.email, d.full_name, d.invited_at
    from public.tenant_users tu
    join public.tenants t on t.id = tu.tenant_id
    left join public.platform_user_directory d on d.user_id = tu.user_id
   where (p_tenant_id is null or tu.tenant_id = p_tenant_id)
     and (p_role is null or tu.role = p_role)
     and (p_status is null or tu.status = p_status)
     -- strpos, not LIKE: a search containing % or _ is matched literally.
     and (needle is null
          or strpos(coalesce(d.email, ''), needle) > 0
          or strpos(lower(coalesce(d.full_name, '')), needle) > 0
          or strpos(tu.user_id::text, needle) > 0)
   order by t.name, case tu.role when 'owner' then 0 else 1 end, coalesce(d.email, ''), tu.created_at;
end;
$$;

create or replace function app.platform_add_member(
  p_tenant_id uuid,
  p_user_id uuid,
  p_role text,
  p_status text default 'active'
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  new_id uuid;
begin
  perform app.require_platform_admin();
  if not exists (select 1 from public.tenants t where t.id = p_tenant_id) then
    raise exception using errcode = 'P0002', message = 'tenant not found';
  end if;
  if p_status not in ('invited', 'active') then
    raise exception using errcode = '22023', message = 'a new membership must be invited or active';
  end if;

  insert into public.tenant_users (tenant_id, user_id, role, status)
  values (p_tenant_id, p_user_id, p_role, p_status)
  returning id into new_id;

  insert into public.audit_log (tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata)
  values (p_tenant_id, app.current_user_id(), 'platform', 'member.added', 'tenant_user', new_id::text,
          jsonb_build_object('user_id', p_user_id, 'role', p_role, 'status', p_status));
  return new_id;
end;
$$;

create or replace function app.platform_update_member(
  p_tenant_id uuid,
  p_membership_id uuid,
  p_role text default null,
  p_status text default null
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  before_row public.tenant_users%rowtype;
begin
  perform app.require_platform_admin();

  -- The tenant id must match: a membership id from another tenant is "not found", so a
  -- tampered form can never change a different tenant's member.
  select * into before_row
    from public.tenant_users tu
   where tu.id = p_membership_id and tu.tenant_id = p_tenant_id
   for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'membership not found';
  end if;

  update public.tenant_users
     set role = coalesce(p_role, before_row.role),
         status = coalesce(p_status, before_row.status)
   where id = p_membership_id;

  insert into public.audit_log (tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata)
  values (p_tenant_id, app.current_user_id(), 'platform', 'member.updated', 'tenant_user', p_membership_id::text,
          jsonb_build_object('user_id', before_row.user_id,
                             'from', jsonb_build_object('role', before_row.role, 'status', before_row.status),
                             'to', jsonb_build_object('role', coalesce(p_role, before_row.role),
                                                      'status', coalesce(p_status, before_row.status))));
end;
$$;

create or replace function app.platform_remove_member(
  p_tenant_id uuid,
  p_membership_id uuid
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  removed public.tenant_users%rowtype;
begin
  perform app.require_platform_admin();

  delete from public.tenant_users tu
   where tu.id = p_membership_id and tu.tenant_id = p_tenant_id
  returning * into removed;
  if not found then
    raise exception using errcode = 'P0002', message = 'membership not found';
  end if;

  insert into public.audit_log (tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata)
  values (p_tenant_id, app.current_user_id(), 'platform', 'member.removed', 'tenant_user', p_membership_id::text,
          jsonb_build_object('user_id', removed.user_id, 'role', removed.role, 'status', removed.status));
end;
$$;

create or replace function app.platform_set_tenant_contact(
  p_tenant_id uuid,
  p_email text default null,
  p_phone text default null
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
begin
  perform app.require_platform_admin();

  update public.tenant_settings ts
     set configuration = jsonb_set(
           coalesce(ts.configuration, '{}'::jsonb), '{contact}',
           jsonb_strip_nulls(jsonb_build_object(
             'email', nullif(lower(btrim(p_email)), ''),
             'phone', nullif(btrim(p_phone), ''))))
   where ts.tenant_id = p_tenant_id;
  if not found then
    raise exception using errcode = 'P0002', message = 'tenant not found';
  end if;

  insert into public.audit_log (tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata)
  values (p_tenant_id, app.current_user_id(), 'platform', 'tenant.contact_updated', 'tenant_settings',
          p_tenant_id::text, '{}'::jsonb);
end;
$$;

-- ---------------------------------------------------------------------------
-- Ownership and execute grants (same contract as 0021)
-- ---------------------------------------------------------------------------
grant create on schema app to saas_platform_provisioner;
alter function app.platform_upsert_user_directory(uuid, text, text, boolean) owner to saas_platform_provisioner;
alter function app.platform_list_members(uuid, text, text, text) owner to saas_platform_provisioner;
alter function app.platform_add_member(uuid, uuid, text, text) owner to saas_platform_provisioner;
alter function app.platform_update_member(uuid, uuid, text, text) owner to saas_platform_provisioner;
alter function app.platform_remove_member(uuid, uuid) owner to saas_platform_provisioner;
alter function app.platform_set_tenant_contact(uuid, text, text) owner to saas_platform_provisioner;
revoke create on schema app from saas_platform_provisioner;

revoke all on function app.platform_upsert_user_directory(uuid, text, text, boolean) from public;
revoke all on function app.platform_list_members(uuid, text, text, text) from public;
revoke all on function app.platform_add_member(uuid, uuid, text, text) from public;
revoke all on function app.platform_update_member(uuid, uuid, text, text) from public;
revoke all on function app.platform_remove_member(uuid, uuid) from public;
revoke all on function app.platform_set_tenant_contact(uuid, text, text) from public;

revoke execute on function app.platform_upsert_user_directory(uuid, text, text, boolean) from app_backend, authenticated, anon;
revoke execute on function app.platform_list_members(uuid, text, text, text) from app_backend, authenticated, anon;
revoke execute on function app.platform_add_member(uuid, uuid, text, text) from app_backend, authenticated, anon;
revoke execute on function app.platform_update_member(uuid, uuid, text, text) from app_backend, authenticated, anon;
revoke execute on function app.platform_remove_member(uuid, uuid) from app_backend, authenticated, anon;
revoke execute on function app.platform_set_tenant_contact(uuid, text, text) from app_backend, authenticated, anon;

grant execute on function app.platform_upsert_user_directory(uuid, text, text, boolean) to saas_platform_admin;
grant execute on function app.platform_list_members(uuid, text, text, text) to saas_platform_admin;
grant execute on function app.platform_add_member(uuid, uuid, text, text) to saas_platform_admin;
grant execute on function app.platform_update_member(uuid, uuid, text, text) to saas_platform_admin;
grant execute on function app.platform_remove_member(uuid, uuid) to saas_platform_admin;
grant execute on function app.platform_set_tenant_contact(uuid, text, text) to saas_platform_admin;
