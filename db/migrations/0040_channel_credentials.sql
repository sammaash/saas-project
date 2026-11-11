-- ============================================================================
-- Phase 1a / 0040 -- channel_credentials (per-channel encrypted credential vault)
-- ============================================================================
-- One row per stored secret belonging to a channel. The secret is stored as
-- ciphertext produced by the application (AES-256-GCM); the encryption key never
-- reaches PostgreSQL, so a database dump on its own is not a credential compromise.
--
-- HOW TENANT ISOLATION IS ENFORCED HERE
--
-- This is the one table in the schema where RLS is *not* the primary control:
--
--   1. NO application role holds any privilege on this table (see the revokes below).
--      A query from app_backend, authenticated, anon or service_role is rejected at
--      privilege-check time (42501), before a row is ever considered.
--   2. The only read path is app.channel_credential(uuid), which requires a NON-EMPTY
--      tenant context and matches the channel's tenant against it. It accepts no
--      tenant argument, so a caller cannot spoof one.
--   3. RLS is enabled with exactly one policy, for the function owner. It is a second
--      lock: if a grant is ever added by mistake, no policy exists for that role and
--      the query still returns zero rows.
--
-- WHY `enable row level security` AND DELIBERATELY NOT `force row level security`
--
-- FORCE would subject the table OWNER (the migration/repair role) to RLS. The owner
-- has no policy here, so a later data migration would silently read and update ZERO
-- rows -- a silent no-op is worse than an error, and it would only bite on the
-- non-superuser owner path (Railway), never on a superuser path (Supabase). This is
-- the opposite of public.tenant_channels (0011), which is not forced because a
-- SECURITY DEFINER helper owned by the table owner reads it.
--
-- The function owner is saas_platform_provisioner, a NOLOGIN NOINHERIT NOBYPASSRLS
-- role reachable only through SECURITY DEFINER functions. It is not the table owner,
-- so RLS applies to it and the policy below is what admits it. No role needs
-- BYPASSRLS for this design to work, and none is granted here.
--
-- Privilege note (post-0030 rule): runtime grants go to app_backend and the platform
-- roles only. Nothing here is granted to `authenticated`, `anon` or `service_role`.
-- ============================================================================

create table if not exists public.channel_credentials (
  id                uuid primary key default gen_random_uuid(),

  -- Denormalised from the channel so the accessor can compare it to the tenant
  -- context without a second table read. It is written ONLY by
  -- app.platform_store_credential, which derives it from public.tenant_channels;
  -- no caller may supply it.
  tenant_id         uuid not null references public.tenants (id) on delete cascade,
  channel_id        uuid not null references public.tenant_channels (id) on delete cascade,

  kind              text not null,

  -- Opaque application-produced ciphertext. `bytea`, never `text`: nothing here is
  -- ever rendered, searched or compared as a token.
  ciphertext        bytea not null,
  nonce             bytea not null,
  algorithm         text not null default 'aes-256-gcm',
  -- Which key encrypted this row (e.g. 'v1'). Rotation re-encrypts in place; an old
  -- row stays readable while its key remains in the keyring.
  key_ref           text not null,
  -- SHA-256 of the plaintext token: lets an operator verify a rotation or detect a
  -- re-stored duplicate WITHOUT the secret ever being stored.
  token_fingerprint text not null,

  issued_at         timestamptz not null default now(),
  expires_at        timestamptz,
  revoked_at        timestamptz,
  rotated_from      uuid references public.channel_credentials (id) on delete set null,

  -- Supabase Auth user id of the administrator who stored it; NULL for system work.
  created_by        uuid,
  created_at        timestamptz not null default now(),

  constraint channel_credentials_kind_check
    check (kind in ('business_token', 'system_user_token', 'registration_pin')),
  constraint channel_credentials_algorithm_check
    check (algorithm = 'aes-256-gcm'),
  -- A 4096-bit token and a 6-digit PIN both fit comfortably; the bound stops a
  -- malformed write from becoming an unbounded blob.
  constraint channel_credentials_ciphertext_check
    check (length(ciphertext) between 1 and 8192),
  -- GCM nonces are 12 bytes in this implementation; the band leaves room for a
  -- later scheme without a migration.
  constraint channel_credentials_nonce_check
    check (length(nonce) between 8 and 32),
  constraint channel_credentials_key_ref_check
    check (key_ref ~ '^[A-Za-z0-9._:-]{1,64}$'),
  -- Lower-case hex SHA-256. A fingerprint is never the secret, but it must be
  -- well-formed enough to be comparable.
  constraint channel_credentials_fingerprint_check
    check (token_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint channel_credentials_expiry_check
    check (expires_at is null or expires_at > issued_at),
  constraint channel_credentials_revoked_check
    check (revoked_at is null or revoked_at >= issued_at),
  constraint channel_credentials_no_self_rotation_check
    check (rotated_from is null or rotated_from <> id)
);

comment on table public.channel_credentials is
  'Encrypted per-channel credentials. No application role holds a privilege on this table; reads go through app.channel_credential(uuid) only.';
comment on column public.channel_credentials.tenant_id is
  'Derived from public.tenant_channels by app.platform_store_credential. Never supplied by a caller.';
comment on column public.channel_credentials.ciphertext is
  'AES-256-GCM ciphertext produced by the application. The key never reaches PostgreSQL.';
comment on column public.channel_credentials.key_ref is
  'Keyring entry that encrypted this row. Rotation re-encrypts and updates this value.';
comment on column public.channel_credentials.token_fingerprint is
  'SHA-256 of the plaintext token, for rotation verification. It is not reversible and is not the secret.';
comment on column public.channel_credentials.revoked_at is
  'Soft revocation. Rows are never deleted, so the audit trail of a replaced credential survives.';

-- One ACTIVE credential per channel per kind: a channel legitimately holds a
-- business token and a registration PIN at the same time, but never two live
-- tokens of the same kind. Revoked rows are exempt, which is what makes rotation
-- (revoke-then-insert) possible without deleting history.
create unique index if not exists channel_credentials_one_active_per_channel_kind_idx
  on public.channel_credentials (channel_id, kind)
  where revoked_at is null;

create index if not exists channel_credentials_tenant_idx
  on public.channel_credentials (tenant_id);

-- Expiry sweep / "which credentials expire soon" for the operator CLI.
create index if not exists channel_credentials_expiry_idx
  on public.channel_credentials (expires_at)
  where revoked_at is null and expires_at is not null;

-- ---------------------------------------------------------------------------
-- Row level security: enabled, NOT forced (see the header for why)
-- ---------------------------------------------------------------------------
alter table public.channel_credentials enable row level security;

drop policy if exists channel_credentials_vault_owner on public.channel_credentials;
create policy channel_credentials_vault_owner on public.channel_credentials
  for all to saas_platform_provisioner
  using (true)
  with check (true);

-- ---------------------------------------------------------------------------
-- Privileges: nobody but the function owner may touch the table
-- ---------------------------------------------------------------------------
revoke all on public.channel_credentials
  from public, anon, authenticated, app_backend;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on public.channel_credentials from service_role';
  end if;
end
$$;

-- The definer functions below run as this role. SELECT serves the accessor,
-- INSERT/UPDATE serve store and revoke. There is deliberately no DELETE grant:
-- revocation is a soft update so the trail survives.
grant select, insert, update on public.channel_credentials
  to saas_platform_provisioner;

-- ---------------------------------------------------------------------------
-- R27: the provisioner must be able to CALL the tenant-context helper
-- ---------------------------------------------------------------------------
-- 0012 revoked EXECUTE on every app.* function from PUBLIC and granted this helper
-- to app_backend/authenticated only. 0021 granted current_user_id() to the
-- provisioner but never current_tenant_id(). Because the accessor's body calls
-- app.current_tenant_id() and the body executes as the function OWNER, omitting
-- this grant makes every send fail with 42501 at runtime. Do not remove.
grant execute on function app.current_tenant_id() to saas_platform_provisioner;

-- ---------------------------------------------------------------------------
-- 1. The only read path: app.channel_credential(uuid)
-- ---------------------------------------------------------------------------
-- Returns AT MOST ONE row, for one channel, of the tenant already in context.
-- There is no tenant argument to spoof, and no way to list credentials.
--
-- The row is returned even when revoked, and expired rows are returned too, because
-- the caller must be able to tell these three situations apart:
--   * no row at all          -> this channel keeps its credential in the server
--                               environment (the pre-Phase-1 manual path);
--   * row, revoked_at set    -> a vault credential was deliberately revoked: fail,
--                               do NOT fall back to the environment;
--   * row, expires_at past   -> a vault credential lapsed: fail, do NOT fall back.
-- Without the inactive row the caller could not tell "revoked" from "never
-- vault-managed", which is exactly the silent fallback we forbid.
create or replace function app.channel_credential(p_channel_id uuid)
returns table (
  ciphertext bytea,
  nonce      bytea,
  algorithm  text,
  key_ref    text,
  expires_at timestamptz,
  revoked_at timestamptz
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  context_tenant uuid := app.current_tenant_id();
begin
  -- Fail closed and loudly: a missing context is a programming fault, not "no data".
  if context_tenant is null then
    raise exception using
      errcode = '42501',
      message = 'tenant context is required to read a channel credential';
  end if;

  return query
    select c.ciphertext, c.nonce, c.algorithm, c.key_ref, c.expires_at, c.revoked_at
      from public.channel_credentials c
     where c.channel_id = p_channel_id
       and c.tenant_id = context_tenant
     -- Prefer the live row; among revoked rows prefer the newest, so an operator
     -- sees the most recent revocation rather than the oldest one.
     order by (c.revoked_at is null) desc, c.created_at desc
     limit 1;
end;
$$;

comment on function app.channel_credential(uuid) is
  'One credential for one channel of the current tenant. Requires tenant context; returns inactive rows so callers can distinguish revoked/expired from absent.';

-- ---------------------------------------------------------------------------
-- 2. Write path: app.platform_store_credential(...)
-- ---------------------------------------------------------------------------
-- Stores a new credential and revokes the previous live one of the same kind in the
-- same transaction, so the partial unique index can never be violated and rotation
-- is atomic. The tenant is DERIVED from the channel: no caller-supplied tenant id is
-- accepted as evidence of anything.
create or replace function app.platform_store_credential(
  p_channel_id        uuid,
  p_kind              text,
  p_ciphertext        bytea,
  p_nonce             bytea,
  p_algorithm         text,
  p_key_ref           text,
  p_token_fingerprint text,
  p_expires_at        timestamptz default null
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  target_tenant uuid;
  previous_id   uuid;
  new_id        uuid;
begin
  perform app.require_platform_admin();

  if p_kind is null or p_kind not in ('business_token', 'system_user_token', 'registration_pin') then
    raise exception using errcode = '22023', message = 'unsupported credential kind';
  end if;
  if p_algorithm is distinct from 'aes-256-gcm' then
    raise exception using errcode = '22023', message = 'unsupported credential algorithm';
  end if;
  if p_ciphertext is null or length(p_ciphertext) = 0 then
    raise exception using errcode = '22023', message = 'ciphertext is required';
  end if;
  if p_nonce is null or length(p_nonce) not between 8 and 32 then
    raise exception using errcode = '22023', message = 'nonce must be between 8 and 32 bytes';
  end if;
  if p_key_ref is null or p_key_ref !~ '^[A-Za-z0-9._:-]{1,64}$' then
    raise exception using errcode = '22023', message = 'invalid key reference';
  end if;
  if p_token_fingerprint is null or p_token_fingerprint !~ '^[0-9a-f]{64}$' then
    raise exception using errcode = '22023', message = 'invalid token fingerprint';
  end if;
  if p_expires_at is not null and p_expires_at <= now() then
    raise exception using errcode = '22023', message = 'expiry must be in the future';
  end if;

  -- The channel is the authority on the tenant. A channel that does not exist, or
  -- that was deleted between the caller's check and this call, fails here.
  select c.tenant_id into target_tenant
    from public.tenant_channels c
   where c.id = p_channel_id;

  if target_tenant is null then
    raise exception using errcode = 'P0002', message = 'channel not found';
  end if;

  select cc.id into previous_id
    from public.channel_credentials cc
   where cc.channel_id = p_channel_id
     and cc.kind = p_kind
     and cc.revoked_at is null
   for update;

  -- Revoke BEFORE inserting: the partial unique index allows only one live row per
  -- (channel, kind), so the order is not interchangeable.
  if previous_id is not null then
    update public.channel_credentials
       set revoked_at = now()
     where id = previous_id
       and revoked_at is null;
  end if;

  insert into public.channel_credentials (
    tenant_id, channel_id, kind, ciphertext, nonce, algorithm, key_ref,
    token_fingerprint, expires_at, rotated_from, created_by
  )
  values (
    target_tenant, p_channel_id, p_kind, p_ciphertext, p_nonce, p_algorithm, p_key_ref,
    p_token_fingerprint, p_expires_at, previous_id, app.current_user_id()
  )
  returning id into new_id;

  insert into public.audit_log (
    tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata
  )
  values (
    target_tenant, app.current_user_id(), 'platform', 'credential.stored',
    'channel_credential', new_id::text,
    jsonb_build_object(
      'channel_id', p_channel_id,
      'kind', p_kind,
      'key_ref', p_key_ref,
      'fingerprint', p_token_fingerprint,
      'expires_at', p_expires_at,
      'rotated_from', previous_id
    )
  );

  return new_id;
end;
$$;

comment on function app.platform_store_credential(uuid, text, bytea, bytea, text, text, text, timestamptz) is
  'Stores an encrypted credential for a channel, rotating any live credential of the same kind. Tenant is derived from the channel.';

-- ---------------------------------------------------------------------------
-- 3. Revocation: app.platform_revoke_credential(...)
-- ---------------------------------------------------------------------------
-- Soft revocation only. There is no DELETE path anywhere: the row is the evidence
-- that a credential existed and when it stopped being valid.
create or replace function app.platform_revoke_credential(
  p_channel_id uuid,
  p_kind       text,
  p_reason     text default null
)
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public, app, pg_temp
as $$
declare
  target_tenant uuid;
  revoked_count integer := 0;
begin
  perform app.require_platform_admin();

  if p_kind is null or p_kind not in ('business_token', 'system_user_token', 'registration_pin') then
    raise exception using errcode = '22023', message = 'unsupported credential kind';
  end if;

  select c.tenant_id into target_tenant
    from public.tenant_channels c
   where c.id = p_channel_id;

  if target_tenant is null then
    raise exception using errcode = 'P0002', message = 'channel not found';
  end if;

  update public.channel_credentials
     set revoked_at = now()
   where channel_id = p_channel_id
     and kind = p_kind
     and revoked_at is null;

  get diagnostics revoked_count = row_count;

  insert into public.audit_log (
    tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata
  )
  values (
    target_tenant, app.current_user_id(), 'platform', 'credential.revoked',
    'tenant_channel', p_channel_id::text,
    jsonb_build_object('kind', p_kind, 'revoked', revoked_count, 'reason', p_reason)
  );

  return revoked_count;
end;
$$;

comment on function app.platform_revoke_credential(uuid, text, text) is
  'Revokes the live credential of a kind for a channel (soft delete) and audits the action.';

-- ---------------------------------------------------------------------------
-- Ownership and grants
-- ---------------------------------------------------------------------------
-- The provisioner must hold CREATE on schema app to be made the owner of a function,
-- and only while the change is made (see 0020/0021/0023/0024).
grant create on schema app to saas_platform_provisioner;

alter function app.channel_credential(uuid) owner to saas_platform_provisioner;
alter function app.platform_store_credential(uuid, text, bytea, bytea, text, text, text, timestamptz)
  owner to saas_platform_provisioner;
alter function app.platform_revoke_credential(uuid, text, text) owner to saas_platform_provisioner;

revoke create on schema app from saas_platform_provisioner;

-- Strip the implicit PUBLIC EXECUTE PostgreSQL grants to every new function
-- (the rule 0012 established; it must be repeated for each new function).
revoke all on function app.channel_credential(uuid) from public;
revoke all on function app.platform_store_credential(uuid, text, bytea, bytea, text, text, text, timestamptz) from public;
revoke all on function app.platform_revoke_credential(uuid, text, text) from public;

revoke execute on function app.channel_credential(uuid) from app_backend, authenticated, anon;
revoke execute on function app.platform_store_credential(uuid, text, bytea, bytea, text, text, text, timestamptz)
  from app_backend, authenticated, anon;
revoke execute on function app.platform_revoke_credential(uuid, text, text)
  from app_backend, authenticated, anon;

-- The accessor is the webhook/backend path: app_backend ONLY. A dashboard session
-- (authenticated) must never be able to read a credential at all.
grant execute on function app.channel_credential(uuid) to app_backend;

-- Writes belong to the platform administrator, exactly like platform_create_channel.
grant execute on function app.platform_store_credential(uuid, text, bytea, bytea, text, text, text, timestamptz)
  to saas_platform_admin;
grant execute on function app.platform_revoke_credential(uuid, text, text)
  to saas_platform_admin;
