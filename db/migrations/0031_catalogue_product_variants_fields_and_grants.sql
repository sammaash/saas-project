-- ===========================================================================
-- Phase 5 / 0031 -- catalogue product_variants fields + app_backend read grants
-- ===========================================================================
-- Re-applies the minimal column additions from 0026 (sku, availability,
-- stock_quantity, reorder_level) and re-states the app_backend read grants
-- for the catalogue path. Deployments whose schema is behind 0026 (the
-- migration was never applied) previously produced a 500 "Something went
-- wrong." on the catalogue GET endpoint because ``product_variants`` was
-- missing the columns the SaaS codebase queries. This migration is
-- idempotent: re-running it is a no-op once the columns exist.
--
-- NOTE: this migration does NOT recreate the policy/trigger/function surface
-- of 0026 (bump_catalogue_version etc.); it only guarantees the columns and
-- the read grants that the catalogue path depends on. Run the full startup
-- migration set to restore those too.
do $$
begin
  if not exists (select 1
                 from information_schema.columns
                 where table_schema = 'public'
                   and table_name = 'product_variants'
                   and column_name in ('sku', 'availability', 'stock_quantity', 'reorder_level')) then

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
        check (length(btrim(sku)) between 1 and 100);

    alter table public.product_variants
      drop constraint if exists product_variants_availability_check,
      add constraint product_variants_availability_check
        check (availability in ('IN_STOCK', 'OUT_OF_STOCK', 'MADE_TO_ORDER'));

    alter table public.product_variants
      drop constraint if exists product_variants_stock_quantity_check,
      add constraint product_variants_stock_quantity_check
        check (stock_quantity is null or stock_quantity >= 0);

    alter table public.product_variants
      drop constraint if exists product_variants_reorder_level_check,
      add constraint product_variants_reorder_level_check
        check (reorder_level is null or reorder_level >= 0);

    create unique index if not exists product_variants_tenant_sku_unique
      on public.product_variants (tenant_id, lower(btrim(sku)));
  end if;
end
$$;

-- app_backend MUST be able to SELECT the catalogue tables it reads under
-- tenant context (RLS applies: products_backend_all /
-- product_variants_backend_all). Without this grant the catalogue queries
-- raise InsufficientPrivilege. Grant is idempotent; previous grants are not
-- truncated by this re-statement.
grant select on public.products to app_backend;
grant select on public.product_variants to app_backend;
grant select on public.quotations to app_backend;
grant select, insert, update, delete on public.catalogue_import_batches to app_backend;
grant usage, select on sequence public.quotation_lines_id_seq to app_backend;
