-- Migration 0043: tenant-portal access policy, verified ownership, directory
-- reassignment, audit completeness and cross-process password-reset throttling.
--
-- Builds on 0042 (platform member management). Additive and idempotent; it replaces
-- functions with the same signatures and adds one table. Older application code keeps
-- working on top of it (it only narrows who the portal admits and which owners count
-- as verified).
--
--   H1/M6  The tenant portal admits members of DRAFT and ACTIVE tenants only.
--          app.my_active_tenants() filters by tenant status, and the per-request guard
--          uses the new app.my_tenant_membership(uuid), which applies the same rule.
--          (Inbound WhatsApp routing already requires an active tenant: 0028.)
--   M3     Tenant readiness requires an active owner with a VERIFIED account, i.e. one
--          the platform resolved through Supabase Auth and recorded in
--          public.platform_user_directory. Membership changes may not leave a tenant
--          that has a verified active owner without one (placeholder owners no longer
--          satisfy the rule once a real owner exists).
--   M1     app.platform_upsert_user_directory reassigns an email whose directory row
--          belongs to a different (deleted / recreated) Auth account, and audits it.
--   M2     Directory changes are audited (no email or name in the metadata).
--   M4     app.platform_list_unattached_accounts lists directory accounts with no
--          membership (invitations whose provisioning failed).
--   M5     app.request_password_reset(text): the only path for self-service reset
--          requests. Returns true only for an account with an active membership of a
--          draft/active tenant, under a per-email and a global hourly cap shared by all
--          application processes (serialized by an advisory lock).

-- ---------------------------------------------------------------------------
-- Audit: platform-level events have no tenant. Allow exactly those actions.
-- ---------------------------------------------------------------------------
drop policy if exists audit_log_platform_provisioner_insert_global on public.audit_log;
create policy audit_log_platform_provisioner_insert_global on public.audit_log
  for insert to saas_platform_provisioner
  with check (
    tenant_id is null
    and (action like 'directory.%' or action = 'auth.password_reset_requested')
  );

grant delete on public.platform_user_directory to saas_platform_provisioner;

-- ---------------------------------------------------------------------------
-- H1/M6: portal access policy
-- ---------------------------------------------------------------------------
create or replace function app.my_active_tenants()
returns table (tenant_id uuid, role text)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
  select tu.tenant_id, tu.role
    from public.tenant_users tu
    join public.tenants t on t.id = tu.tenant_id
   where tu.user_id = app.current_user_id()
     and tu.status = 'active'
     and t.status in ('draft', 'active')
$$;

comment on function app.my_active_tenants() is
  'Active memberships of the caller (verified JWT sub) in draft or active tenants only; zero rows when unauthenticated.';

create or replace function app.my_tenant_membership(p_tenant_id uuid)
returns text
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
  select tu.role
    from public.tenant_users tu
    join public.tenants t on t.id = tu.tenant_id
   where tu.tenant_id = p_tenant_id
     and tu.user_id = app.current_user_id()
     and tu.status = 'active'
     and t.status in ('draft', 'active')
   limit 1
$$;

comment on function app.my_tenant_membership(uuid) is
  'Role of the caller''s active membership of one draft/active tenant, else NULL. Portal access check.';

-- ---------------------------------------------------------------------------
-- M3: verified ownership
-- ---------------------------------------------------------------------------
create or replace function app.platform_tenant_readiness(p_tenant_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  has_tenant boolean;
  has_settings boolean;
  has_catalogue boolean;
  has_owner boolean;
  has_channel boolean;
begin
  perform app.require_platform_admin();

  select exists (select 1 from public.tenants t where t.id = p_tenant_id) into has_tenant;
  if not has_tenant then
    raise exception using errcode = 'P0002', message = 'tenant not found';
  end if;

  select exists (select 1 from public.tenant_settings s where s.tenant_id = p_tenant_id)
    into has_settings;
  select exists (
    select 1
      from public.products p
      join public.product_variants v on v.product_id = p.id and v.tenant_id = p.tenant_id
     where p.tenant_id = p_tenant_id and p.is_active and v.is_active
  ) into has_catalogue;
  -- A placeholder or mistyped UUID has no directory entry, so it never counts.
  select exists (
    select 1
      from public.tenant_users u
      join public.platform_user_directory d on d.user_id = u.user_id
     where u.tenant_id = p_tenant_id and u.role = 'owner' and u.status = 'active'
  ) into has_owner;
  select exists (
    select 1 from public.tenant_channels c where c.tenant_id = p_tenant_id
  ) into has_channel;

  return jsonb_build_object(
    'checks', jsonb_build_array(
      jsonb_build_object('key', 'settings', 'label', 'Business profile and rules saved', 'ok', has_settings, 'required', true),
      jsonb_build_object('key', 'catalogue', 'label', 'At least one active product with an active variant', 'ok', has_catalogue, 'required', true),
      jsonb_build_object('key', 'owner', 'label', 'An active owner with a verified account is assigned', 'ok', has_owner, 'required', true),
      jsonb_build_object('key', 'channel', 'label', 'A WhatsApp channel is recorded', 'ok', has_channel, 'required', false)
    )
  );
end;
$$;

-- Raises 23514 when the change would remove the tenant's last verified active owner.
create or replace function app.platform_assert_verified_owner_remains(
  p_tenant_id uuid,
  p_membership_id uuid
)
returns void
language plpgsql
stable
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
begin
  if exists (
       select 1
         from public.tenant_users u
         join public.platform_user_directory d on d.user_id = u.user_id
        where u.id = p_membership_id and u.tenant_id = p_tenant_id
          and u.role = 'owner' and u.status = 'active')
     and not exists (
       select 1
         from public.tenant_users u
         join public.platform_user_directory d on d.user_id = u.user_id
        where u.tenant_id = p_tenant_id and u.id <> p_membership_id
          and u.role = 'owner' and u.status = 'active')
  then
    raise exception using
      errcode = '23514',
      message = 'tenant must retain at least one active owner with a verified account';
  end if;
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
  new_role text;
  new_status text;
begin
  perform app.require_platform_admin();

  select * into before_row
    from public.tenant_users tu
   where tu.id = p_membership_id and tu.tenant_id = p_tenant_id
   for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'membership not found';
  end if;

  new_role := coalesce(p_role, before_row.role);
  new_status := coalesce(p_status, before_row.status);
  if not (new_role = 'owner' and new_status = 'active') then
    perform app.platform_assert_verified_owner_remains(p_tenant_id, p_membership_id);
  end if;

  update public.tenant_users
     set role = new_role,
         status = new_status
   where id = p_membership_id;

  insert into public.audit_log (tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata)
  values (p_tenant_id, app.current_user_id(), 'platform', 'member.updated', 'tenant_user', p_membership_id::text,
          jsonb_build_object('user_id', before_row.user_id,
                             'from', jsonb_build_object('role', before_row.role, 'status', before_row.status),
                             'to', jsonb_build_object('role', new_role, 'status', new_status)));
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

  perform 1 from public.tenant_users tu
   where tu.id = p_membership_id and tu.tenant_id = p_tenant_id
   for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'membership not found';
  end if;
  perform app.platform_assert_verified_owner_remains(p_tenant_id, p_membership_id);

  delete from public.tenant_users tu
   where tu.id = p_membership_id and tu.tenant_id = p_tenant_id
  returning * into removed;

  insert into public.audit_log (tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata)
  values (p_tenant_id, app.current_user_id(), 'platform', 'member.removed', 'tenant_user', p_membership_id::text,
          jsonb_build_object('user_id', removed.user_id, 'role', removed.role, 'status', removed.status));
end;
$$;

-- ---------------------------------------------------------------------------
-- M1 + M2: directory upsert with stale-email reassignment and audit
-- ---------------------------------------------------------------------------
-- The caller (the platform application) has already resolved p_user_id from Supabase
-- Auth for p_email, which is the source of truth. A different directory row holding the
-- same email is therefore stale (its account was deleted and recreated, or the email
-- moved): it is removed and the reassignment audited. Memberships of the previous id are
-- left untouched and now show as unverified, so an administrator can review them.
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
declare
  v_email text := lower(btrim(p_email));
  v_name text := nullif(btrim(coalesce(p_full_name, '')), '');
  stale_user uuid;
  stale_memberships integer;
  existing public.platform_user_directory%rowtype;
  changed text[] := '{}';
begin
  perform app.require_platform_admin();
  if p_user_id is null or v_email is null or v_email = '' then
    raise exception using errcode = '22004', message = 'user id and email are required';
  end if;

  select d.user_id into stale_user
    from public.platform_user_directory d
   where d.email = v_email and d.user_id <> p_user_id
   for update;
  if found then
    select count(*) into stale_memberships from public.tenant_users tu where tu.user_id = stale_user;
    delete from public.platform_user_directory d where d.user_id = stale_user;
    insert into public.audit_log (tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata)
    values (null, app.current_user_id(), 'platform', 'directory.entry_reassigned', 'platform_user',
            p_user_id::text,
            jsonb_build_object('previous_user_id', stale_user, 'user_id', p_user_id,
                               'previous_user_memberships', stale_memberships));
  end if;

  select * into existing from public.platform_user_directory d where d.user_id = p_user_id for update;
  if not found then
    insert into public.platform_user_directory (user_id, email, full_name, invited_at)
    values (p_user_id, v_email, v_name, case when p_invited then now() end);
    insert into public.audit_log (tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata)
    values (null, app.current_user_id(), 'platform', 'directory.entry_added', 'platform_user',
            p_user_id::text, jsonb_build_object('user_id', p_user_id, 'invited', coalesce(p_invited, false)));
    return;
  end if;

  if existing.email is distinct from v_email then
    changed := array_append(changed, 'email');
  end if;
  if v_name is not null and existing.full_name is distinct from v_name then
    changed := array_append(changed, 'full_name');
  end if;
  if p_invited then
    changed := array_append(changed, 'invited_at');
  end if;
  if cardinality(changed) = 0 then
    return;
  end if;

  update public.platform_user_directory d
     set email = v_email,
         full_name = coalesce(v_name, d.full_name),
         invited_at = case when p_invited then now() else d.invited_at end
   where d.user_id = p_user_id;
  insert into public.audit_log (tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata)
  values (null, app.current_user_id(), 'platform', 'directory.entry_updated', 'platform_user',
          p_user_id::text, jsonb_build_object('user_id', p_user_id, 'changed', to_jsonb(changed)));
end;
$$;

-- ---------------------------------------------------------------------------
-- M4: accounts the platform knows about that have no membership
-- ---------------------------------------------------------------------------
create or replace function app.platform_list_unattached_accounts(p_search text default null)
returns table (
  user_id uuid,
  email text,
  full_name text,
  invited_at timestamptz,
  created_at timestamptz
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
  select d.user_id, d.email, d.full_name, d.invited_at, d.created_at
    from public.platform_user_directory d
   where not exists (select 1 from public.tenant_users tu where tu.user_id = d.user_id)
     and (needle is null
          or strpos(d.email, needle) > 0
          or strpos(lower(coalesce(d.full_name, '')), needle) > 0
          or strpos(d.user_id::text, needle) > 0)
   order by d.created_at desc, d.email;
end;
$$;

-- ---------------------------------------------------------------------------
-- M5: self-service password reset eligibility and cross-process throttle
-- ---------------------------------------------------------------------------
create table if not exists public.password_reset_requests (
  id          bigint generated always as identity primary key,
  email_hash  bytea not null,
  created_at  timestamptz not null default now()
);

comment on table public.password_reset_requests is
  'Self-service password-reset emails actually requested from Supabase (SHA-256 of the email only). Throttle state; rows older than a day are pruned.';

create index if not exists password_reset_requests_created_at_idx
  on public.password_reset_requests (created_at);
create index if not exists password_reset_requests_email_hash_idx
  on public.password_reset_requests (email_hash, created_at);

alter table public.password_reset_requests enable row level security;
alter table public.password_reset_requests force row level security;

-- The identity column owns a sequence; Supabase's default privileges grant client roles on
-- new sequences too (the 0041 lesson), so revoke both.
revoke all on public.password_reset_requests from public, anon, authenticated, app_backend;
revoke all on sequence public.password_reset_requests_id_seq from public, anon, authenticated, app_backend;
do $$
begin
  if exists (select 1 from pg_catalog.pg_roles where rolname = 'service_role') then
    execute 'revoke all on public.password_reset_requests from service_role';
    execute 'revoke all on sequence public.password_reset_requests_id_seq from service_role';
  end if;
end
$$;

grant select, insert, delete on public.password_reset_requests to saas_platform_provisioner;
drop policy if exists password_reset_requests_provisioner_all on public.password_reset_requests;
create policy password_reset_requests_provisioner_all on public.password_reset_requests
  for all to saas_platform_provisioner using (true) with check (true);

-- Returns true when the caller should ask Supabase to send a reset email. The answer is
-- the only output: it never reveals why a request was declined. Callers may lower the
-- caps (tests do) but never raise them above the built-in maxima.
create or replace function app.request_password_reset(
  p_email text,
  p_global_per_hour integer default null,
  p_per_email_per_hour integer default null
)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  v_email text := lower(btrim(coalesce(p_email, '')));
  v_hash bytea;
  v_user uuid;
  global_cap integer := least(coalesce(p_global_per_hour, 30), 30);
  email_cap integer := least(coalesce(p_per_email_per_hour, 3), 3);
begin
  if v_email = '' or length(v_email) > 320 then
    return false;
  end if;

  -- One request at a time across every application process, so the caps are exact.
  perform pg_advisory_xact_lock(hashtext('app.request_password_reset'));
  delete from public.password_reset_requests where created_at < now() - interval '1 day';

  select d.user_id into v_user
    from public.platform_user_directory d
   where d.email = v_email
     and exists (
       select 1
         from public.tenant_users tu
         join public.tenants t on t.id = tu.tenant_id
        where tu.user_id = d.user_id
          and tu.status = 'active'
          and t.status in ('draft', 'active'));
  if v_user is null then
    return false;
  end if;

  v_hash := sha256(convert_to(v_email, 'UTF8'));
  if (select count(*) from public.password_reset_requests r
       where r.created_at > now() - interval '1 hour') >= global_cap then
    return false;
  end if;
  if (select count(*) from public.password_reset_requests r
       where r.email_hash = v_hash and r.created_at > now() - interval '1 hour') >= email_cap then
    return false;
  end if;

  insert into public.password_reset_requests (email_hash) values (v_hash);
  insert into public.audit_log (tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata)
  values (null, null, 'system', 'auth.password_reset_requested', 'platform_user', v_user::text,
          jsonb_build_object('user_id', v_user));
  return true;
end;
$$;

-- ---------------------------------------------------------------------------
-- Ownership and privileges (0021 contract)
-- ---------------------------------------------------------------------------
grant create on schema app to saas_platform_provisioner;
alter function app.my_active_tenants() owner to saas_platform_provisioner;
alter function app.my_tenant_membership(uuid) owner to saas_platform_provisioner;
alter function app.platform_tenant_readiness(uuid) owner to saas_platform_provisioner;
alter function app.platform_assert_verified_owner_remains(uuid, uuid) owner to saas_platform_provisioner;
alter function app.platform_update_member(uuid, uuid, text, text) owner to saas_platform_provisioner;
alter function app.platform_remove_member(uuid, uuid) owner to saas_platform_provisioner;
alter function app.platform_upsert_user_directory(uuid, text, text, boolean) owner to saas_platform_provisioner;
alter function app.platform_list_unattached_accounts(text) owner to saas_platform_provisioner;
alter function app.request_password_reset(text, integer, integer) owner to saas_platform_provisioner;
revoke create on schema app from saas_platform_provisioner;

revoke all on function app.my_tenant_membership(uuid) from public;
revoke all on function app.platform_assert_verified_owner_remains(uuid, uuid) from public;
revoke all on function app.platform_list_unattached_accounts(text) from public;
revoke all on function app.request_password_reset(text, integer, integer) from public;

revoke execute on function app.my_tenant_membership(uuid) from anon, authenticated;
revoke execute on function app.platform_assert_verified_owner_remains(uuid, uuid) from app_backend, authenticated, anon;
revoke execute on function app.platform_list_unattached_accounts(text) from app_backend, authenticated, anon;
revoke execute on function app.request_password_reset(text, integer, integer) from authenticated, anon;

do $$
begin
  if exists (select 1 from pg_catalog.pg_roles where rolname = 'service_role') then
    execute 'revoke execute on function app.my_tenant_membership(uuid) from service_role';
    execute 'revoke execute on function app.platform_assert_verified_owner_remains(uuid, uuid) from service_role';
    execute 'revoke execute on function app.platform_list_unattached_accounts(text) from service_role';
    execute 'revoke execute on function app.request_password_reset(text, integer, integer) from service_role';
  end if;
end
$$;

grant execute on function app.my_tenant_membership(uuid) to app_backend;
grant execute on function app.request_password_reset(text, integer, integer) to app_backend;
grant execute on function app.platform_list_unattached_accounts(text) to saas_platform_admin;
