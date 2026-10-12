-- ============================================================================
-- Phase 3 / 0020 -- draft tenant state, channel guards, resolver status check
-- ============================================================================
-- A tenant is created as `draft`, configured, and then activated by a platform
-- function that checks readiness. Draft tenants must never receive traffic.

alter table public.tenants drop constraint if exists tenants_status_check;
alter table public.tenants
  add constraint tenants_status_check
  check (status in ('draft', 'active', 'suspended', 'inactive'));

-- Fail closed: a row inserted without an explicit status is a draft.
alter table public.tenants alter column status set default 'draft';

comment on column public.tenants.status is
  'draft | active | suspended | inactive. draft is the initial onboarding state and is never routed.';

-- Provisioner-owned functions below read and update tenant rows. Every policy is
-- limited to the NOLOGIN provisioner role, which is reachable only through
-- SECURITY DEFINER functions.
drop policy if exists tenants_platform_provisioner_update on public.tenants;
create policy tenants_platform_provisioner_update on public.tenants
  for update to saas_platform_provisioner using (true) with check (true);

drop policy if exists tenant_channels_platform_provisioner_select on public.tenant_channels;
create policy tenant_channels_platform_provisioner_select on public.tenant_channels
  for select to saas_platform_provisioner using (true);

grant update (status) on public.tenants to saas_platform_provisioner;
grant select on public.tenant_channels to saas_platform_provisioner;

-- Tenants (and their owners) may rename the business, but not change status or slug.
-- Status transitions are platform actions (app.platform_set_tenant_status).
revoke update on public.tenants from app_backend, authenticated;
grant update (name) on public.tenants to app_backend, authenticated;

-- A tenant can never go back to draft once it has left it.
create or replace function app.tenant_status_transition_guard()
returns trigger
language plpgsql
set search_path = pg_catalog, public, pg_temp
as $$
begin
  if new.status = 'draft' and old.status <> 'draft' then
    raise exception using errcode = '23514', message = 'a tenant cannot return to draft';
  end if;
  return new;
end;
$$;

drop trigger if exists tenants_status_transition_guard on public.tenants;
create trigger tenants_status_transition_guard
  before update of status on public.tenants
  for each row execute function app.tenant_status_transition_guard();

-- An active channel requires an active tenant. SECURITY DEFINER because the writer
-- (app_backend) may not be able to read the tenant row of a different context.
create or replace function app.tenant_channel_requires_active_tenant()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
  owning_status text;
begin
  if new.status = 'active' then
    select t.status into owning_status from public.tenants t where t.id = new.tenant_id;
    if owning_status is distinct from 'active' then
      raise exception using
        errcode = '23514',
        message = 'an active channel requires an active tenant';
    end if;
  end if;
  return new;
end;
$$;

grant create on schema app to saas_platform_provisioner;
alter function app.tenant_channel_requires_active_tenant() owner to saas_platform_provisioner;
revoke all on function app.tenant_channel_requires_active_tenant() from public;

drop trigger if exists tenant_channels_require_active_tenant on public.tenant_channels;
create trigger tenant_channels_require_active_tenant
  before insert or update of status, tenant_id on public.tenant_channels
  for each row execute function app.tenant_channel_requires_active_tenant();

-- Routing identifies the tenant for an inbound number; a draft, suspended or inactive
-- tenant must not resolve. The function is re-owned by the provisioner role because it
-- must read FORCE-RLS public.tenants (the provisioner has a select policy for that).
create or replace function app.resolve_tenant_by_phone_number_id(target_phone_number_id text)
returns uuid
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
  select tc.tenant_id
    from public.tenant_channels tc
    join public.tenants t on t.id = tc.tenant_id
   where tc.phone_number_id = target_phone_number_id
     and tc.channel_type = 'whatsapp'
     and tc.status = 'active'
     and t.status = 'active'
   limit 1;
$$;

alter function app.resolve_tenant_by_phone_number_id(text) owner to saas_platform_provisioner;
revoke create on schema app from saas_platform_provisioner;
revoke all on function app.resolve_tenant_by_phone_number_id(text) from public;
revoke execute on function app.resolve_tenant_by_phone_number_id(text) from authenticated, anon;
grant execute on function app.resolve_tenant_by_phone_number_id(text) to app_backend;

comment on function app.resolve_tenant_by_phone_number_id(text) is
  'Inbound WhatsApp routing key -> tenant id. NULL when the number is unknown/disabled or the tenant is not active.';
