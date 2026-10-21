-- ===========================================================================
-- Phase 1 / 0032 -- generic tenant-aware orders, order items, payments, and
--                   the per-tenant order-number allocator
-- ===========================================================================
-- Purely additive: creates four new tables and alters none of the existing
-- ones, so rollback is a simple drop of these tables and cannot break any
-- behaviour that works today.
--
-- PRIVILEGE NOTE: every migration after 0030 may grant ONLY to `app_backend`.
-- The test test_hardening_migration_is_last_and_not_applied_with_other_grants_in_between
-- asserts no migration after the hardening pass re-grants to a Supabase-exposed
-- role (anon / authenticated / service_role). Like 0031, all grants below go to
-- app_backend alone. The dashboard and bot both connect as app_backend
-- (saas/db.py::connect), so nothing else is needed.
--
-- Nothing here is bakery- or Cindy-specific: prices, deposits, add-ons and
-- delivery arrive as tenant-configured data, never as code branches.

-- ---------------------------------------------------------------------------
-- Per-tenant order-number allocator. Claimed with an atomic UPDATE ... RETURNING
-- inside the caller's tenant transaction, so allocation is collision-safe. The
-- prefix is tenant-configurable (e.g. 'CB' for Cindy Bakes, default 'ORD') and
-- uniqueness is backstopped by orders' unique (tenant_id, order_number).
-- ---------------------------------------------------------------------------
create table if not exists public.tenant_order_sequences (
  tenant_id   uuid primary key references public.tenants (id) on delete cascade,
  prefix      text not null default 'ORD',
  next_value  bigint not null default 1,
  updated_at  timestamptz not null default now(),
  constraint tenant_order_sequences_prefix_check check (prefix ~ '^[A-Z0-9]{1,10}$'),
  constraint tenant_order_sequences_next_value_check check (next_value >= 1)
);

comment on table public.tenant_order_sequences is
  'Per-tenant allocator for human-facing order numbers. Prefix is tenant-configured.';

drop trigger if exists tenant_order_sequences_set_updated_at on public.tenant_order_sequences;
create trigger tenant_order_sequences_set_updated_at
  before update on public.tenant_order_sequences
  for each row execute function app.set_updated_at();

-- ---------------------------------------------------------------------------
-- Orders. A row in status 'draft' is an in-progress conversation draft.
-- 'pending_payment' means the customer accepted the complete final quote (order
-- acceptance) but no payment is verified yet. All money is computed server-side
-- from catalogue + order_policy; the LLM is never authoritative for amounts.
-- ---------------------------------------------------------------------------
create table if not exists public.orders (
  id                       uuid primary key default gen_random_uuid(),
  tenant_id                uuid not null references public.tenants (id) on delete cascade,
  order_number             text not null,
  idempotency_key          text,
  customer_name            text,
  customer_phone           text,
  whatsapp_wa_id           text,
  status                   text not null default 'draft',
  currency                 text not null default 'KES',
  fulfilment               text,
  required_date            date,
  delivery_location        text,
  delivery_fee             numeric(14,2) not null default 0,
  delivery_distance_km     numeric(12,2),
  delivery_needs_human_confirmation boolean not null default false,
  subtotal                 numeric(14,2) not null default 0,
  discount                 numeric(14,2) not null default 0,
  total                    numeric(14,2) not null default 0,
  deposit_rate             numeric(7,4),
  required_deposit         numeric(14,2) not null default 0,
  amount_paid              numeric(14,2) not null default 0,
  outstanding_balance      numeric(14,2) not null default 0,
  payment_status           text not null default 'unpaid',
  payment_deadline         date,
  payment_reminder_sent_at timestamptz,
  invoice_number           text,
  confirmed_at             timestamptz,
  cancelled_at             timestamptz,
  cancellation_reason      text,
  created_by_user_id       uuid,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);

alter table public.orders add constraint orders_number_unique unique (tenant_id, order_number);
alter table public.orders add constraint orders_tenant_id_unique unique (tenant_id, id);
alter table public.orders add constraint orders_idempotency_unique unique (tenant_id, idempotency_key);
alter table public.orders add constraint orders_number_check check (length(btrim(order_number)) between 1 and 80);
alter table public.orders add constraint orders_status_check
  check (status in ('draft', 'pending_payment', 'confirmed', 'in_progress',
                    'ready', 'completed', 'cancelled'));
alter table public.orders add constraint orders_payment_status_check
  check (payment_status in ('unpaid', 'awaiting_final', 'paid', 'overdue', 'refunded'));
alter table public.orders add constraint orders_currency_check check (currency ~ '^[A-Z]{3}$');
alter table public.orders add constraint orders_fulfilment_check
  check (fulfilment is null or fulfilment in ('pickup', 'delivery'));
alter table public.orders add constraint orders_amounts_check check (
  subtotal >= 0 and total >= 0 and discount >= 0 and required_deposit >= 0
  and amount_paid >= 0 and outstanding_balance >= 0 and delivery_fee >= 0
  and (delivery_distance_km is null or delivery_distance_km >= 0)
  and (deposit_rate is null or (deposit_rate >= 0 and deposit_rate <= 1))
);
alter table public.orders add constraint orders_customer_name_check
  check (customer_name is null or length(btrim(customer_name)) between 1 and 200);

comment on table public.orders is
  'Generic tenant order. Order acceptance, deposit verification and full payment are distinct states; prices/deposits are computed server-side.';
comment on column public.orders.idempotency_key is
  'Opaque caller-supplied key making a retried confirm_order a no-op instead of a duplicate.';

create index if not exists orders_tenant_created_idx
  on public.orders (tenant_id, created_at desc);
create index if not exists orders_tenant_status_idx
  on public.orders (tenant_id, status);
create index if not exists orders_tenant_wa_idx
  on public.orders (tenant_id, whatsapp_wa_id);
create index if not exists orders_reminder_due_idx
  on public.orders (tenant_id, payment_deadline)
  where payment_status in ('unpaid', 'awaiting_final')
    and payment_reminder_sent_at is null;

drop trigger if exists orders_set_updated_at on public.orders;
create trigger orders_set_updated_at
  before update on public.orders
  for each row execute function app.set_updated_at();

-- ---------------------------------------------------------------------------
-- Order items. Products, variants, add-ons/personalisation, delivery and
-- discount lines all live here as typed rows, so an invoice or quote can be
-- rebuilt exactly from persisted data. unit_price is a snapshot taken when the
-- order is confirmed: later catalogue price changes never rewrite history.
-- ---------------------------------------------------------------------------
-- product_variants has PRIMARY KEY (id) and UNIQUE (tenant_id, product_id,
-- code), but no UNIQUE (tenant_id, id) for a composite tenant-scoped FK to
-- target. Adding it here (before order_items) keeps the FK tenant-scoped
-- without touching 0031.
alter table public.product_variants
  add constraint product_variants_tenant_id_unique unique (tenant_id, id);
create table if not exists public.order_items (
  id          bigint generated always as identity primary key,
  tenant_id   uuid not null,
  order_id    uuid not null,
  item_type   text not null default 'product',
  product_id  uuid,
  variant_id  uuid,
  sku         text,
  name        text not null,
  quantity    integer not null,
  unit_price  numeric(12,2) not null,
  line_total  numeric(14,2) not null,
  attributes  jsonb not null default '{}'::jsonb,

  constraint order_items_order_tenant_fk
    foreign key (tenant_id, order_id)
    references public.orders (tenant_id, id)
    on delete cascade,
  constraint order_items_product_tenant_fk
    foreign key (tenant_id, product_id)
    references public.products (tenant_id, id)
    on delete set null,
  constraint order_items_variant_tenant_fk
    foreign key (tenant_id, variant_id)
    references public.product_variants (tenant_id, id)
    on delete set null,
  constraint order_items_item_type_check
    check (item_type in ('product', 'add_on', 'delivery', 'discount')),
  constraint order_items_name_check check (length(btrim(name)) between 1 and 200),
  constraint order_items_quantity_check check (quantity between 1 and 100000),
  constraint order_items_amounts_check check (unit_price >= 0 and line_total >= 0)
);

comment on table public.order_items is
  'Immutable-priced line items for an order: products, add-ons/personalisation, delivery and discount.';

create index if not exists order_items_tenant_order_idx
  on public.order_items (tenant_id, order_id, id);


-- ---------------------------------------------------------------------------
-- Payments. An auditable transaction ledger supporting partial payments,
-- multiple payments, refunds and reconciliation. amount is always positive and
-- the direction is carried by kind; orders.amount_paid is the net of VERIFIED
-- rows only (payments minus refunds), so an unverified or rejected transaction
-- never moves a balance.
--
-- Three distinct concepts:
--   order acceptance -> orders.status = 'pending_payment' (no payment row yet)
--   deposit verified -> a verified 'payment' row >= required_deposit
--   full payment     -> net verified amount >= orders.total
-- ---------------------------------------------------------------------------
create table if not exists public.order_payments (
  id              bigint generated always as identity primary key,
  tenant_id       uuid not null,
  order_id        uuid not null,
  kind            text not null,
  amount          numeric(14,2) not null,
  method          text,
  reference       text,
  idempotency_key text,
  status          text not null default 'recorded',
  recorded_by     uuid,
  verified_by     uuid,
  verified_at     timestamptz,
  note            text,
  created_at      timestamptz not null default now(),

  constraint order_payments_order_tenant_fk
    foreign key (tenant_id, order_id)
    references public.orders (tenant_id, id)
    on delete cascade,
  constraint order_payments_idempotency_unique unique (tenant_id, idempotency_key),
  constraint order_payments_kind_check check (kind in ('payment', 'refund')),
  constraint order_payments_status_check check (status in ('recorded', 'verified', 'rejected')),
  constraint order_payments_amount_check check (amount > 0),
  constraint order_payments_method_check check (method is null or length(btrim(method)) between 1 and 60),
  constraint order_payments_verified_check
    check ((status = 'verified') = (verified_at is not null))
);

comment on table public.order_payments is
  'Append-friendly payment ledger: partial, multiple and refund entries; balances derive from verified rows only.';
comment on column public.order_payments.kind is
  'payment increases, refund decreases; amount is always positive, direction is the kind.';

create index if not exists order_payments_tenant_order_idx
  on public.order_payments (tenant_id, order_id, id);

-- ---------------------------------------------------------------------------
-- Row-level security: enabled AND forced on every new table, with both a SELECT
-- policy and an ALL policy per table (required by PolicyCoverageTests). The sole
-- predicate is app.current_tenant_id(), exactly like the existing tables.
-- ---------------------------------------------------------------------------
alter table public.tenant_order_sequences enable row level security;
alter table public.tenant_order_sequences force row level security;
alter table public.orders enable row level security;
alter table public.orders force row level security;
alter table public.order_items enable row level security;
alter table public.order_items force row level security;
alter table public.order_payments enable row level security;
alter table public.order_payments force row level security;

drop policy if exists tenant_order_sequences_backend_select on public.tenant_order_sequences;
create policy tenant_order_sequences_backend_select on public.tenant_order_sequences
  for select using (tenant_id = app.current_tenant_id());
drop policy if exists tenant_order_sequences_backend_all on public.tenant_order_sequences;
create policy tenant_order_sequences_backend_all on public.tenant_order_sequences
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

drop policy if exists orders_backend_select on public.orders;
create policy orders_backend_select on public.orders
  for select using (tenant_id = app.current_tenant_id());
drop policy if exists orders_backend_all on public.orders;
create policy orders_backend_all on public.orders
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

drop policy if exists order_items_backend_select on public.order_items;
create policy order_items_backend_select on public.order_items
  for select using (tenant_id = app.current_tenant_id());
drop policy if exists order_items_backend_all on public.order_items;
create policy order_items_backend_all on public.order_items
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

drop policy if exists order_payments_backend_select on public.order_payments;
create policy order_payments_backend_select on public.order_payments
  for select using (tenant_id = app.current_tenant_id());
drop policy if exists order_payments_backend_all on public.order_payments;
create policy order_payments_backend_all on public.order_payments
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

-- ---------------------------------------------------------------------------
-- Grants: app_backend ONLY (see privilege note at top of file).
-- ---------------------------------------------------------------------------
grant select on public.tenant_order_sequences to app_backend;
grant select, insert, update, delete on public.tenant_order_sequences to app_backend;

grant select on public.orders to app_backend;
grant select, insert, update, delete on public.orders to app_backend;

grant select on public.order_items to app_backend;
grant select, insert, update, delete on public.order_items to app_backend;
grant usage, select on sequence public.order_items_id_seq to app_backend;

grant select on public.order_payments to app_backend;
grant select, insert, update, delete on public.order_payments to app_backend;
grant usage, select on sequence public.order_payments_id_seq to app_backend;
