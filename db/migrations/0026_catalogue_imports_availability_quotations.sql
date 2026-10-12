-- ============================================================================
-- Phase 4 / 0026 -- catalogue imports, availability and quotation snapshots
-- ============================================================================

alter table public.tenant_settings
  add column if not exists catalogue_version bigint not null default 0;

alter table public.product_variants
  add column if not exists sku text,
  add column if not exists availability text not null default 'IN_STOCK',
  add column if not exists stock_quantity integer,
  add column if not exists reorder_level integer;

update public.product_variants
set sku = left(product_id::text || '-' || code, 100)
where sku is null;

alter table public.product_variants
  alter column sku set not null;

alter table public.product_variants
  drop constraint if exists product_variants_sku_check,
  add constraint product_variants_sku_check
    check (length(btrim(sku)) between 1 and 100),
  drop constraint if exists product_variants_availability_check,
  add constraint product_variants_availability_check
    check (availability in ('IN_STOCK', 'OUT_OF_STOCK', 'MADE_TO_ORDER')),
  drop constraint if exists product_variants_stock_quantity_check,
  add constraint product_variants_stock_quantity_check
    check (stock_quantity is null or stock_quantity >= 0),
  drop constraint if exists product_variants_reorder_level_check,
  add constraint product_variants_reorder_level_check
    check (reorder_level is null or reorder_level >= 0);

create unique index if not exists product_variants_tenant_sku_unique
  on public.product_variants (tenant_id, lower(btrim(sku)));

create or replace function app.bump_catalogue_version()
returns trigger
language plpgsql
set search_path = pg_catalog, public, app
as $$
declare
  changed_tenant_id uuid;
begin
  if tg_op = 'DELETE' then
    changed_tenant_id := old.tenant_id;
  else
    changed_tenant_id := new.tenant_id;
  end if;
  update public.tenant_settings
  set catalogue_version = catalogue_version + 1
  where tenant_id = changed_tenant_id;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end
$$;

revoke all on function app.bump_catalogue_version() from public;
grant execute on function app.bump_catalogue_version() to app_backend, authenticated;

drop trigger if exists products_bump_catalogue_version on public.products;
create trigger products_bump_catalogue_version
  after insert or update or delete on public.products
  for each row execute function app.bump_catalogue_version();

drop trigger if exists product_variants_bump_catalogue_version on public.product_variants;
create trigger product_variants_bump_catalogue_version
  after insert or update or delete on public.product_variants
  for each row execute function app.bump_catalogue_version();

create table if not exists public.catalogue_import_batches (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null references public.tenants (id) on delete cascade,
  actor_user_id       uuid not null,
  status              text not null default 'preview',
  expected_version    bigint not null,
  rows                 jsonb not null,
  counts               jsonb not null default '{}'::jsonb,
  created_at           timestamptz not null default now(),
  expires_at           timestamptz not null default (now() + interval '30 minutes'),
  applied_at           timestamptz,

  constraint catalogue_import_batches_status_check
    check (status in ('preview', 'applied', 'failed', 'expired')),
  constraint catalogue_import_batches_rows_array
    check (jsonb_typeof(rows) = 'array'),
  constraint catalogue_import_batches_counts_object
    check (jsonb_typeof(counts) = 'object')
);

create index if not exists catalogue_import_batches_tenant_created_idx
  on public.catalogue_import_batches (tenant_id, created_at desc);

create table if not exists public.quotations (
  id                    uuid primary key default gen_random_uuid(),
  tenant_id             uuid not null references public.tenants (id) on delete cascade,
  quotation_number      text not null,
  customer_name         text not null,
  customer_contact      text,
  issue_date            date not null,
  expiry_date           date not null,
  currency              text not null,
  subtotal              numeric(14,2) not null,
  tax_rate              numeric(7,4),
  tax_amount            numeric(14,2),
  total                 numeric(14,2) not null,
  tax_treatment         text not null,
  terms                 text,
  status                text not null default 'issued',
  created_by_user_id    uuid not null,
  created_at            timestamptz not null default now(),

  constraint quotations_number_unique unique (tenant_id, quotation_number),
  constraint quotations_tenant_id_unique unique (tenant_id, id),
  constraint quotations_number_check check (length(btrim(quotation_number)) between 1 and 80),
  constraint quotations_customer_name_check check (length(btrim(customer_name)) between 1 and 200),
  constraint quotations_currency_check check (currency ~ '^[A-Z]{3}$'),
  constraint quotations_dates_check check (expiry_date >= issue_date),
  constraint quotations_amounts_check
    check (subtotal >= 0 and total >= 0 and (tax_amount is null or tax_amount >= 0)),
  constraint quotations_tax_rate_check
    check (tax_rate is null or (tax_rate >= 0 and tax_rate <= 100)),
  constraint quotations_status_check
    check (status in ('issued', 'cancelled'))
);

create index if not exists quotations_tenant_created_idx
  on public.quotations (tenant_id, created_at desc);

create table if not exists public.quotation_lines (
  id                    bigint generated always as identity primary key,
  tenant_id             uuid not null,
  quotation_id          uuid not null,
  source_product_id     uuid,
  source_variant_id     uuid,
  sku                   text not null,
  description           text not null,
  availability          text not null,
  quantity              integer not null,
  unit_price            numeric(12,2) not null,
  subtotal              numeric(14,2) not null,

  constraint quotation_lines_quotation_tenant_fk
    foreign key (tenant_id, quotation_id)
    references public.quotations (tenant_id, id)
    on delete cascade,
  constraint quotation_lines_sku_check check (length(btrim(sku)) between 1 and 100),
  constraint quotation_lines_availability_check
    check (availability in ('IN_STOCK', 'MADE_TO_ORDER')),
  constraint quotation_lines_quantity_check check (quantity between 1 and 100000),
  constraint quotation_lines_amounts_check check (unit_price >= 0 and subtotal >= 0)
);

create index if not exists quotation_lines_tenant_quotation_idx
  on public.quotation_lines (tenant_id, quotation_id, id);

alter table public.catalogue_import_batches enable row level security;
alter table public.catalogue_import_batches force row level security;
alter table public.quotations enable row level security;
alter table public.quotations force row level security;
alter table public.quotation_lines enable row level security;
alter table public.quotation_lines force row level security;

drop policy if exists catalogue_import_batches_backend_all on public.catalogue_import_batches;
create policy catalogue_import_batches_backend_all on public.catalogue_import_batches
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

drop policy if exists catalogue_import_batches_backend_select on public.catalogue_import_batches;
create policy catalogue_import_batches_backend_select on public.catalogue_import_batches
  for select to app_backend
  using (tenant_id = app.current_tenant_id());

drop policy if exists quotations_backend_all on public.quotations;
create policy quotations_backend_all on public.quotations
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

drop policy if exists quotations_member_select on public.quotations;
create policy quotations_member_select on public.quotations
  for select to authenticated
  using (app.is_tenant_member(tenant_id));

drop policy if exists quotation_lines_backend_all on public.quotation_lines;
create policy quotation_lines_backend_all on public.quotation_lines
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

drop policy if exists quotation_lines_member_select on public.quotation_lines;
create policy quotation_lines_member_select on public.quotation_lines
  for select to authenticated
  using (app.is_tenant_member(tenant_id));

revoke all on public.catalogue_import_batches, public.quotations, public.quotation_lines
  from anon, authenticated;
grant select on public.quotations, public.quotation_lines to authenticated;
grant select, insert, update, delete on
  public.catalogue_import_batches, public.quotations, public.quotation_lines
  to app_backend;
grant usage, select on sequence public.quotation_lines_id_seq to app_backend;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke all on public.catalogue_import_batches, public.quotations,
      public.quotation_lines from service_role';
    execute 'revoke all on sequence public.quotation_lines_id_seq from service_role';
  end if;
end
$$;

comment on column public.product_variants.sku is
  'Stable tenant-scoped import and quotation identifier.';
comment on column public.product_variants.availability is
  'IN_STOCK, OUT_OF_STOCK or MADE_TO_ORDER; does not imply an exact quantity.';
comment on table public.catalogue_import_batches is
  'Short-lived validated import previews with optimistic catalogue-version conflict checks.';
comment on table public.quotations is
  'Issued quotation headers with immutable monetary and tax snapshots.';
comment on table public.quotation_lines is
  'Immutable quotation line snapshots; issuing a quotation never reserves stock or creates an order.';
