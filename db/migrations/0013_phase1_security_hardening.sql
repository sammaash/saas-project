-- ============================================================================
-- Phase 1 / 0013 -- role hardening, tenant provisioning, owner protection
-- ============================================================================

do $$
begin
  if not exists (select 1 from pg_catalog.pg_roles where rolname = 'saas_platform_admin') then
    create role saas_platform_admin nologin noinherit nobypassrls;
  end if;
  if not exists (select 1 from pg_catalog.pg_roles where rolname = 'saas_platform_provisioner') then
    create role saas_platform_provisioner nologin noinherit nobypassrls;
  end if;
end
$$;

alter role saas_platform_admin nologin noinherit nobypassrls;
alter role saas_platform_provisioner nologin noinherit nobypassrls;

create index if not exists subscriptions_plan_idx
  on public.subscriptions (plan_id);

alter default privileges in schema public
  grant usage, select on sequences to app_backend;

-- The backend resolves inactive plans for historical subscriptions. Dashboard users
-- receive only active plans; the platform bootstrap capability may validate a plan.
drop policy if exists plans_read_active on public.plans;
drop policy if exists plans_backend_select on public.plans;
drop policy if exists plans_platform_provisioner_select on public.plans;

create policy plans_backend_select on public.plans
  for select to app_backend using (true);

create policy plans_read_active on public.plans
  for select to authenticated using (is_active);

create policy plans_platform_provisioner_select on public.plans
  for select to saas_platform_provisioner using (true);

-- A client-facing role cannot insert a tenant or invoke the platform function.
drop policy if exists tenants_platform_provisioner_insert on public.tenants;
create policy tenants_platform_provisioner_insert on public.tenants
  for insert to saas_platform_provisioner with check (true);

drop policy if exists tenant_users_platform_provisioner_select on public.tenant_users;
drop policy if exists tenant_users_platform_provisioner_insert on public.tenant_users;
create policy tenant_users_platform_provisioner_select on public.tenant_users
  for select to saas_platform_provisioner using (true);
create policy tenant_users_platform_provisioner_insert on public.tenant_users
  for insert to saas_platform_provisioner with check (true);

drop policy if exists tenants_platform_provisioner_select on public.tenants;
create policy tenants_platform_provisioner_select on public.tenants
  for select to saas_platform_provisioner using (true);

drop policy if exists tenant_settings_platform_provisioner_insert on public.tenant_settings;
create policy tenant_settings_platform_provisioner_insert on public.tenant_settings
  for insert to saas_platform_provisioner with check (true);

drop policy if exists subscriptions_platform_provisioner_insert on public.subscriptions;
create policy subscriptions_platform_provisioner_insert on public.subscriptions
  for insert to saas_platform_provisioner with check (true);

drop policy if exists audit_log_platform_provisioner_insert on public.audit_log;
create policy audit_log_platform_provisioner_insert on public.audit_log
  for insert to saas_platform_provisioner with check (tenant_id is not null);

grant usage on schema app to saas_platform_admin, saas_platform_provisioner;
grant select on public.tenants, public.tenant_users, public.plans to saas_platform_provisioner;
grant insert on public.tenants, public.tenant_users, public.tenant_settings,
  public.subscriptions, public.audit_log to saas_platform_provisioner;
grant usage, select on all sequences in schema public to saas_platform_provisioner;

create or replace function app.prevent_final_active_owner_removal()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
  remaining_active_owners integer;
begin
  if old.role <> 'owner' or old.status <> 'active' then
    if tg_op = 'DELETE' then
      return old;
    end if;
    return new;
  end if;

  if tg_op = 'UPDATE'
     and new.tenant_id = old.tenant_id
     and new.role = 'owner'
     and new.status = 'active' then
    return new;
  end if;

  perform 1
    from public.tenants t
   where t.id = old.tenant_id
   for update;

  -- A tenant deletion cascades membership deletion; it is not an owner removal.
  if not found then
    if tg_op = 'DELETE' then
      return old;
    end if;
    return new;
  end if;

  select count(*)
    into remaining_active_owners
    from public.tenant_users tu
   where tu.tenant_id = old.tenant_id
     and tu.id <> old.id
     and tu.role = 'owner'
     and tu.status = 'active';

  if remaining_active_owners = 0 then
    raise exception using
      errcode = '23514',
      message = 'tenant must retain at least one active owner';
  end if;

  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

grant create on schema app to saas_platform_provisioner;
alter function app.prevent_final_active_owner_removal() owner to saas_platform_provisioner;
revoke create on schema app from saas_platform_provisioner;
revoke all on function app.prevent_final_active_owner_removal() from public;

drop trigger if exists tenant_users_prevent_final_active_owner_removal on public.tenant_users;
create trigger tenant_users_prevent_final_active_owner_removal
  before update of tenant_id, role, status or delete on public.tenant_users
  for each row execute function app.prevent_final_active_owner_removal();

create or replace function app.provision_tenant(
  p_name text,
  p_slug text,
  p_owner_user_id uuid,
  p_timezone text default 'Africa/Nairobi',
  p_currency text default 'KES',
  p_initial_plan_id uuid default null
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  new_tenant_id uuid := gen_random_uuid();
begin
  if p_owner_user_id is null then
    raise exception using errcode = '22004', message = 'owner user id is required';
  end if;

  insert into public.tenants (id, name, slug)
  values (new_tenant_id, p_name, p_slug);

  insert into public.tenant_users (tenant_id, user_id, role, status)
  values (new_tenant_id, p_owner_user_id, 'owner', 'active');

  insert into public.tenant_settings (tenant_id, timezone, currency)
  values (new_tenant_id, p_timezone, p_currency);

  if p_initial_plan_id is not null then
    if not exists (
      select 1
        from public.plans p
       where p.id = p_initial_plan_id
         and p.is_active
    ) then
      raise exception using
        errcode = '23503',
        message = 'initial subscription plan must exist and be active';
    end if;

    insert into public.subscriptions (tenant_id, plan_id, status, billing_method)
    values (new_tenant_id, p_initial_plan_id, 'trial', 'manual');
  end if;

  insert into public.audit_log (
    tenant_id, actor_type, action, entity_type, entity_id, metadata
  )
  values (
    new_tenant_id,
    'platform',
    'tenant.provisioned',
    'tenant',
    new_tenant_id::text,
    jsonb_build_object('owner_user_id', p_owner_user_id)
  );

  return new_tenant_id;
end;
$$;

grant create on schema app to saas_platform_provisioner;
alter function app.provision_tenant(text, text, uuid, text, text, uuid)
  owner to saas_platform_provisioner;
revoke create on schema app from saas_platform_provisioner;
revoke all on function app.provision_tenant(text, text, uuid, text, text, uuid) from public;
revoke execute on function app.provision_tenant(text, text, uuid, text, text, uuid)
  from app_backend, authenticated, anon;
grant execute on function app.provision_tenant(text, text, uuid, text, text, uuid)
  to saas_platform_admin;

comment on function app.provision_tenant(text, text, uuid, text, text, uuid) is
  'Atomically creates a tenant, active initial owner, settings, optional initial subscription, and audit event. Execute only from a trusted platform API using saas_platform_admin.';

alter default privileges in schema public
  grant usage, select on sequences to saas_platform_provisioner;