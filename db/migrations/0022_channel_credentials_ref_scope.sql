-- ============================================================================
-- Phase 3 / 0022 -- a channel's credentials_ref must belong to the channel's tenant
-- ============================================================================
-- credentials_ref is a pointer into a secret store, never a secret. Scoping the pointer
-- to the owning tenant stops one tenant's credentials being attached to another tenant's
-- channel. NOT VALID: rows written before this migration are not re-checked, but every
-- insert and update from now on is.

alter table public.tenant_channels
  drop constraint if exists tenant_channels_credentials_ref_scope_check;

alter table public.tenant_channels
  add constraint tenant_channels_credentials_ref_scope_check
  check (
    credentials_ref is null
    or credentials_ref like 'tenants/' || tenant_id::text || '/%'
  ) not valid;

comment on constraint tenant_channels_credentials_ref_scope_check on public.tenant_channels is
  'credentials_ref must start with tenants/<tenant_id>/ so a pointer cannot cross tenants.';
