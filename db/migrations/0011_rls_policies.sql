-- ============================================================================
-- Phase 1 / 0011 -- Row Level Security
-- ============================================================================
-- Two policy families, deliberately separate because the two access paths have
-- different notions of "who is this?":
--
--   Family A -- app_backend (the Flask backend, bot and workers)
--       Context is established server-side per transaction:
--         select set_config('app.tenant_id', <uuid>, true)
--       Predicate: tenant_id = app.current_tenant_id()
--       With no context set the helper returns NULL, the predicate is NULL, and the
--       query returns ZERO rows. Fail closed, always.
--
--   Family B -- authenticated (the dashboard, via a Supabase user JWT)
--       Context comes from verified JWT claims (request.jwt.claims->>'sub', read by
--       app.current_user_id()).
--       Predicate: app.is_tenant_member(tenant_id) for reads,
--                  app.is_tenant_owner(tenant_id) for writes.
--
-- Deny by default: with RLS enabled and no matching policy a command is refused. No
-- policy ever reads a tenant id from a browser request body or query string -- the
-- client cannot claim a tenant.
--
-- USING vs WITH CHECK: USING filters which existing rows are visible/affected;
-- WITH CHECK constrains what may be WRITTEN. Both are required, otherwise tenant A
-- could insert rows or move them into tenant B.
-- ============================================================================

alter table public.tenants          enable row level security;
alter table public.tenant_channels  enable row level security;
alter table public.tenant_users     enable row level security;
alter table public.tenant_settings  enable row level security;
alter table public.plans            enable row level security;
alter table public.subscriptions    enable row level security;
alter table public.audit_log        enable row level security;

-- FORCE where appropriate: it makes RLS apply to the table owner as well.
--
-- Deliberately NOT forced on tenant_channels and tenant_users: both are read by the
-- SECURITY DEFINER helpers in 0010, and forcing would filter those helpers too. Only
-- the owner is affected by this choice; app_backend and authenticated stay fully
-- filtered, which the isolation test suite asserts directly.
alter table public.tenants          force row level security;
alter table public.tenant_settings  force row level security;
alter table public.plans            force row level security;
alter table public.subscriptions    force row level security;
alter table public.audit_log        force row level security;
-- intentionally not forced:
--   public.tenant_channels  (read by app.resolve_tenant_by_phone_number_id)
--   public.tenant_users     (read by app.is_tenant_member / app.is_tenant_owner)

-- Re-runnable: drop before create so this migration can be applied repeatedly.
drop policy if exists tenants_backend_select on public.tenants;
drop policy if exists tenants_backend_update on public.tenants;
drop policy if exists tenants_member_select  on public.tenants;
drop policy if exists tenants_owner_update   on public.tenants;
drop policy if exists tenant_channels_backend_all   on public.tenant_channels;
drop policy if exists tenant_channels_member_select on public.tenant_channels;
drop policy if exists tenant_channels_owner_insert  on public.tenant_channels;
drop policy if exists tenant_channels_owner_update  on public.tenant_channels;
drop policy if exists tenant_channels_owner_delete  on public.tenant_channels;

-- ----------------------------------------------------------------------------
-- tenants
-- The root table has no tenant_id: its primary key IS the tenant identity.
-- INSERT and DELETE are intentionally absent -- creating or destroying a tenant is a
-- platform action (provisioning/offboarding), never something a tenant performs.
-- ----------------------------------------------------------------------------
create policy tenants_backend_select on public.tenants
  for select to app_backend
  using (id = app.current_tenant_id());

create policy tenants_backend_update on public.tenants
  for update to app_backend
  using (id = app.current_tenant_id())
  with check (id = app.current_tenant_id());

create policy tenants_member_select on public.tenants
  for select to authenticated
  using (app.is_tenant_member(id));

create policy tenants_owner_update on public.tenants
  for update to authenticated
  using (app.is_tenant_owner(id))
  with check (app.is_tenant_owner(id));

-- ----------------------------------------------------------------------------
-- tenant_channels
-- ----------------------------------------------------------------------------
-- Family A: full DML, but only ever for rows belonging to the current tenant.
create policy tenant_channels_backend_all on public.tenant_channels
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

-- Family B: members may read; only owners may change a channel.
create policy tenant_channels_member_select on public.tenant_channels
  for select to authenticated
  using (app.is_tenant_member(tenant_id));

create policy tenant_channels_owner_insert on public.tenant_channels
  for insert to authenticated
  with check (app.is_tenant_owner(tenant_id));

create policy tenant_channels_owner_update on public.tenant_channels
  for update to authenticated
  using (app.is_tenant_owner(tenant_id))
  with check (app.is_tenant_owner(tenant_id));

create policy tenant_channels_owner_delete on public.tenant_channels
  for delete to authenticated
  using (app.is_tenant_owner(tenant_id));

drop policy if exists tenant_users_backend_all  on public.tenant_users;
drop policy if exists tenant_users_self_or_owner_select on public.tenant_users;
drop policy if exists tenant_users_owner_insert on public.tenant_users;
drop policy if exists tenant_users_owner_update on public.tenant_users;
drop policy if exists tenant_users_owner_delete on public.tenant_users;
drop policy if exists tenant_settings_backend_select on public.tenant_settings;
drop policy if exists tenant_settings_backend_insert on public.tenant_settings;
drop policy if exists tenant_settings_backend_update on public.tenant_settings;
drop policy if exists tenant_settings_member_select  on public.tenant_settings;
drop policy if exists tenant_settings_owner_insert   on public.tenant_settings;
drop policy if exists tenant_settings_owner_update   on public.tenant_settings;

-- ----------------------------------------------------------------------------
-- tenant_users
-- ----------------------------------------------------------------------------
create policy tenant_users_backend_all on public.tenant_users
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

-- A member can see their own membership row; an owner can see the tenant's team.
create policy tenant_users_self_or_owner_select on public.tenant_users
  for select to authenticated
  using (user_id = app.current_user_id() or app.is_tenant_owner(tenant_id));

create policy tenant_users_owner_insert on public.tenant_users
  for insert to authenticated
  with check (app.is_tenant_owner(tenant_id));

create policy tenant_users_owner_update on public.tenant_users
  for update to authenticated
  using (app.is_tenant_owner(tenant_id))
  with check (app.is_tenant_owner(tenant_id));

create policy tenant_users_owner_delete on public.tenant_users
  for delete to authenticated
  using (app.is_tenant_owner(tenant_id));

-- ----------------------------------------------------------------------------
-- tenant_settings
-- No DELETE policy: the row is one-to-one with its tenant and disappears only with the
-- tenant (ON DELETE CASCADE, which is not subject to the policy).
-- ----------------------------------------------------------------------------
create policy tenant_settings_backend_select on public.tenant_settings
  for select to app_backend
  using (tenant_id = app.current_tenant_id());

create policy tenant_settings_backend_insert on public.tenant_settings
  for insert to app_backend
  with check (tenant_id = app.current_tenant_id());

create policy tenant_settings_backend_update on public.tenant_settings
  for update to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

create policy tenant_settings_member_select on public.tenant_settings
  for select to authenticated
  using (app.is_tenant_member(tenant_id));

create policy tenant_settings_owner_insert on public.tenant_settings
  for insert to authenticated
  with check (app.is_tenant_owner(tenant_id));

create policy tenant_settings_owner_update on public.tenant_settings
  for update to authenticated
  using (app.is_tenant_owner(tenant_id))
  with check (app.is_tenant_owner(tenant_id));

-- ----------------------------------------------------------------------------
-- plans (platform-owned, no tenant_id)
-- Read-only to applications: plans are maintained by the platform, so no
-- insert/update/delete policy exists for either application role.
-- ----------------------------------------------------------------------------
create policy plans_read_active on public.plans
  for select to app_backend, authenticated
  using (is_active);

-- ----------------------------------------------------------------------------
-- subscriptions
-- Tenants may read their own subscription. Writing subscriptions is platform/billing
-- business, so `authenticated` gets no write policy; only app_backend may write, and
-- only within its tenant context. No DELETE policy: cancellation is a status change.
-- ----------------------------------------------------------------------------
create policy subscriptions_backend_select on public.subscriptions
  for select to app_backend
  using (tenant_id = app.current_tenant_id());

create policy subscriptions_backend_insert on public.subscriptions
  for insert to app_backend
  with check (tenant_id = app.current_tenant_id());

create policy subscriptions_backend_update on public.subscriptions
  for update to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

create policy subscriptions_member_select on public.subscriptions
  for select to authenticated
  using (app.is_tenant_member(tenant_id));

-- ----------------------------------------------------------------------------
-- audit_log (append-only)
-- NO update and NO delete policy for anybody: RLS denies both by default, so the audit
-- trail cannot be rewritten through the application path even if a role were
-- mistakenly granted UPDATE later.
--
-- Platform-level rows (tenant_id IS NULL) are visible to no tenant policy, and the
-- backend cannot create them: its WITH CHECK requires a matching tenant context.
-- ----------------------------------------------------------------------------
create policy audit_log_backend_select on public.audit_log
  for select to app_backend
  using (tenant_id = app.current_tenant_id());

create policy audit_log_backend_insert on public.audit_log
  for insert to app_backend
  with check (tenant_id = app.current_tenant_id());

create policy audit_log_member_select on public.audit_log
  for select to authenticated
  using (tenant_id is not null and app.is_tenant_member(tenant_id));


