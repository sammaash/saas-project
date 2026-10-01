-- ============================================================================
-- Phase 1 / 0009 -- audit_log (append-only)
-- ============================================================================
-- Append-only by design, enforced in three independent places:
--   1. no UPDATE or DELETE policy exists in 0011 (RLS denies by default),
--   2. 0012 does not grant UPDATE or DELETE on this table to any application role,
--   3. there is no updated_at column, so nothing suggests rows are mutable.
--
-- tenant_id is NULLABLE here, and only here: platform-level events (plan changes,
-- platform staff actions) legitimately have no tenant. That is a deliberate
-- exception to the tenant_id NOT NULL rule, documented in the structural test.

create table if not exists public.audit_log (
  -- bigint identity rather than uuid: audit rows are high-volume and append-only,
  -- so narrower index entries and sequential inserts are worth more than
  -- non-guessable ids. (Convention: high-volume append tables use bigint.)
  id            bigint generated always as identity primary key,

  -- ON DELETE SET NULL keeps the audit trail when a tenant row is removed
  -- (auditability outlives the tenant), losing row-level attribution but never
  -- the record itself.
  tenant_id     uuid references public.tenants (id) on delete set null,

  -- Supabase Auth user id of the actor, when a human acted. NULL for system work.
  actor_user_id uuid,

  actor_type    text not null default 'system',
  action        text not null,
  entity_type   text,
  -- text on purpose: Phase 1 entities use uuid keys, future high-volume tables
  -- (messages, orders, payments) will use bigint identity keys.
  entity_id     text,
  metadata      jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now(),

  constraint audit_log_actor_type_check
    check (actor_type in ('user', 'system', 'platform')),
  constraint audit_log_action_check
    check (length(btrim(action)) between 1 and 120),
  constraint audit_log_metadata_is_object
    check (jsonb_typeof(metadata) = 'object')
);

comment on table public.audit_log is
  'Append-only audit trail. No update/delete policy and no update/delete grants.';
comment on column public.audit_log.tenant_id is
  'NULL for platform-level events. The only tenant-owned table where this is nullable.';
comment on column public.audit_log.entity_id is
  'text because entity keys are uuid today and bigint for future high-volume tables.';

-- Tenant activity view: newest first, per tenant.
create index if not exists audit_log_tenant_created_idx
  on public.audit_log (tenant_id, created_at desc);

-- "What happened to this entity?" -- the query that resolves disputes.
create index if not exists audit_log_entity_idx
  on public.audit_log (entity_type, entity_id);

create index if not exists audit_log_action_idx
  on public.audit_log (action);
