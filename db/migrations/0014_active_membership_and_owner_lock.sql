-- ============================================================================
-- Phase 1 / 0014 -- active membership authorization and owner-removal locking
-- ============================================================================

create or replace function app.is_tenant_member(target_tenant_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
  select exists (
    select 1
      from public.tenant_users tu
     where tu.tenant_id = target_tenant_id
       and tu.user_id = app.current_user_id()
       and tu.status = 'active'
  );
$$;

comment on function app.is_tenant_member(uuid) is
  'True only when the authenticated user has an active membership in the tenant.';

create or replace function app.is_tenant_owner(target_tenant_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
  select exists (
    select 1
      from public.tenant_users tu
     where tu.tenant_id = target_tenant_id
       and tu.user_id = app.current_user_id()
       and tu.role = 'owner'
       and tu.status = 'active'
  );
$$;

comment on function app.is_tenant_owner(uuid) is
  'True only when the authenticated user is an active owner of the tenant.';

create or replace function app.prevent_final_active_owner_removal()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
  remaining_active_owners integer;
begin
  if pg_trigger_depth() > 1
     or old.role <> 'owner'
     or old.status <> 'active' then
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

  -- Serializes concurrent demotions/deletions for one tenant without granting this
  -- trigger function write access to the tenants table.
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(old.tenant_id::text, 0)
  );

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

alter function app.prevent_final_active_owner_removal() owner to saas_platform_provisioner;
revoke all on function app.prevent_final_active_owner_removal() from public;