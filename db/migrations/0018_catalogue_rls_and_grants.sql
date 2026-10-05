-- ============================================================================
-- Phase 2 / 0018 -- catalogue RLS and least-privilege grants
-- ============================================================================

alter table public.products enable row level security;
alter table public.product_variants enable row level security;
alter table public.products force row level security;
alter table public.product_variants force row level security;

drop policy if exists products_backend_all on public.products;
drop policy if exists products_member_select on public.products;
drop policy if exists products_owner_write on public.products;
drop policy if exists product_variants_backend_all on public.product_variants;
drop policy if exists product_variants_member_select on public.product_variants;
drop policy if exists product_variants_owner_write on public.product_variants;

create policy products_backend_all on public.products
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

create policy products_member_select on public.products
  for select to authenticated
  using (app.is_tenant_member(tenant_id));

create policy products_owner_write on public.products
  for all to authenticated
  using (app.is_tenant_owner(tenant_id))
  with check (app.is_tenant_owner(tenant_id));

create policy product_variants_backend_all on public.product_variants
  for all to app_backend
  using (tenant_id = app.current_tenant_id())
  with check (tenant_id = app.current_tenant_id());

create policy product_variants_member_select on public.product_variants
  for select to authenticated
  using (app.is_tenant_member(tenant_id));

create policy product_variants_owner_write on public.product_variants
  for all to authenticated
  using (app.is_tenant_owner(tenant_id))
  with check (app.is_tenant_owner(tenant_id));

grant select, insert, update, delete on public.products to app_backend, authenticated;
grant select, insert, update, delete on public.product_variants to app_backend, authenticated;
