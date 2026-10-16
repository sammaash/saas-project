# Cindy Bakes Onboarding — Status Report

**Date:** 16 October 2026 · **Deadline:** Saturday 17 October 2026
**Branch:** `platform-admin-dashboard` (head `0a8ad3e`)
**Environment used for verification:** staging Supabase project (`aws-0-eu-west-1`) plus a
disposable local PostgreSQL database for the full test suite.

Every claim below is backed by a command that was actually run. Nothing is marked PASS on
the strength of the Bot Playground alone, and no live WhatsApp capability is claimed.

---

## 1. Tenant provisioning status — **PASS**

The decision was to reuse the existing `cindy-bakes` tenant rather than create a second
"real" row, so there is exactly one tenant for this business.

| Evidence | Result |
|---|---|
| `tenant_onboarding verify --tenant-slug cindy-bakes` | `Cindy Bakes Delights (cindy-bakes) 11111111-1111-4111-8111-111111111111` |
| tenant status | `active` |
| active owner assigned | yes (`owner` check `ok`) |
| business profile | name *Cindy Bakes Delights*, activity *Bakery*, `Africa/Nairobi`, `KES` |

The tenant was renamed from the development seed label "Cindy Bakes" to the business name
"Cindy Bakes Delights". No application code branches on tenant name, slug or id.

**Caveat (not blocking, but must be stated):** this tenant was originally created by the
development seed (`db/seeds/0001_dev_seed.sql`) with a fixed UUID and two fake members. The
generic provisioning function (`app.platform_provision_tenant`) is what creates tenants for
future businesses, and it is unchanged and covered by tests. The dev seed is *development-only*
(guarded by `saas.allow_dev_data`) and must never be run against production.

## 2. Catalogue and business configuration — **PASS**

Loaded through the dashboard's own service layer
(`platform_admin.services.catalogue_import.preview_import` → `apply_import`), not by raw SQL,
so validation, catalogue versioning and audit records behave exactly as they do in the UI.

| Evidence | Result |
|---|---|
| dry run | `{'new': 33, 'updates': 0, 'unchanged': 0, 'availability_changes': 0}`, 0 validation errors |
| apply | `{'new': 33, ...}` |
| re-run (idempotency) | `{'new': 0, 'updates': 0, 'unchanged': 33, ...}` |
| staged catalogue | 11 products, 33 active variants |
| price resolution | `'Vanilla' at 2kg -> 4200.00 (sku CINDY-VANILLA-2KG)`, `'Rainbow' at 3kg -> 6700.00` |

Prices/variants are the documented Cindy Bakes list (Vanilla/Carrot/Lemon/Orange 2 500 /
4 200 / 5 600; White Forest, Black Forest, Caramel, Red Velvet, Chocolate 3 100 / 4 700 /
6 000; Blueberry 2 700 / 4 550 / 5 800; Rainbow 3 750 / 5 000 / 6 700).
Source of truth for the load: `db/tenants/cindy-bakes/catalogue.csv` (reviewable data, not code).

Business configuration applied (`db/tenants/cindy-bakes/settings.json`):

* rules: `deposit_rate 0.7`, `deposit_deadline_days 3`, `delivery_rate_per_km 32`
* persona: greeting/tone/name, `language: en`
* hours: Mon 08:00–18:00, Sat 09:00–16:00
* configuration: `payment_methods ['M-Pesa']`, `delivery_available true`,
  `use_catalogue_prices true`, `allow_unlisted_products false`,
  `escalate_complaints_to_human true`, `message_retention_days 90`

**Still to confirm with the client (placeholders used):** the `300` personalisation
surcharge and the `M-Pesa`-only payment list. Both are data, changeable from the dashboard.

## 3. Customer-history implementation (customers / conversations / messages) — **PASS**

* Migration `0033_customers_conversations_messages.sql` is **applied** (33 of 33 migrations).
* All three tables have RLS **enabled and forced**; `app_backend` is neither superuser nor
  `BYPASSRLS`; `authenticated` holds **no** privilege on them (Postgres refuses the query).
* Webhook persistence, the tenant-scoped service, the three admin views
  (customer list, customer detail, conversation detail) and `REQUIRED_SCHEMA` are in place.
* Backward compatibility: the pre-existing WhatsApp tables and the order/payment workflows
  are untouched; all their tests still pass.

Full suite result: **407 passed, 657 subtests passed, 0 failed** (disposable local database,
`SAAS_TEST_DATABASE_URL` pointed at it, `SAAS_REQUIRE_TEST_DATABASE=1`).

## 4. Bot Playground, orders and payments — **PASS (automated), see limitations**

| Area | Evidence |
|---|---|
| Playground routes, catalogue, cart, checkout | `tests/test_bot_playground_routes.py` — pass |
| Order creation, duplicate-request handling, per-tenant sequence | `tests/test_orders.py` — pass |
| Payment status, outstanding balance, idempotency keys | `tests/test_orders.py` — pass |
| Tool layer (`update_order_draft` end-to-end through the real admin) | `tests/test_saas_catalogue.py::...prices_from_the_catalogue` — pass |
| Webhook signature verification, retries, provider failure, dedup | `tests/test_whatsapp_webhook.py` — pass |
| Tenant isolation and unauthorised access | `tests/test_saas_isolation.py`, `tests/test_customers.py` — pass |

**Limitation:** these are automated tests against a real PostgreSQL database with mocked
external providers. No real Meta/WhatsApp provider call and no real M-Pesa STK push was made.
## 5. Migrations, RLS and database-role verification — **PASS**

| Check | Evidence |
|---|---|
| Migrations applied | 33, `0001_extensions_and_helpers.sql` … `0033_customers_conversations_messages.sql` |
| `0033` present | yes |
| RLS enabled + forced on `customers`, `conversations`, `messages` | yes (from `pg_class`) |
| `app_backend` | not superuser, not `BYPASSRLS`, owns no table |
| `saas_platform_admin` | no table privileges; only `app.platform_*` functions |
| `authenticated` | refused on `customers`/`conversations`/`messages` (no grant) |
| Backend without tenant context | sees 0 rows (fails closed) |
| RLS scoping | proven per tenant in `tests/test_saas_isolation.py` |

## 6. WhatsApp integration status — **BLOCKED**

| Item | Status |
|---|---|
| Inbound webhook route, signature verification, idempotent message claim | implemented, tested against a mock provider |
| Outbound delivery | gated off: `WHATSAPP_SEND_ENABLED` unset |
| Channel row for this tenant | **still the development fixture**: `phone_number_id dev-phone-id-cindy-001`, `waba_id dev-waba-001`, display `+254700000001` |
| Real `phone_number_id` / WABA id / display number | **not supplied** |
| `WHATSAPP_ACCESS_TOKEN_<TENANT_ID>` | **not present** in the environment |
| `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` | **not set** |
| Registered test number for webhook verification | **not supplied** |

The channel row exists (so the readiness `channel` check is `ok`) but it points at the
development placeholder from the seed. It must not be presented as a working WhatsApp
channel, and the production number must not be pointed at the staging webhook.

**Blocking questions for the client:**

1. The real WhatsApp Cloud API `phone_number_id` and WABA id for Cindy Bakes.
2. Confirmation of the display number in E.164.
3. A separate registered test number (its own verify token and app secret) to verify the
   webhook end to end, so the live number is never disturbed.
4. Whether outbound sending should be enabled at launch, and the token to hold out of band.
## 7. Deployment, rollback and next actions

| Item | Status |
|---|---|
| Code pushed | `f88151c`, `df4f8b5`, `a0de28f`, `0a8ad3e` on `origin/platform-admin-dashboard` |
| Railway service branch | **unconfirmed** — no `railway.json`/`railway.toml` in the repo, only a `Procfile`; the service may track `main`, in which case nothing is deployed |
| Production database state | **not inspected** (no production credentials, and none were assumed) |
| Live webhook / registered number / production credentials | **not touched** |

**Rollback plan (staging only):**

1. Application: redeploy the previous Railway deployment, or `git revert` the offending
   commit. The app is stateless apart from the database.
2. Catalogue: re-upload the previous workbook, or delete the tenant's products. Imports are
   versioned and audited.
3. Settings: re-apply an edited `db/tenants/cindy-bakes/settings.json`, or restore the
   previous document from `audit_log`.
4. Migration `0033` is additive (`create table`). Its rollback would be
   `drop table public.messages, public.conversations, public.customers;` and the
   `orders.customer_id` column — not recommended while a dashboard build that expects those
   tables is live.

**No production change of any kind was made, so no production rollback is required.**

## 8. Defects found and fixed during this work

1. **Catalogue price resolution was broken (product defect).**
   `TenantAdmin.lookup_price` compared `str(product["id"])` with the `uuid.UUID` returned for
   `variant["product_id"]`, so the comparison never matched. Both the requested-weight match
   and the documented "first active variant" fallback failed, and every catalogue
   add-to-cart returned *"Could not find '<product>' at <n>kg in the catalogue."*
   Fixed by normalising both sides with `str()`, with regression tests for all 33 weight
   variants, case-insensitivity, tenant scoping, the name fallback the import path relies on,
   and an end-to-end `update_order_draft` run.
2. **Isolation suite asked the dashboard role to read backend-only tables.**
   Migrations 0032/0033 grant `orders`, `order_items`, `order_payments`,
   `tenant_order_sequences`, `customers`, `conversations` and `messages` to `app_backend`
   alone, but they were never added to `AUTHENTICATED_INACCESSIBLE_TABLES`, so the suite
   failed with `permission denied` (42 failures / 214 errors) instead of asserting the
   refusal. Fixed in `tests/saas_test_support.py`.
3. **Customer test fixtures were written through a rolled-back connection**, so the second
   connection correctly saw nothing and the test misreported the application.
   Fixed with an explicitly committed fixture helper.
## 9. Incident disclosure (staging)

While running the full suite, `.env` pointed `SAAS_DATABASE_URL` at **staging**, and
`saas.db.connect()` reads that variable rather than `SAAS_TEST_DATABASE_URL`. One test
(`CustomerServiceDbTests.test_get_or_create_is_idempotent`) therefore created a fixture
customer row (`wa_id +254733333333`, name `Idem`) in the real `cindy-bakes` tenant before
deleting it in its `finally` block. A leftover row was found on staging and has been
**deleted**; the tenant now holds 0 customers, 0 conversations, 0 messages and 0 orders.

Two hardening actions were taken as a result:

* the test now pins `SAAS_DATABASE_URL` to the test database for the duration of the call;
* the suite must be run with `SAAS_TEST_DATABASE_URL` set and `SAAS_REQUIRE_TEST_DATABASE=1`.

Separately, Supabase's pooler temporarily blocked new connections
(`ECIRCUITBREAKER: too many authentication failures`) after connection attempts with stale
credentials in `.env`. The stale `SAAS_OPERATOR_DATABASE_URL` and
`SAAS_SUPABASE_MIGRATION_DATABASE_URL` passwords should be rotated and corrected.

## 10. Real-provider tests: performed vs not performed

| Performed | Not performed |
|---|---|
| Webhook signature verification, replay/idempotency and provider-failure paths (mocked) | Any call to the real Meta WhatsApp Cloud API |
| Catalogue import, price lookup, cart, checkout and order/payment flows against a real PostgreSQL database | Any M-Pesa / Daraja STK push |
| RLS isolation, unauthorised access and readiness checks | Webhook delivery from a real registered test number |
| Outbound-send gating logic | Any message sent to a real customer |

## 11. How to reproduce

```bash
# Disposable local database (never point tests at staging)
createdb saas_cindy_verify
python db/run_migrations.py --database-url postgresql://postgres:postgres@127.0.0.1:5432/saas_cindy_verify \
    --include-local --seed
SAAS_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/saas_cindy_verify \
SAAS_REQUIRE_TEST_DATABASE=1 python -m pytest tests -q

# Staging onboarding (idempotent; run --dry-run first)
python db/tenant_onboarding.py --admin-user-id <verified-admin-uuid> \
    catalogue --tenant-slug cindy-bakes --csv db/tenants/cindy-bakes/catalogue.csv --dry-run
python db/tenant_onboarding.py --admin-user-id <verified-admin-uuid> \
    catalogue --tenant-slug cindy-bakes --csv db/tenants/cindy-bakes/catalogue.csv
python db/tenant_onboarding.py --admin-user-id <verified-admin-uuid> \
    settings  --tenant-slug cindy-bakes --json db/tenants/cindy-bakes/settings.json
python db/tenant_onboarding.py --admin-user-id <verified-admin-uuid> \
    verify    --tenant-slug cindy-bakes --lookup 'Vanilla:2'
```

The administrator UUID must be the verified Supabase Auth `sub` (an active row in
`public.platform_admins`); the database re-checks it before writing any audit event.

## 12. Summary

| Capability | Verdict |
|---|---|
| Tenant provisioning (reuse, no duplicate) | **PASS** |
| Catalogue load and variant/price resolution | **PASS** |
| Business configuration (identity, persona, rules, hours, settings) | **PASS** |
| Admin access / ownership | **PASS** |
| Customer-history implementation (0033, isolation, webhook, dashboard) | **PASS** |
| Bot Playground, orders and payments (automated) | **PASS** |
| Migrations, RLS and database roles | **PASS** |
| Test suite | **PASS** — 407 passed, 657 subtests, 0 failed |
| Real WhatsApp channel (identifiers, credentials, registered test number) | **BLOCKED** |
| Railway deployment confirmation | **BLOCKED** — service branch unconfirmed |
| Production migration and live-number verification | **NOT ATTEMPTED** — requires explicit authorisation |