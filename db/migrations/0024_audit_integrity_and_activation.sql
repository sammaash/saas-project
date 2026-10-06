-- ============================================================================
-- Phase 3 / 0024 -- audit integrity and activation hardening
-- ============================================================================
-- Platform-attributed audit events (actor_type = 'platform') may only be written by the
-- app.platform_* functions, using the verified administrator identity. The tenant runtime
-- (app_backend) can append 'system' events only.

drop policy if exists audit_log_backend_insert on public.audit_log;
create policy audit_log_backend_insert on public.audit_log
  for insert to app_backend
  with check (tenant_id = app.current_tenant_id() and actor_type = 'system');

grant create on schema app to saas_platform_provisioner;

create or replace function app.platform_record_audit(
  p_tenant_id uuid,
  p_action text,
  p_entity_type text default null,
  p_entity_id text default null,
  p_metadata jsonb default '{}'::jsonb
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
begin
  perform app.require_platform_admin();

  if not exists (select 1 from public.tenants t where t.id = p_tenant_id) then
    raise exception using errcode = 'P0002', message = 'tenant not found';
  end if;

  insert into public.audit_log (
    tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata
  )
  values (
    p_tenant_id, app.current_user_id(), 'platform', p_action, p_entity_type, p_entity_id,
    coalesce(p_metadata, '{}'::jsonb)
  );
end;
$$;

alter function app.platform_record_audit(uuid, text, text, text, jsonb) owner to saas_platform_provisioner;
revoke all on function app.platform_record_audit(uuid, text, text, text, jsonb) from public;
revoke execute on function app.platform_record_audit(uuid, text, text, text, jsonb)
  from app_backend, authenticated, anon;
grant execute on function app.platform_record_audit(uuid, text, text, text, jsonb) to saas_platform_admin;

-- A NULL waiver is "no waiver", never a bypass.
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
  waive boolean := coalesce(p_waive_owner, false);
begin
  perform app.require_platform_admin();

  if p_status is null or p_status not in ('active', 'suspended', 'inactive') then
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
       and not (c ->> 'key' = 'owner' and waive);
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
    jsonb_build_object('from', previous_status, 'to', p_status, 'owner_waived', waive)
  );

  return p_status;
end;
$$;

revoke create on schema app from saas_platform_provisioner;
