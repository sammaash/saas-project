# SaaS Platform

A multi-tenant SaaS platform for small businesses that take orders over WhatsApp.

**Status: Phase 1 foundation plus Phase 2 catalogue schema/data-access library.** This
repository still has no Flask HTTP service, dashboard, WhatsApp runtime, order processing,
or billing implementation. See [`docs/PHASE_1_SAAS_FOUNDATION.md`](docs/PHASE_1_SAAS_FOUNDATION.md)
and [`docs/PHASE_2_CATALOGUE_STAGING.md`](docs/PHASE_2_CATALOGUE_STAGING.md).

---

## Tenancy model

One shared PostgreSQL schema, a `tenant_id` on every tenant-owned row, and PostgreSQL Row
Level Security.

```text
shared PostgreSQL schema  +  tenant_id  +  Row Level Security
```

- **Not** database-per-tenant.
- **Not** schema-per-tenant.
- **No per-tenant special cases.** Businesses are onboarded as *data* — a row in
  `public.tenants` — never as code. Every tenant follows the same path, including the first.

The platform is **platform-first**: it hosts many businesses and knows nothing about any
particular one. The first business onboarded simply happens to be the first row.

## What Phase 1 provides

| Area | Where |
|---|---|
| Schema migrations (7 tables, hardening and provisioning included) | `db/migrations/` |
| RLS policies | `db/migrations/0011_rls_policies.sql` |
| Least-privilege grants | `db/migrations/0012_grants.sql` |
| Tenant context (server-side scoping) | `saas/tenant_context.py` |
| Membership + routing helpers | `db/migrations/0010_membership_and_routing_helpers.sql` |
| Inbound WhatsApp routing abstraction | `saas/routing.py` |
| Development seed data | `db/seeds/0001_dev_seed.sql` |
| Isolation + structural tests | `tests/` |
| CI | `.github/workflows/saas-isolation.yml` |

### Tables

`tenants`, `tenant_channels`, `tenant_users`, `tenant_settings`, `plans`, `subscriptions`,
`audit_log`.

Phase 2 adds `products` and `product_variants` as tenant-owned catalogue tables. This
repository still does not contain a Flask HTTP service, dashboard, WhatsApp runtime, or
order-processing application; it remains a SaaS database/access foundation and catalogue
service library.

### Phase 2 catalogue layer

| Area | Where |
|---|---|
| Catalogue schema and forced RLS | `db/migrations/0017_catalogue.sql`, `db/migrations/0018_catalogue_rls_and_grants.sql` |
| Cindy Bakes catalogue-only seed | `db/seeds/0002_cindy_bakes_catalogue.sql` |
| Tenant-scoped catalogue data service | `saas/catalogue.py` |
| SaaS staging scope and current runtime boundary | `docs/PHASE_2_CATALOGUE_STAGING.md` |

`plans` is platform-owned and therefore has no `tenant_id`. `audit_log` is append-only and
its `tenant_id` is nullable, because platform-level events have no tenant.

### Isolation in one paragraph

Every tenant-owned table has RLS **enabled** and, with two documented exceptions, **forced**.
Two policy families exist: the backend, which establishes context server-side per
transaction via `set_config('app.tenant_id', <uuid>, true)` (transaction-local, so context
cannot leak across pooled connections); and dashboard sessions, which carry verified JWT
claims and are matched against a membership table. The design **fails closed** — with no
tenant context, a tenant-scoped query returns *no rows*, never all rows. The application
connects as a dedicated `app_backend` role that owns no table and does not hold `BYPASSRLS`;
Supabase's `service_role` key is deliberately never used, because it would bypass RLS and
make every policy decorative.

## Getting started

### 1. PostgreSQL

Any PostgreSQL 15+ (16 recommended). Docker:

```bash
docker run -d --name saas-pg \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=saas_platform \
  -p 5432:5432 postgres:16
```

No Docker? A local PostgreSQL install or unpacked PostgreSQL binaries work identically.

### 2. Requirements

```bash
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

```bash
export SAAS_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:5432/saas_platform"
export SAAS_TEST_DATABASE_URL="$SAAS_DATABASE_URL"
```

### 4. Migrate and seed

```bash
python db/run_migrations.py --include-local --seed
python db/run_migrations.py --status        # show applied / pending
```

Migrations are immutable once applied: the runner records a checksum and refuses to
continue if an applied file changes. Add a new migration instead.

`--include-local` applies `db/local/`, which gives `app_backend` a **development-only**
LOGIN so the tests can connect as the real application role. Never apply it to a shared or
production database. Reset is restricted to localhost/loopback URLs and requires explicit
confirmation. To start over:

```bash
python db/run_migrations.py --reset --i-am-sure --include-local --seed
```

### 5. Test

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: **56 tests, OK (0 skipped).** Tests skip automatically when no database is configured
for local convenience. CI requires `SAAS_TEST_DATABASE_URL`, fails if no SaaS tests execute,
and fails if any SaaS test skips.

### Bot Playground

The private platform admin dashboard includes an authenticated Bot Playground. It uses the
shared tenant-aware response engine with the selected tenant's active catalogue and saved
rules/persona/hours/configuration. Configure `OPENAI_API_KEY` server-side and optionally set
`OPENAI_MODEL` (default: `gpt-4o-mini`). The key is never sent to the browser.

The WhatsApp-style simulator also provides structured catalogue, cart, and demo checkout
actions. Product and variant IDs are revalidated against the selected tenant's active
catalogue, and prices and totals are calculated server-side. Cart and conversation context
are short-lived, tenant-scoped, and stored only in the signed admin session. Demo checkout
does not persist an order, reserve inventory, request payment, or send a WhatsApp message.
Delivery and payment methods are shown only when explicitly present in tenant configuration.
The page progressively enhances these server-rendered forms with same-origin `fetch()`
requests, optimistic customer bubbles, a real pending indicator, and in-place chat and panel
updates. The authenticated HTML form flow remains available when JavaScript is unavailable.

Playground mode connects no WhatsApp, order, or payment action adapters. The live channel
adapter is deliberately gated: the webhook accepts only verified Meta signatures, resolves
an active phone number through the tenant channel mapping, claims each Meta message ID once,
and keeps conversation history tenant/customer scoped. Outbound delivery remains disabled
unless `WHATSAPP_SEND_ENABLED=1`, the channel uses the exact
`tenants/<tenant-id>/whatsapp` credential reference, and a matching backend-only
`WHATSAPP_ACCESS_TOKEN_<TENANT_ID>` environment variable is present. No token is stored in
the database or exposed to templates/logs.

The webhook is synchronous because this repository has no durable queue or worker. It is
therefore suitable for local/test channels only until queue-backed processing is provided.
The endpoint is `/webhooks/whatsapp`; configure the Meta verification token and app secret
only for a separate registered test number. It must not be pointed at the production Cindy
Bakes number. The channel adapter calls the same `saas.bot.BotResponseEngine` rather than
implementing a second response engine.

Free-text Playground requests reuse their already authorized tenant context for response
generation and rendering; no tenant context is cached across requests. Aggregate timing logs
cover context loading, prompt construction, provider wait, response processing, and total
Playground request duration. These logs include only durations, counts, outcome, and the
configured model identifier--never prompts, customer messages, credentials, or tenant
configuration values.

## Layout

```text
db/
  migrations/   ordered, immutable schema migrations
  local/        development/CI-only role setup (never applied to production)
  seeds/        idempotent development seed data
  run_migrations.py
saas/           tenant-scoped access layer (context, routing, connection helpers)
tests/          isolation + structural test suites
docs/           Phase 1 documentation and the database design plan
```

## Security notes

- Tenant identity is **always** derived server-side. A `tenant_id` supplied by a client is
  never trusted.
- Application roles receive DML grants only — no DDL, no superuser, no `BYPASSRLS`.
- The `audit_log` is append-only: no UPDATE or DELETE policy exists for any application
  role, and no such grant is issued.
- `.env` is gitignored. No secret belongs in this repository.