-- ============================================================================
-- Phase 3 / 0023 -- WhatsApp channels are written only by platform functions
-- ============================================================================
-- Channels are routing keys: whoever binds a phone_number_id to a tenant decides where
-- inbound traffic goes. So no tenant-facing role may write them:
--   * app_backend and authenticated keep SELECT only (RLS still confines it to the tenant);
--   * every write goes through the app.platform_* functions below, which verify the
--     platform administrator, validate the identifiers and write an audit event
--     attributed to the verified administrator.
-- Only non-secret metadata is stored; credentials_ref stays NULL on this path.

-- ---------------------------------------------------------------------------
-- 1. Remove tenant-facing write access
-- ---------------------------------------------------------------------------
revoke insert, update, delete on public.tenant_channels from app_backend, authenticated;

drop policy if exists tenant_channels_backend_all on public.tenant_channels;
drop policy if exists tenant_channels_owner_insert on public.tenant_channels;
drop policy if exists tenant_channels_owner_update on public.tenant_channels;
drop policy if exists tenant_channels_owner_delete on public.tenant_channels;

drop policy if exists tenant_channels_backend_select on public.tenant_channels;
create policy tenant_channels_backend_select on public.tenant_channels
  for select to app_backend
  using (tenant_id = app.current_tenant_id());

-- ---------------------------------------------------------------------------
-- 2. The provisioner (function owner) may write channels
-- ---------------------------------------------------------------------------
grant insert on public.tenant_channels to saas_platform_provisioner;
grant update (phone_number_id, waba_id, display_phone_number, status, is_default)
  on public.tenant_channels to saas_platform_provisioner;

drop policy if exists tenant_channels_platform_provisioner_insert on public.tenant_channels;
create policy tenant_channels_platform_provisioner_insert on public.tenant_channels
  for insert to saas_platform_provisioner with check (true);

drop policy if exists tenant_channels_platform_provisioner_update on public.tenant_channels;
create policy tenant_channels_platform_provisioner_update on public.tenant_channels
  for update to saas_platform_provisioner using (true) with check (true);

-- ---------------------------------------------------------------------------
-- 3. Reserved phone numbers (e.g. numbers that belong to live deployments)
-- ---------------------------------------------------------------------------
-- Maintained by an operator. No application role can read or write it; the trigger below
-- refuses to bind a reserved number for ANY writer.
create table if not exists public.reserved_phone_numbers (
  phone_number_id text primary key,
  reason          text,
  created_at      timestamptz not null default now()
);

alter table public.reserved_phone_numbers enable row level security;
revoke all on public.reserved_phone_numbers from public, anon, authenticated, app_backend, saas_platform_admin;

grant select on public.reserved_phone_numbers to saas_platform_provisioner;
drop policy if exists reserved_phone_numbers_provisioner_select on public.reserved_phone_numbers;
create policy reserved_phone_numbers_provisioner_select on public.reserved_phone_numbers
  for select to saas_platform_provisioner using (true);

comment on table public.reserved_phone_numbers is
  'phone_number_ids that may never be bound to a tenant channel (operator-maintained).';

grant create on schema app to saas_platform_provisioner;

create or replace function app.tenant_channel_binding_guard()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
  check_reserved boolean := true;
begin
  if tg_op = 'UPDATE' then
    check_reserved := new.phone_number_id is distinct from old.phone_number_id;
    if new.tenant_id is distinct from old.tenant_id
       or new.channel_type is distinct from old.channel_type then
      raise exception using
        errcode = '23514',
        message = 'a channel cannot be moved to another tenant';
    end if;
    if old.status = 'active'
       and new.status = 'active'
       and (new.phone_number_id is distinct from old.phone_number_id
            or new.waba_id is distinct from old.waba_id) then
      raise exception using
        errcode = '23514',
        message = 'disable the channel before changing its identifiers';
    end if;
  end if;

  if check_reserved
     and exists (
       select 1 from public.reserved_phone_numbers r
        where r.phone_number_id = new.phone_number_id
     ) then
    raise exception using
      errcode = '23514',
      message = 'this phone number is reserved and cannot be bound to a tenant';
  end if;

  return new;
end;
$$;

alter function app.tenant_channel_binding_guard() owner to saas_platform_provisioner;
revoke all on function app.tenant_channel_binding_guard() from public;

drop trigger if exists tenant_channels_binding_guard on public.tenant_channels;
create trigger tenant_channels_binding_guard
  before insert or update on public.tenant_channels
  for each row execute function app.tenant_channel_binding_guard();

-- ---------------------------------------------------------------------------
-- 4. Platform functions: create, edit, change status
-- ---------------------------------------------------------------------------
create or replace function app.platform_create_channel(
  p_tenant_id uuid,
  p_phone_number_id text,
  p_waba_id text default null,
  p_display_phone_number text default null
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  new_channel_id uuid;
  tenant_has_channel boolean;
begin
  perform app.require_platform_admin();

  if not exists (select 1 from public.tenants t where t.id = p_tenant_id) then
    raise exception using errcode = 'P0002', message = 'tenant not found';
  end if;
  if p_phone_number_id is null or p_phone_number_id !~ '^[A-Za-z0-9_-]{3,64}$' then
    raise exception using errcode = '22023', message = 'invalid phone number id';
  end if;
  if p_waba_id is not null and p_waba_id !~ '^[A-Za-z0-9_-]{1,64}$' then
    raise exception using errcode = '22023', message = 'invalid WABA id';
  end if;
  if p_display_phone_number is not null and p_display_phone_number !~ '^\+[1-9][0-9]{6,14}$' then
    raise exception using errcode = '22023', message = 'display phone number must be E.164';
  end if;

  select exists (select 1 from public.tenant_channels c where c.tenant_id = p_tenant_id)
    into tenant_has_channel;

  insert into public.tenant_channels (
    tenant_id, channel_type, phone_number_id, waba_id, display_phone_number,
    status, is_default, credentials_ref
  )
  values (
    p_tenant_id, 'whatsapp', p_phone_number_id, p_waba_id, p_display_phone_number,
    'pending', not tenant_has_channel, null
  )
  returning id into new_channel_id;

  insert into public.audit_log (
    tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata
  )
  values (
    p_tenant_id, app.current_user_id(), 'platform', 'channel.created', 'tenant_channel',
    new_channel_id::text,
    jsonb_build_object('phone_number_id', p_phone_number_id, 'waba_id', p_waba_id, 'status', 'pending')
  );

  return new_channel_id;
end;
$$;

create or replace function app.platform_update_channel(
  p_channel_id uuid,
  p_phone_number_id text,
  p_waba_id text default null,
  p_display_phone_number text default null
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  existing public.tenant_channels%rowtype;
begin
  perform app.require_platform_admin();

  select * into existing from public.tenant_channels c where c.id = p_channel_id for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'channel not found';
  end if;
  if existing.status = 'active' then
    raise exception using
      errcode = '23514',
      message = 'disable the channel before changing its identifiers';
  end if;
  if p_phone_number_id is null or p_phone_number_id !~ '^[A-Za-z0-9_-]{3,64}$' then
    raise exception using errcode = '22023', message = 'invalid phone number id';
  end if;
  if p_waba_id is not null and p_waba_id !~ '^[A-Za-z0-9_-]{1,64}$' then
    raise exception using errcode = '22023', message = 'invalid WABA id';
  end if;
  if p_display_phone_number is not null and p_display_phone_number !~ '^\+[1-9][0-9]{6,14}$' then
    raise exception using errcode = '22023', message = 'display phone number must be E.164';
  end if;

  update public.tenant_channels
     set phone_number_id = p_phone_number_id,
         waba_id = p_waba_id,
         display_phone_number = p_display_phone_number
   where id = p_channel_id;

  insert into public.audit_log (
    tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata
  )
  values (
    existing.tenant_id, app.current_user_id(), 'platform', 'channel.updated', 'tenant_channel',
    p_channel_id::text,
    jsonb_build_object(
      'old_phone_number_id', existing.phone_number_id, 'phone_number_id', p_phone_number_id,
      'old_waba_id', existing.waba_id, 'waba_id', p_waba_id
    )
  );
end;
$$;

create or replace function app.platform_set_channel_status(p_channel_id uuid, p_status text)
returns text
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  existing public.tenant_channels%rowtype;
begin
  perform app.require_platform_admin();

  if p_status is null or p_status not in ('pending', 'active', 'disabled') then
    raise exception using errcode = '22023', message = 'status must be pending, active or disabled';
  end if;

  select * into existing from public.tenant_channels c where c.id = p_channel_id for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'channel not found';
  end if;

  -- An active channel on a non-active tenant is also refused by tenant_channel_requires_active_tenant.
  update public.tenant_channels set status = p_status where id = p_channel_id;

  insert into public.audit_log (
    tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata
  )
  values (
    existing.tenant_id, app.current_user_id(), 'platform', 'channel.status_changed',
    'tenant_channel', p_channel_id::text,
    jsonb_build_object('from', existing.status, 'to', p_status)
  );

  return p_status;
end;
$$;

alter function app.platform_create_channel(uuid, text, text, text) owner to saas_platform_provisioner;
alter function app.platform_update_channel(uuid, text, text, text) owner to saas_platform_provisioner;
alter function app.platform_set_channel_status(uuid, text) owner to saas_platform_provisioner;

revoke create on schema app from saas_platform_provisioner;

revoke all on function app.platform_create_channel(uuid, text, text, text) from public;
revoke all on function app.platform_update_channel(uuid, text, text, text) from public;
revoke all on function app.platform_set_channel_status(uuid, text) from public;
revoke execute on function app.platform_create_channel(uuid, text, text, text) from app_backend, authenticated, anon;
revoke execute on function app.platform_update_channel(uuid, text, text, text) from app_backend, authenticated, anon;
revoke execute on function app.platform_set_channel_status(uuid, text) from app_backend, authenticated, anon;
grant execute on function app.platform_create_channel(uuid, text, text, text) to saas_platform_admin;
grant execute on function app.platform_update_channel(uuid, text, text, text) to saas_platform_admin;
grant execute on function app.platform_set_channel_status(uuid, text) to saas_platform_admin;
