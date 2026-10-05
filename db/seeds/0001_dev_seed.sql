-- ============================================================================
-- Phase 1 -- development / test seed data
-- ============================================================================
-- Two tenants, deliberately symmetric so isolation tests cannot pass by accident:
--
--   Tenant 001  Cindy Bakes   11111111-1111-4111-8111-111111111111  slug cindy-bakes
--   Tenant 002  Test Bakery   22222222-2222-4222-8222-222222222222  slug test-bakery
--
-- Tenant 001 is simply the first business onboarded onto the platform. It gets no special
-- treatment: no code anywhere branches on a tenant name, slug or id. "Cindy Bakes" here is
-- a label on a row, exactly like Tenant 002's.
--
-- ALL identifiers below are obviously fake development values. Neither tenant is pointed
-- at a real WhatsApp number: the channels are dev-phone-id-cindy-001 and
-- dev-phone-id-testbakery-002 with display numbers in the +2547000000xx range (the 000
-- range is not assignable). No real credential, phone number or business configuration
-- appears in this file.
--
-- Deterministic UUIDs keep the seed re-runnable and let the tests reference rows by
-- constant instead of by lookup. Idempotent throughout (ON CONFLICT DO NOTHING), so
-- `python db/run_migrations.py --seed` can be run any number of times.

-- ---------------------------------------------------------------------------
-- Plans (platform-owned: no tenant_id)
-- ---------------------------------------------------------------------------
insert into public.plans (id, code, name, description, monthly_price, currency, is_active, limits)
values (
  'd0000000-0000-4000-8000-000000000001',
  'starter',
  'Starter',
  'Development seed plan. Pricing and quotas are placeholders until finalised.',
  2500.00,
  'KES',
  true,
  '{"messages_per_month": 1500, "ai_included": true, "ai_tokens_per_month": 200000, "seats": 3}'::jsonb
)
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- Tenants
-- ---------------------------------------------------------------------------
insert into public.tenants (id, name, slug, status) values
  ('11111111-1111-4111-8111-111111111111', 'Cindy Bakes', 'cindy-bakes', 'active'),
  ('22222222-2222-4222-8222-222222222222', 'Test Bakery', 'test-bakery', 'active')
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- Channels: one WhatsApp channel per tenant.
-- phone_number_id is globally unique and is the inbound routing key.
-- credentials_ref points into the secret store -- it is never a token.
-- ---------------------------------------------------------------------------
insert into public.tenant_channels
  (id, tenant_id, channel_type, phone_number_id, waba_id, display_phone_number, status, is_default, credentials_ref)
values
  ('c0000000-0000-4000-8000-000000000001',
   '11111111-1111-4111-8111-111111111111',
   'whatsapp', 'dev-phone-id-cindy-001', 'dev-waba-001', '+254700000001', 'active', true,
   'dev/tenants/cindy-bakes/whatsapp'),
  ('c0000000-0000-4000-8000-000000000002',
   '22222222-2222-4222-8222-222222222222',
   'whatsapp', 'dev-phone-id-testbakery-002', 'dev-waba-002', '+254700000002', 'active', true,
   'dev/tenants/test-bakery/whatsapp')
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- Memberships: one owner per tenant, plus a staff member for Cindy Bakes so that
-- owner-vs-staff policy behaviour is exercised by the tests.
-- ---------------------------------------------------------------------------
insert into public.tenant_users (id, tenant_id, user_id, role, status) values
  ('a1000000-0000-4000-8000-000000000001',
   '11111111-1111-4111-8111-111111111111',
   'aaaaaaaa-0000-4000-8000-000000000001', 'owner', 'active'),
  ('a1000000-0000-4000-8000-000000000002',
   '11111111-1111-4111-8111-111111111111',
   'aaaaaaaa-0000-4000-8000-000000000002', 'staff', 'active'),
  ('a1000000-0000-4000-8000-000000000003',
   '22222222-2222-4222-8222-222222222222',
   'bbbbbbbb-0000-4000-8000-000000000001', 'owner', 'active')
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- Settings
--
-- These JSON blobs are PLACEHOLDER demonstration values, not any real business's
-- configuration. They differ between the two tenants so tenant-scoped reads are visibly
-- distinct, and they carry no real pricing, branding or business rules.
-- ---------------------------------------------------------------------------
insert into public.tenant_settings
  (tenant_id, timezone, currency, business_rules, bot_persona, business_hours, configuration)
values
  ('11111111-1111-4111-8111-111111111111', 'Africa/Nairobi', 'KES',
   '{"business_type":"bakery","deposit_rate":0.50,"deposit_deadline_days":3,"delivery_rate_per_km":25,"personalisation_prices":{"none":0,"option_a":300}}'::jsonb,
   '{"greeting":"Hello and welcome!","language":"en"}'::jsonb,
   '{"mon":{"open":"08:00","close":"18:00"},"sat":{"open":"09:00","close":"16:00"}}'::jsonb,
   '{"message_retention_days":90}'::jsonb),
  ('22222222-2222-4222-8222-222222222222', 'Africa/Nairobi', 'KES',
   '{"business_type":"bakery","deposit_rate":0.40,"deposit_deadline_days":2,"delivery_rate_per_km":20,"personalisation_prices":{"none":0,"option_a":250}}'::jsonb,
   '{"greeting":"Karibu!","language":"en"}'::jsonb,
   '{"mon":{"open":"07:00","close":"19:00"}}'::jsonb,
   '{"message_retention_days":90}'::jsonb)
on conflict (tenant_id) do nothing;

-- ---------------------------------------------------------------------------
-- Subscriptions: manual/invoice billing only. No STK push exists in Phase 1.
-- ---------------------------------------------------------------------------
insert into public.subscriptions
  (id, tenant_id, plan_id, status, billing_method, current_period_start, current_period_end)
values
  ('e0000000-0000-4000-8000-000000000001',
   '11111111-1111-4111-8111-111111111111',
   'd0000000-0000-4000-8000-000000000001', 'active', 'manual',
   date_trunc('month', now())::date,
   (date_trunc('month', now()) + interval '1 month - 1 day')::date),
  ('e0000000-0000-4000-8000-000000000002',
   '22222222-2222-4222-8222-222222222222',
   'd0000000-0000-4000-8000-000000000001', 'trial', 'manual',
   date_trunc('month', now())::date,
   (date_trunc('month', now()) + interval '1 month - 1 day')::date)
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- Audit rows: one platform-level event (tenant_id NULL -- the documented exception)
-- and one per-tenant event, so tests can prove platform rows are invisible to tenants.
-- Guarded by NOT EXISTS because audit_log.id is an identity column and the table is
-- intentionally append-only (there is no conflict target to rely on).
-- ---------------------------------------------------------------------------
insert into public.audit_log (tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata)
select null, null, 'platform', 'seed.platform_bootstrap', 'platform', 'phase-1',
       '{"note":"development seed"}'::jsonb
where not exists (
  select 1 from public.audit_log where action = 'seed.platform_bootstrap'
);

insert into public.audit_log (tenant_id, actor_user_id, actor_type, action, entity_type, entity_id, metadata)
select '11111111-1111-4111-8111-111111111111',
       'aaaaaaaa-0000-4000-8000-000000000001',
       'user', 'tenant.seeded', 'tenant', '11111111-1111-4111-8111-111111111111',
       '{"note":"development seed"}'::jsonb
where not exists (
  select 1 from public.audit_log
  where action = 'tenant.seeded' and tenant_id = '11111111-1111-4111-8111-111111111111'
);

