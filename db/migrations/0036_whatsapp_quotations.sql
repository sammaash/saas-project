-- ============================================================================
-- Phase 5 / 0036 -- WhatsApp-origin quotations
-- ============================================================================
-- Additive only. Existing dashboard quotations keep their values and continue to
-- work unchanged:
--   * source defaults to 'dashboard' for every existing row;
--   * customer_wa_id and idempotency_key are nullable;
--   * created_by_user_id becomes nullable because a customer-initiated WhatsApp
--     quotation has no platform administrator actor. Dashboard quotations still
--     write the administrator's user id, and the dashboard audit event still
--     records that verified actor.
--
-- The unique constraint is tenant-scoped and includes source so a dashboard
-- quotation and a WhatsApp quotation can never collide, while retries with the
-- same stable idempotency_key cannot create duplicate rows. PostgreSQL treats
-- NULL idempotency_key values as distinct, so dashboard quotations (which have
-- no retry key) are unaffected.
--
-- No new table is created, so platform_admin.config.REQUIRED_SCHEMA is
-- deliberately unchanged: code that uses these columns must degrade safely when
-- the migration has not been applied rather than turn a missing migration into a
-- boot failure. Deploy order: migration first, then code.

alter table public.quotations
  alter column created_by_user_id drop not null;

alter table public.quotations
  add column if not exists source text not null default 'dashboard',
  add column if not exists customer_wa_id text,
  add column if not exists idempotency_key text;

alter table public.quotations
  drop constraint if exists quotations_source_check,
  add constraint quotations_source_check
    check (source in ('dashboard', 'whatsapp'));

alter table public.quotations
  drop constraint if exists quotations_customer_wa_id_check,
  add constraint quotations_customer_wa_id_check
    check (customer_wa_id is null
           or length(btrim(customer_wa_id)) between 1 and 200);

alter table public.quotations
  drop constraint if exists quotations_idempotency_key_check,
  add constraint quotations_idempotency_key_check
    check (idempotency_key is null
           or length(btrim(idempotency_key)) between 1 and 200);

alter table public.quotations
  drop constraint if exists quotations_source_idempotency_unique,
  add constraint quotations_source_idempotency_unique
    unique (tenant_id, source, idempotency_key);

comment on column public.quotations.created_by_user_id is
  'Platform administrator who issued the quotation, or NULL for a system-generated '
  'WhatsApp quotation.';
comment on column public.quotations.source is
  'Origin of the quotation: dashboard (staff-issued) or whatsapp (customer-initiated).';
comment on column public.quotations.customer_wa_id is
  'WhatsApp identity the quotation was issued to, for customer-initiated quotations.';
comment on column public.quotations.idempotency_key is
  'Stable retry key (for example the inbound provider message id) that prevents '
  'duplicate customer-initiated quotations.';
