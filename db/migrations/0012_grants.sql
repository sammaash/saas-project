-- ============================================================================
-- Phase 1 / 0012 -- least-privilege grants
-- ============================================================================
-- Grants are deliberately narrower than the policy matrix, so a future mistake in a
-- policy cannot by itself widen access:
--   * no role except platform tooling can UPDATE or DELETE audit_log,
--   * no application role can write plans,
--   * anon (unauthenticated browser) gets nothing at all.
--
-- Note that GRANT and RLS are independent layers: a role needs both the grant and a
-- matching policy to touch a row.

grant usage on schema app to app_backend, authenticated;

-- Explicit schema usage for `public` as well: the built-in public schema grants USAGE
-- to PUBLIC, but a schema created by a migration (or recreated by --reset) may not.
-- Stating it keeps behaviour identical across local, CI and Supabase.
grant usage on schema public to app_backend, authenticated;

-- tenants: read/update within context. No INSERT/DELETE -- provisioning and
-- offboarding are platform actions.
grant select, update on public.tenants to app_backend, authenticated;

-- tenant_channels: tenants may manage their own channels; the backend manages all
-- channel operations within context.
grant select, insert, update, delete on public.tenant_channels to app_backend, authenticated;

-- tenant_users: membership management.
grant select, insert, update, delete on public.tenant_users to app_backend, authenticated;

-- tenant_settings: no DELETE (the row is one-to-one with the tenant).
grant select, insert, update on public.tenant_settings to app_backend, authenticated;

-- subscriptions: tenants read; the backend writes within context.
grant select, insert, update on public.subscriptions to app_backend;
grant select on public.subscriptions to authenticated;

-- audit_log: append and read only. Nothing else, for anybody.
grant select, insert on public.audit_log to app_backend;
grant select on public.audit_log to authenticated;

-- plans: read-only to applications.
grant select on public.plans to app_backend, authenticated;

-- Belt and braces: state the append-only intent explicitly, so that even if a future
-- broad GRANT is added carelessly, this migration documents the requirement.
revoke update, delete on public.audit_log from app_backend, authenticated;
revoke insert, update, delete on public.plans from app_backend, authenticated;

-- audit_log uses a bigint identity column, so writers need the sequence.
grant usage, select on all sequences in schema public to app_backend;

-- The `app` schema is ours: strip the default PUBLIC surface and grant only the
-- functions each role actually needs. (Functions are EXECUTE-able by PUBLIC unless
-- revoked, which would expose helpers to unauthenticated callers.)
revoke all on schema app from public;
revoke execute on all functions in schema app from public;

grant execute on function app.current_tenant_id() to app_backend, authenticated;
grant execute on function app.current_user_id() to app_backend, authenticated;
grant execute on function app.is_tenant_member(uuid) to app_backend, authenticated;
grant execute on function app.is_tenant_owner(uuid) to app_backend, authenticated;

-- Routing is a backend/webhook concern only: a browser never resolves a tenant from a
-- phone number, so `authenticated` is deliberately not granted this function.
grant execute on function app.resolve_tenant_by_phone_number_id(text) to app_backend;

-- Unauthenticated callers get no access to any platform table.
revoke all on all tables in schema public from anon;
revoke all on all sequences in schema public from anon;
