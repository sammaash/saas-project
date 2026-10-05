-- ============================================================================
-- Phase 2 / 0017 -- tenant-owned catalogue
-- ============================================================================
-- Products and variants are tenant-scoped and support multiple business
-- verticals without schema forks.

create table if not exists public.products (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references public.tenants (id) on delete cascade,
  name            text not null,
  name_normalized text generated always as (lower(btrim(name))) stored,
  description     text,
  category        text,
  is_active       boolean not null default true,
  sort_order      integer not null default 0,
  attributes      jsonb not null default '{}'::jsonb,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),

  constraint products_name_check check (length(btrim(name)) between 1 and 200),
  constraint products_sort_order_check check (sort_order >= 0),
  constraint products_attributes_is_object check (jsonb_typeof(attributes) = 'object'),
  constraint products_tenant_name_unique unique (tenant_id, name_normalized),
  constraint products_tenant_id_id_unique unique (tenant_id, id)
);

create index if not exists products_tenant_active_sort_idx
  on public.products (tenant_id, is_active, sort_order, name);

drop trigger if exists products_set_updated_at on public.products;
create trigger products_set_updated_at
  before update on public.products
  for each row execute function app.set_updated_at();

create table if not exists public.product_variants (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null,
  product_id  uuid not null,
  code        text not null,
  name        text not null,
  price       numeric(12,2) not null,
  attributes  jsonb not null default '{}'::jsonb,
  is_active   boolean not null default true,
  sort_order  integer not null default 0,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),

  constraint product_variants_product_tenant_fk
    foreign key (tenant_id, product_id)
    references public.products (tenant_id, id)
    on delete cascade,
  constraint product_variants_code_check check (length(btrim(code)) between 1 and 100),
  constraint product_variants_name_check check (length(btrim(name)) between 1 and 200),
  constraint product_variants_price_check check (price >= 0),
  constraint product_variants_sort_order_check check (sort_order >= 0),
  constraint product_variants_attributes_is_object check (jsonb_typeof(attributes) = 'object'),
  constraint product_variants_tenant_product_code_unique unique (tenant_id, product_id, code)
);

create index if not exists product_variants_tenant_product_active_sort_idx
  on public.product_variants (tenant_id, product_id, is_active, sort_order, name);

drop trigger if exists product_variants_set_updated_at on public.product_variants;
create trigger product_variants_set_updated_at
  before update on public.product_variants
  for each row execute function app.set_updated_at();

comment on table public.products is
  'Tenant-owned catalogue products.';
comment on table public.product_variants is
  'Tenant-owned priced options belonging to a product.';
