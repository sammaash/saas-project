-- ============================================================================
-- 0041 -- remove hosted-platform default privileges left by 0032 and 0039
-- ============================================================================
-- Supabase sets default privileges so every table and sequence the migration role creates
-- in schema public is granted ALL to anon, authenticated and service_role (see 0025).
-- 0032 (orders, order_items, order_payments, tenant_order_sequences and the identity
-- sequences) and 0039
-- (conversation_control_events) revoked only from PUBLIC, so on the hosted database those
-- three roles kept every privilege. Row Level Security hides the rows (no policy exists for
-- them), but TRUNCATE, TRIGGER, REFERENCES and sequence UPDATE (setval) are not subject to
-- RLS, and none of these tables is meant to be reachable through the Data API at all: they
-- are backend-only (app_backend), like every table since 0030.
--
-- Found by a read-only privilege inventory of production on 2026-11-16. On plain PostgreSQL
-- (CI, staging) the roles never held these grants and this migration changes nothing.
-- app_backend's own grants are explicit and are not touched.

revoke all on
  public.orders, public.order_items, public.order_payments, public.tenant_order_sequences,
  public.conversation_control_events
  from public, anon, authenticated;
revoke all on sequence
  public.order_items_id_seq, public.order_payments_id_seq
  from public, anon, authenticated;

do $$
begin
  if exists (select 1 from pg_catalog.pg_roles where rolname = 'service_role') then
    execute 'revoke all on public.orders, public.order_items, public.order_payments, public.tenant_order_sequences,
               public.conversation_control_events from service_role';
    execute 'revoke all on sequence public.order_items_id_seq, public.order_payments_id_seq
               from service_role';
  end if;
end
$$;
