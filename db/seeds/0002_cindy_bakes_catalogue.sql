-- ============================================================================
-- Phase 2 -- Cindy Bakes (Tenant 001) catalogue seed
-- ============================================================================
-- This script looks up the existing tenant by slug. It never inserts a tenant.

with cindy as (
  select id from public.tenants where slug = 'cindy-bakes'
), products_to_seed(name, category, sort_order) as (
  values
    ('Vanilla', 'standard-cake', 10), ('Carrot', 'standard-cake', 20),
    ('Lemon', 'standard-cake', 30), ('Orange', 'standard-cake', 40),
    ('White Forest', 'standard-cake', 50), ('Black Forest', 'standard-cake', 60),
    ('Blueberry', 'standard-cake', 70), ('Caramel', 'standard-cake', 80),
    ('Rainbow', 'standard-cake', 90), ('Red Velvet', 'standard-cake', 100),
    ('Chocolate', 'standard-cake', 110)
)
insert into public.products (tenant_id, name, category, sort_order)
select cindy.id, products_to_seed.name, products_to_seed.category, products_to_seed.sort_order
from cindy cross join products_to_seed
on conflict (tenant_id, name_normalized) do nothing;

with cindy_products as (
  select p.id, p.tenant_id, p.name
  from public.products p
  join public.tenants t on t.id = p.tenant_id
  where t.slug = 'cindy-bakes'
), prices(product_name, code, variant_name, price, weight_kg, sort_order) as (
  values
    ('Vanilla','1kg','1 kg',2500.00,1,10),('Vanilla','2kg','2 kg',4200.00,2,20),('Vanilla','3kg','3 kg',5600.00,3,30),
    ('Carrot','1kg','1 kg',2500.00,1,10),('Carrot','2kg','2 kg',4200.00,2,20),('Carrot','3kg','3 kg',5600.00,3,30),
    ('Lemon','1kg','1 kg',2500.00,1,10),('Lemon','2kg','2 kg',4200.00,2,20),('Lemon','3kg','3 kg',5600.00,3,30),
    ('Orange','1kg','1 kg',2500.00,1,10),('Orange','2kg','2 kg',4200.00,2,20),('Orange','3kg','3 kg',5600.00,3,30),
    ('White Forest','1kg','1 kg',3100.00,1,10),('White Forest','2kg','2 kg',4700.00,2,20),('White Forest','3kg','3 kg',6000.00,3,30),
    ('Black Forest','1kg','1 kg',3100.00,1,10),('Black Forest','2kg','2 kg',4700.00,2,20),('Black Forest','3kg','3 kg',6000.00,3,30),
    ('Blueberry','1kg','1 kg',2700.00,1,10),('Blueberry','2kg','2 kg',4550.00,2,20),('Blueberry','3kg','3 kg',5800.00,3,30),
    ('Caramel','1kg','1 kg',3100.00,1,10),('Caramel','2kg','2 kg',4700.00,2,20),('Caramel','3kg','3 kg',6000.00,3,30),
    ('Rainbow','1kg','1 kg',3750.00,1,10),('Rainbow','2kg','2 kg',5000.00,2,20),('Rainbow','3kg','3 kg',6700.00,3,30),
    ('Red Velvet','1kg','1 kg',3100.00,1,10),('Red Velvet','2kg','2 kg',4700.00,2,20),('Red Velvet','3kg','3 kg',6000.00,3,30),
    ('Chocolate','1kg','1 kg',3100.00,1,10),('Chocolate','2kg','2 kg',4700.00,2,20),('Chocolate','3kg','3 kg',6000.00,3,30)
)
insert into public.product_variants (tenant_id, product_id, code, name, price, attributes, sort_order)
select p.tenant_id, p.id, prices.code, prices.variant_name, prices.price,
       jsonb_build_object('weight_kg', prices.weight_kg), prices.sort_order
from cindy_products p join prices on prices.product_name = p.name
on conflict (tenant_id, product_id, code) do nothing;
