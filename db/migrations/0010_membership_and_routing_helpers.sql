-- ============================================================================
-- Phase 1 / 0010 -- membership helpers and the phone_number_id routing resolver
-- ============================================================================
-- WHY SECURITY DEFINER
--
-- An RLS policy must never query the table it protects. PostgreSQL would re-evaluate
-- that same policy for the inner query and recurse until it errors. These functions
-- are therefore owned by the migration / table-owner role and read
-- public.tenant_users with the owner's privileges.
--
-- For that to work, FORCE ROW LEVEL SECURITY is deliberately NOT applied to
-- public.tenant_users and public.tenant_channels (see 0011): FORCE would subject the
-- owner to RLS as well, and these helpers would stop seeing rows.
--
-- This does NOT weaken application isolation. FORCE only changes the behaviour of the
-- table OWNER, and the application never connects as the owner -- app_backend and
-- authenticated are ordinary roles, so RLS still filters every application query on
-- both tables. tests/test_saas_isolation.py asserts that directly.

-- ----------------------------------------------------------------------------
-- Membership helpers, used by the dashboard (JWT) policy family.
-- Both read the caller's own membership AND the membership of the tenant being
-- checked, which is only possible because they run with the owner's privileges.
-- ----------------------------------------------------------------------------
create or replace function app.is_tenant_member(target_tenant_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1
      from public.tenant_users tu
     where tu.tenant_id = target_tenant_id
       and tu.user_id = app.current_user_id()
       and tu.status <> 'disabled'
  );
$$;

comment on function app.is_tenant_member(uuid) is
  'True when the authenticated user (verified JWT sub) is a non-disabled member of the tenant.';

create or replace function app.is_tenant_owner(target_tenant_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1
      from public.tenant_users tu
     where tu.tenant_id = target_tenant_id
       and tu.user_id = app.current_user_id()
       and tu.role = 'owner'
       and tu.status <> 'disabled'
  );
$$;

comment on function app.is_tenant_owner(uuid) is
  'True when the authenticated user is a non-disabled owner of the tenant. Used for write policies.';

-- ----------------------------------------------------------------------------
-- WhatsApp routing: phone_number_id -> tenant_id.
--
-- The single intentionally elevated read path in Phase 1. An inbound WhatsApp
-- webhook must resolve its tenant BEFORE any tenant context can exist, and RLS
-- (correctly) blocks that lookup, so this narrow function performs just that step.
--
-- Least privilege: it returns ONE uuid, never row data, and only for an exact match
-- on an active WhatsApp channel. Unknown or disabled numbers return NULL, which the
-- webhook must treat as "quarantine and alert", never as "guess a tenant".
--
-- Tenant *status* is not checked here on purpose: public.tenants is FORCE-RLS'd, so
-- this function cannot read it. The caller verifies tenant status after establishing
-- tenant context, where the tenant row is legitimately readable.
-- ----------------------------------------------------------------------------
create or replace function app.resolve_tenant_by_phone_number_id(target_phone_number_id text)
returns uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select tc.tenant_id
    from public.tenant_channels tc
   where tc.phone_number_id = target_phone_number_id
     and tc.channel_type = 'whatsapp'
     and tc.status = 'active'
   limit 1;
$$;

comment on function app.resolve_tenant_by_phone_number_id(text) is
  'Inbound WhatsApp routing key -> tenant id. Returns NULL when the number is unknown or disabled.';
