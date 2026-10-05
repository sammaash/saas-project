# Phase 2 Catalogue on the SaaS Foundation

**Scope:** SaaS database/schema and catalogue-access library only. This branch is based on the SaaS repository's Phase 1 commit `e9c5911`; it is not based on the Cindy Bakes application repository. No Railway deployment has occurred.

## Migration numbering

The SaaS repository already uses migrations 0013–0016 for Phase 1 security hardening, active membership, platform provisioning, and sequence privileges. Therefore, the Cindy application repository's catalogue migration filenames 0013/0014 cannot be copied verbatim here. This branch uses:

1. Existing Phase 1 migrations `0001`–`0016`.
2. `0017_catalogue.sql` for tenant-owned products and variants.
3. `0018_catalogue_rls_and_grants.sql` for forced RLS and grants.

Do not mix migration sets from the two repositories or renumber already-applied migrations. Check `public.saas_schema_migrations` and checksums in the target database before applying anything.

## Seed boundaries and tenant bootstrap

`0001_dev_seed.sql` is the renamed existing development fixture. It inserts Cindy Bakes and **Test Bakery**, fake channels, placeholder memberships/settings, and other test records. It exists for local integration tests only. Do not run `python db/run_migrations.py --seed` against a staging or production-like SaaS database because that command runs every seed file and creates development data.

`0002_cindy_bakes_catalogue.sql` looks up an existing tenant by `slug = 'cindy-bakes'`; it does not create a tenant. It inserts 11 products and 33 variants idempotently. Apply it only after the tenant has been provisioned and verified.

Before provisioning, obtain a real staging Auth user UUID:

- In a Supabase staging project, create/invite the intended staging operator through Authentication → Users (or its authenticated admin provisioning flow).
- Copy the immutable user `id`/UID from that staging project's user record; do not invent it, use a local fixture, or copy a production Auth ID across projects.
- Confirm the human/account is the intended staging owner, that sign-in succeeds, and that the verified staging JWT's `sub` equals that user UUID. Do not paste or log bearer tokens.
- If another identity provider is selected, verify its trusted issuer and stable subject claim before using that UUID.

The expected Cindy tenant identity is `11111111-1111-4111-8111-111111111111` / `cindy-bakes`. The existing `app.provision_tenant()` function safely creates a tenant, owner, settings, optional subscription, and audit event atomically, but it generates the tenant UUID and does not accept a requested ID. If the fixed Cindy UUID is a requirement, use a reviewed, restricted DBA transaction to insert that exact ID, the verified Auth UUID as active owner, required settings, and an audit event. Check for either UUID or slug conflicts first and abort on any mismatch; do not create a duplicate. The runtime in this repository does not yet resolve a production tenant from a configured slug/ID.

After tenant bootstrap, apply only `db/seeds/0002_cindy_bakes_catalogue.sql` in a transaction. Verify it produces 11 products and 33 variants for Cindy Bakes, that all prices match the approved catalogue, and that other tenants have no products unless explicitly provisioned.

## Database verification

On the target database verify:

- Migrations `0001`–`0018` have the expected checksums and no pending migrations.
- `products` and `product_variants` both have RLS enabled and forced.
- `app_backend` is the runtime login, is not a table owner, is not superuser or `BYPASSRLS`, and is not a member of an elevated or `service_role`-style role. The Python connection helper now rejects any other session/current role.
- The intended tenant UUID, slug, active owner membership, and settings are correct.
- Catalogue counts, active states, and the complete product/variant/price mapping match the approved seed.

Run database-backed SaaS tests only against a disposable test database containing the fixtures, with `SAAS_TEST_DATABASE_URL` configured. Do not load the development fixture into a staging database just to satisfy tests.

## Runtime boundary and missing work

This SaaS repository still has no Flask application factory, HTTP routes, web server entrypoint (`Procfile`/Railway start command), login/API layer, or SaaS dashboard. `saas/catalogue.py` is a tenant-scoped data-access library, not a runnable service. The Cindy Bakes app's `agent.py`, `whatsapp_webhook.py`, `whatsapp_agent_service.py`, `database.py`, `order.py`, `invoice.py`, admin routes/dashboard, and frontend are application-specific runtime files and are intentionally not copied into this SaaS branch.

The Phase 2 catalogue integration currently exists only in the Cindy Bakes application: it selects a catalogue service when `SAAS_DATABASE_URL` is set, uses catalogue product/variant IDs and price snapshots, but continues writing orders/conversation state to that application's SQLite database. This is not implemented as a standalone SaaS service here. Before creating a Railway web service, the product/API runtime, authentication/tenant selection, order persistence, and service start command need an explicit design and implementation review. Do not solve that gap by deploying the Cindy Bakes app wholesale or connecting its live WhatsApp number.

## Local validation

A clean local database can apply all migrations and development fixtures with `--include-local --seed`, but that command deliberately creates Test Bakery and fake channels. For a staging-like database, apply migrations without `--seed`, bootstrap the staging tenant with a verified owner, then run only the Cindy catalogue seed. The complete database test suite should use a separate disposable test database with the standard fixtures.
