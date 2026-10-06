-- ============================================================================
-- Phase 3 / 0021 -- platform provisioning, activation and read-only platform views
-- ============================================================================
-- Every function here:
--   * is SECURITY DEFINER, owned by the NOLOGIN saas_platform_provisioner role,
--   * calls app.require_platform_admin() first (database-enforced authorization),
--   * is executable ONLY by saas_platform_admin,
--   * pins search_path and uses no dynamic SQL.

grant select on public.tenant_settings, public.subscriptions, public.products,
  public.product_variants, public.audit_log to saas_platform_provisioner;

drop policy if exists tenant_settings_platform_provisioner_select on public.tenant_settings;
create policy tenant_settings_platform_provisioner_select on public.tenant_settings
  for select to saas_platform_provisioner using (true);

drop policy if exists subscriptions_platform_provisioner_select on public.subscriptions;
create policy subscriptions_platform_provisioner_select on public.subscriptions
  for select to saas_platform_provisioner using (true);

drop policy if exists products_platform_provisioner_select on public.products;
create policy products_platform_provisioner_select on public.products
  for select to saas_platform_provisioner using (true);

drop policy if exists product_variants_platform_provisioner_select on public.product_variants;
create policy product_variants_platform_provisioner_select on public.product_variants
  for select to saas_platform_provisioner using (true);

drop policy if exists audit_log_platform_provisioner_select on public.audit_log;
create policy audit_log_platform_provisioner_select on public.audit_log
  for select to saas_platform_provisioner using (true);

grant create on schema app to saas_platform_provisioner;
grant execute on function app.current_user_id() to saas_platform_provisioner;

-- ---------------------------------------------------------------------------
-- Provisioning: tenant (draft) + settings + optional owner + optional subscription + audit
-- ---------------------------------------------------------------------------
create or replace function app.platform_provision_tenant(
  p_name text,
  p_slug text,
  p_timezone text default 'Africa/Nairobi',
  p_currency text default 'KES',
  p_owner_user_id uuid default null,
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
  perform app.require_platform_admin();

  insert into public.tenants (id, name, slug, status)
  values (new_tenant_id, p_name, p_slug, 'draft');

  insert into public.tenant_settings (tenant_id, timezone, currency)
  values (new_tenant_id, p_timezone, p_currency);

  if p_owner_user_id is not null then
    insert into public.tenant_users (tenant_id, user_id, role, status)
    values (new_tenant_id, p_owner_user_id, 'owner', 'active');
  end if;

  if p_initial_plan_id is not null then
    if not exists (
      select 1 from public.plans p where p.id = p_initial_plan_id and p.is_active
    ) then
      raise exception using
        errcode = '23503',
        message = 'initial subscription plan must exist and be active';
    end if;
    insert into public.subscriptions (tenant_id, plan_id, status, billing_method)
    values (new_tenant_id, p_initial_plan_id, 'trial', 'manual');
  end if;

  insert into public.audit_log (
    tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata
  )
  values (
    new_tenant_id,
    app.current_user_id(),
    'platform',
    'tenant.provisioned',
    'tenant',
    new_tenant_id::text,
    jsonb_build_object(
      'slug', p_slug,
      'owner_user_id', p_owner_user_id,
      'initial_plan_id', p_initial_plan_id
    )
  );

  return new_tenant_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- Readiness: what is still missing before a tenant can be activated
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
  select exists (
    select 1 from public.tenant_users u
     where u.tenant_id = p_tenant_id and u.role = 'owner' and u.status = 'active'
  ) into has_owner;
  select exists (
    select 1 from public.tenant_channels c where c.tenant_id = p_tenant_id
  ) into has_channel;

  return jsonb_build_object(
    'checks', jsonb_build_array(
      jsonb_build_object('key', 'settings', 'label', 'Business profile and rules saved', 'ok', has_settings, 'required', true),
      jsonb_build_object('key', 'catalogue', 'label', 'At least one active product with an active variant', 'ok', has_catalogue, 'required', true),
      jsonb_build_object('key', 'owner', 'label', 'An active owner is assigned', 'ok', has_owner, 'required', true),
      jsonb_build_object('key', 'channel', 'label', 'A WhatsApp channel is recorded', 'ok', has_channel, 'required', false)
    )
  );
end;
$$;

-- ---------------------------------------------------------------------------
-- Status changes (activation requires readiness; an owner may be waived explicitly)
-- ---------------------------------------------------------------------------
create or replace function app.platform_set_tenant_status(
  p_tenant_id uuid,
  p_status text,
  p_waive_owner boolean default false
)
returns text
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  previous_status text;
  readiness jsonb;
  failing text;
begin
  perform app.require_platform_admin();

  if p_status not in ('active', 'suspended', 'inactive') then
    raise exception using errcode = '22023', message = 'status must be active, suspended or inactive';
  end if;

  select t.status into previous_status from public.tenants t where t.id = p_tenant_id for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'tenant not found';
  end if;

  if p_status = 'active' then
    readiness := app.platform_tenant_readiness(p_tenant_id);
    select string_agg(c ->> 'key', ', ')
      into failing
      from jsonb_array_elements(readiness -> 'checks') c
     where (c ->> 'required')::boolean
       and not (c ->> 'ok')::boolean
       and not (c ->> 'key' = 'owner' and p_waive_owner);
    if failing is not null then
      raise exception using
        errcode = '23514',
        message = 'tenant is not ready to activate: ' || failing;
    end if;
  end if;

  update public.tenants set status = p_status where id = p_tenant_id;

  insert into public.audit_log (
    tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata
  )
  values (
    p_tenant_id,
    app.current_user_id(),
    'platform',
    'tenant.status_changed',
    'tenant',
    p_tenant_id::text,
    jsonb_build_object('from', previous_status, 'to', p_status, 'owner_waived', p_waive_owner)
  );

  return p_status;
end;
$$;

-- ---------------------------------------------------------------------------
-- Read-only platform views
-- ---------------------------------------------------------------------------
create or replace function app.platform_list_tenants(
  p_tenant_id uuid default null,
  p_status text default null
)
returns table (
  id uuid,
  name text,
  slug text,
  status text,
  created_at timestamptz,
  updated_at timestamptz,
  product_count bigint,
  active_variant_count bigint,
  owner_count bigint,
  channel_status text,
  subscription_status text
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
begin
  perform app.require_platform_admin();

  return query
  select t.id, t.name, t.slug, t.status, t.created_at, t.updated_at,
         (select count(*) from public.products p where p.tenant_id = t.id),
         (select count(*) from public.product_variants v where v.tenant_id = t.id and v.is_active),
         (select count(*) from public.tenant_users u
           where u.tenant_id = t.id and u.role = 'owner' and u.status = 'active'),
         (select c.status from public.tenant_channels c
           where c.tenant_id = t.id order by c.is_default desc, c.created_at limit 1),
         (select s.status from public.subscriptions s
           where s.tenant_id = t.id
             and s.status in ('trial', 'active', 'past_due', 'suspended') limit 1)
    from public.tenants t
   where (p_tenant_id is null or t.id = p_tenant_id)
     and (p_status is null or t.status = p_status)
   order by t.created_at desc, t.name;
end;
$$;

create or replace function app.platform_overview()
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
begin
  perform app.require_platform_admin();

  return jsonb_build_object(
    'tenants_total', (select count(*) from public.tenants),
    'tenants_by_status', coalesce(
      (select jsonb_object_agg(status, n) from (
         select status, count(*) as n from public.tenants group by status) s),
      '{}'::jsonb),
    'products_total', (select count(*) from public.products),
    'products_active', (select count(*) from public.products where is_active),
    'variants_total', (select count(*) from public.product_variants),
    'variants_active', (select count(*) from public.product_variants where is_active),
    'subscriptions_by_status', coalesce(
      (select jsonb_object_agg(status, n) from (
         select status, count(*) as n from public.subscriptions group by status) s),
      '{}'::jsonb)
  );
end;
$$;

create or replace function app.platform_recent_audit(
  p_tenant_id uuid default null,
  p_limit integer default 50
)
returns table (
  id bigint,
  tenant_id uuid,
  tenant_name text,
  actor_user_id uuid,
  actor_type text,
  action text,
  entity_type text,
  entity_id text,
  metadata jsonb,
  created_at timestamptz
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
begin
  perform app.require_platform_admin();

  return query
  select a.id, a.tenant_id, t.name, a.actor_user_id, a.actor_type, a.action,
         a.entity_type, a.entity_id, a.metadata, a.created_at
    from public.audit_log a
    left join public.tenants t on t.id = a.tenant_id
   where (p_tenant_id is null or a.tenant_id = p_tenant_id)
   order by a.created_at desc, a.id desc
   limit greatest(1, least(coalesce(p_limit, 50), 200));
end;
$$;

-- ---------------------------------------------------------------------------
-- Ownership and privileges: platform API role only
-- ---------------------------------------------------------------------------
create or replace function app.platform_list_plans()
returns table (id uuid, code text, name text, monthly_price numeric, currency text)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
begin
  perform app.require_platform_admin();

  return query
  select p.id, p.code, p.name, p.monthly_price, p.currency
    from public.plans p
   where p.is_active
   order by p.monthly_price, p.code;
end;
$$;

alter function app.platform_list_plans() owner to saas_platform_provisioner;
revoke all on function app.platform_list_plans() from public;
revoke execute on function app.platform_list_plans() from app_backend, authenticated, anon;
grant execute on function app.platform_list_plans() to saas_platform_admin;
alter function app.platform_provision_tenant(text, text, text, text, uuid, uuid)
  owner to saas_platform_provisioner;
alter function app.platform_tenant_readiness(uuid) owner to saas_platform_provisioner;
alter function app.platform_set_tenant_status(uuid, text, boolean) owner to saas_platform_provisioner;
alter function app.platform_list_tenants(uuid, text) owner to saas_platform_provisioner;
alter function app.platform_overview() owner to saas_platform_provisioner;
alter function app.platform_recent_audit(uuid, integer) owner to saas_platform_provisioner;

revoke create on schema app from saas_platform_provisioner;

revoke all on function app.platform_provision_tenant(text, text, text, text, uuid, uuid) from public;
revoke all on function app.platform_tenant_readiness(uuid) from public;
revoke all on function app.platform_set_tenant_status(uuid, text, boolean) from public;
revoke all on function app.platform_list_tenants(uuid, text) from public;
revoke all on function app.platform_overview() from public;
revoke all on function app.platform_recent_audit(uuid, integer) from public;

revoke execute on function app.platform_provision_tenant(text, text, text, text, uuid, uuid)
  from app_backend, authenticated, anon;
revoke execute on function app.platform_tenant_readiness(uuid) from app_backend, authenticated, anon;
revoke execute on function app.platform_set_tenant_status(uuid, text, boolean)
  from app_backend, authenticated, anon;
revoke execute on function app.platform_list_tenants(uuid, text) from app_backend, authenticated, anon;
revoke execute on function app.platform_overview() from app_backend, authenticated, anon;
revoke execute on function app.platform_recent_audit(uuid, integer) from app_backend, authenticated, anon;

grant execute on function app.platform_provision_tenant(text, text, text, text, uuid, uuid)
  to saas_platform_admin;
grant execute on function app.platform_tenant_readiness(uuid) to saas_platform_admin, saas_platform_provisioner;
grant execute on function app.platform_set_tenant_status(uuid, text, boolean) to saas_platform_admin;
grant execute on function app.platform_list_tenants(uuid, text) to saas_platform_admin;
grant execute on function app.platform_overview() to saas_platform_admin;
grant execute on function app.platform_recent_audit(uuid, integer) to saas_platform_admin;

-- The unguarded 0013 provisioner (optional owner impossible, no draft, no actor) is retired.
drop function if exists app.provision_tenant(text, text, uuid, text, text, uuid);
