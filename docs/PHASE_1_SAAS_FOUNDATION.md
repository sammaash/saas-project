# SaaS Foundation — Phase 1 Core and Phase 2 Catalogue

**Scope:** Phase 1 tenancy/security foundations plus the Phase 2 tenant-owned catalogue schema
and database access library. No customer, conversation, message, order, payment or usage
tables. No Flask HTTP service, dashboard, WhatsApp runtime, billing or STK push. §14 records
what was excluded from Phase 1 itself.

**Tenancy model:** one shared PostgreSQL schema + `tenant_id` + Row Level Security. Not
database-per-tenant. Not schema-per-tenant. There is no per-tenant legacy mode and no
special case: the first business onboarded is a row like any other.

---

## Repository context

This repository contains the independent, platform-first SaaS foundation. Its development
seed includes synthetic fixture data for two tenants; it does not migrate or manage any
external application's database or production records.

---

## 1. Files created

### Migrations (`db/migrations/`, applied in file-name order)

| File | Contents |
|---|---|
| `0001_extensions_and_helpers.sql` | `app` schema; `app.set_updated_at()`; `app.current_tenant_id()`; `app.current_user_id()` |
| `0002_roles.sql` | `app_backend` (NOLOGIN), `authenticated`, `anon`; asserts `app_backend` cannot bypass RLS |
| `0003_tenants.sql` | `tenants` |
| `0004_tenant_channels.sql` | `tenant_channels` (globally unique `phone_number_id`) |
| `0005_tenant_users.sql` | `tenant_users` (roles `owner` / `staff`) |
| `0006_tenant_settings.sql` | `tenant_settings` (JSONB configuration) |
| `0007_plans.sql` | `plans` (platform-owned, no `tenant_id`) |
| `0008_subscriptions.sql` | `subscriptions` (one live subscription per tenant) |
| `0009_audit_log.sql` | `audit_log` (append-only, nullable `tenant_id`) |
| `0010_membership_and_routing_helpers.sql` | `app.is_tenant_member()`, `app.is_tenant_owner()`, `app.resolve_tenant_by_phone_number_id()` |
| `0011_rls_policies.sql` | ENABLE / FORCE RLS and every policy |
| `0012_grants.sql` | Least-privilege grants; append-only enforcement |
| `0013_phase1_security_hardening.sql` | Trusted platform provisioning capability, owner protection, inactive-plan lookup, and subscription plan index |
| `0014_active_membership_and_owner_lock.sql` | Active-only membership predicates and serialized owner removal |
| `0015_platform_provisioning_login.sql` | Dedicated platform API role (credential configured out of band) |
| `0016_provisioner_schema_usage.sql` | Explicit schema access for the provisioning function owner |
| `0017_catalogue.sql` | Tenant-owned products and product variants |
| `0018_catalogue_rls_and_grants.sql` | Forced RLS and least-privilege catalogue grants |

### Local / development (`db/local/`, never applied on Supabase)

| File | Contents |
|---|---|
| `0001_local_app_backend_login.sql` | Adds `LOGIN` + a throwaway dev password to `app_backend` so the tests can connect as the real application role |

### Seed data (`db/seeds/`)

| File | Contents |
|---|---|
| `0001_dev_seed.sql` | Tenant 001 Cindy Bakes, Tenant 002 Test Bakery, channels, memberships, settings, plan, subscriptions, audit rows |
| `0002_cindy_bakes_catalogue.sql` | Cindy Bakes products and variants only; looks up the existing tenant by slug |

### Tooling, application layer and tests

| File | Contents |
|---|---|
| `db/run_migrations.py` | Migration runner (`--include-local`, `--seed`, `--status`; confirmed localhost-only `--reset`) |
| `saas/__init__.py` | Package marker + scope note |
| `saas/config.py` | Environment configuration |
| `saas/db.py` | PostgreSQL connections; enforces the non-privileged `app_backend` identity |
| `saas/tenant_context.py` | **Tenant context implementation** |
| `saas/routing.py` | `phone_number_id` → `tenant_id` routing |
| `requirements.txt` | `psycopg[binary]` — the PostgreSQL driver |
| `tests/saas_test_support.py` | Fixtures, identity-switching helpers, base test case |
| `tests/test_saas_isolation.py` | **Isolation and provisioning test suite** |
| `tests/test_saas_schema_structure.py` | **Structural tests** (requirements 9–11) |
| `tests/test_saas_migration_runner.py` | Reset safety and migration checksum tests |
| `.github/workflows/saas-isolation.yml` | CI: Postgres service container → migrate → seed → test |
| `docs/PHASE_1_SAAS_FOUNDATION.md` | This document |

## 2. Scope boundary

This repository contains database migrations, SaaS connection/context/routing helpers,
the tenant-aware catalogue data service, development seed data, tests, and CI. It does not
contain a dashboard, Flask HTTP service, WhatsApp runtime, or customer, conversation, order,
payment, or usage implementation.

## 3. Database migration files

Eighteen migrations, applied in order by `db/run_migrations.py`. They use **only standard
PostgreSQL**: no `auth` schema references, no Supabase-specific extensions, no shims. The
same files run unchanged on local PostgreSQL, in CI and on Supabase.

Applied versions and checksums are recorded in `public.saas_schema_migrations`. A migration
that has already been applied is skipped; if a file's contents change after being applied,
the runner **refuses to continue** and tells you to add a new migration instead. Migrations
are therefore immutable once applied, which is what keeps local and production schemas
comparable.

**Conventions followed:** UUID primary keys for the normal/low-volume entities
(`tenants`, `tenant_channels`, `tenant_users`, `tenant_settings`, `plans`, `subscriptions`)
and `bigint generated always as identity` for the append-only `audit_log`; `numeric(12,2)`
for money; `timestamptz` throughout; `text` + `CHECK` instead of native enums; JSONB only
for configuration blobs; `KES` stored explicitly.

**One deliberate exception to UUID keys:** `audit_log` uses `bigint identity` because it is
the high-volume append-only table, where narrower index entries and sequential inserts
matter more than non-guessable ids.

## 4. Tenant context implementation

`saas/tenant_context.py`. The application never accepts a tenant id from a client. It
derives the tenant itself and then scopes all work:

```python
from saas.tenant_context import tenant_transaction

with tenant_transaction(tenant_id) as cursor:      # tenant_id came from the server
    cursor.execute("select * from public.orders ...")   # automatically confined to the tenant
```

Under the hood this issues, inside a transaction:

```sql
select set_config('app.tenant_id', <uuid>, true)
```

Three deliberate decisions:

1. **`set_config(..., true)` rather than `SET LOCAL`.** `SET LOCAL app.tenant_id = '...'`
   cannot take a bound parameter, so the uuid would have to be interpolated into SQL. The
   function form is parameterised, and the third argument `true` is what makes the setting
   **transaction-local** — the exact property that stops one tenant's context leaking into
   the next request on a pooled connection.
2. **An empty `tenant_id` raises immediately.** Running without context returns zero rows,
   which looks like "no data" rather than "this path forgot the tenant". Mid-order, a loud
   error is far safer than a silent empty result.
3. **No context means no rows, never all rows.** `app.current_tenant_id()` returns NULL when
   unset, and any `tenant_id = NULL` comparison is NULL, which matches nothing. That is the
   fail-closed guarantee, and it is asserted by tests 8 and 8b.

## 5. RLS implementation

`db/migrations/0011_rls_policies.sql`. Every Phase 1 table has RLS enabled. Deny by default:
with RLS on and no matching policy, a command is refused.

**Two policy families**, because the two access paths answer "who is this?" differently:

| | Family A — `app_backend` (backend, bot, workers) | Family B — `authenticated` (dashboard JWT) |
|---|---|---|
| Who | The Flask service | A logged-in human |
| Context | `set_config('app.tenant_id', <uuid>, true)` | `request.jwt.claims->>'sub'` |
| Predicate | `tenant_id = app.current_tenant_id()` | `app.is_tenant_member(tenant_id)` |
| Writes | `tenant_id = app.current_tenant_id()` (WITH CHECK) | `app.is_tenant_owner(tenant_id)` (WITH CHECK) |

Both `USING` and `WITH CHECK` are used throughout. `USING` decides which rows are visible or
affected; `WITH CHECK` decides what may be **written**. Without `WITH CHECK`, tenant A could
insert rows or move them into tenant B — a write-side poisoning path, not just a read leak.

`ENABLE` **and** `FORCE` are applied, with two documented exceptions:

| Table | RLS enabled | FORCE | Why |
|---|---|---|---|
| `tenants` | ✅ | ✅ | — |
| `tenant_settings` | ✅ | ✅ | — |
| `plans` | ✅ | ✅ | — |
| `subscriptions` | ✅ | ✅ | — |
| `audit_log` | ✅ | ✅ | — |
| `tenant_channels` | ✅ | ❌ | Read by `app.resolve_tenant_by_phone_number_id()` |
| `tenant_users` | ✅ | ❌ | Read by `app.is_tenant_member()` / `app.is_tenant_owner()` |

**Why those two exceptions exist, and why they are safe:** an RLS policy must never query the
table it protects (PostgreSQL would re-evaluate the same policy and recurse), so the helpers
in migration 0010 are `SECURITY DEFINER` and read those two tables with the owner's
privileges. `FORCE` would subject the owner to RLS too and break them. `FORCE` only changes
the behaviour of the **table owner**, and the application never connects as the owner —
`app_backend` and `authenticated` are ordinary roles. The suite proves this directly
(`test_not_forced_tables_are_still_filtered_for_application_roles`).

**Why not service_role:** Supabase's `service_role` key **bypasses RLS entirely**. If the
backend used it, every policy above would be decorative. Hence a dedicated `app_backend`
role that is not a superuser, does not own any table and does not hold `BYPASSRLS`
(asserted by `test_app_backend_cannot_bypass_rls`). It receives DML grants only — no DDL.

**Append-only audit trail**, enforced three independent ways: no UPDATE/DELETE policy exists
(RLS denies by default); no UPDATE/DELETE grant is issued to any application role; and there
is no `updated_at` column.

## 6. Membership helper / function

`db/migrations/0010_membership_and_routing_helpers.sql`. Three functions, all
`SECURITY DEFINER STABLE` with a pinned `search_path`:

| Function | Returns | Used by |
|---|---|---|
| `app.is_tenant_member(target_tenant_id uuid)` | boolean | Family B read policies |
| `app.is_tenant_owner(target_tenant_id uuid)` | boolean | Family B write policies |
| `app.resolve_tenant_by_phone_number_id(text)` | uuid | WhatsApp routing (§11) |

`SECURITY DEFINER` is what lets a policy check membership without either recursing into
`tenant_users`' own policy or granting the application broad read access. `STABLE` is what
lets the planner evaluate the membership check once per query instead of once per row —
without it, a policy predicate can turn an indexed lookup into a sequential scan.

`EXECUTE` is revoked from `PUBLIC` and then granted narrowly:

| Function | `app_backend` | `authenticated` |
|---|---|---|
| `is_tenant_member` | ✅ | ✅ |
| `is_tenant_owner` | ✅ | ✅ |
| `resolve_tenant_by_phone_number_id` | ✅ | ❌ |

The routing resolver is deliberately **not** available to dashboard sessions: a browser never
resolves a tenant from a phone number.
`test_routing_function_is_not_available_to_dashboard_sessions` asserts that.

## 7. Seed data

`db/seeds/0001_dev_seed.sql` — idempotent, deterministic UUIDs, obviously fake identifiers.
`db/seeds/0002_cindy_bakes_catalogue.sql` — idempotent, Cindy Bakes catalogue only.

| | Tenant 001 | Tenant 002 |
|---|---|---|
| Name | **Cindy Bakes** | **Test Bakery** |
| UUID | `11111111-1111-4111-8111-111111111111` | `22222222-2222-4222-8222-222222222222` |
| Slug | `cindy-bakes` | `test-bakery` |
| Channel | `dev-phone-id-cindy-001`, `dev-waba-001`, `+254700000001` | `dev-phone-id-testbakery-002`, `dev-waba-002`, `+254700000002` |
| Members | owner `aaaaaaaa-…0001` + staff `aaaaaaaa-…0002` | owner `bbbbbbbb-…0001` |
| Settings | `Africa/Nairobi`, `KES`, 70% deposit, 3-day deadline, KSh 32/km | `Africa/Nairobi`, `KES`, 50% deposit, 2-day deadline, KSh 30/km |
| Subscription | `active`, `manual` | `trial`, `manual` |

Plus one `Starter` plan (platform-owned) and two audit rows: one tenant-scoped
(`tenant.seeded`) and one **platform-level with `tenant_id = NULL`**
(`seed.platform_bootstrap`), so the tests can prove platform rows are invisible to tenants.

**Test Bakery is not connected to a real WhatsApp number.** Its `phone_number_id` and
`display_phone_number` are clearly fake development values (`+254700000002` is not an
assignable number), so there is no risk of routing live traffic to it even if these values
were mistakenly used. No real credential, token or phone number appears anywhere in the seed.
`credentials_ref` holds a pointer such as `dev/tenants/test-bakery/whatsapp`, never a secret.

The two tenants are deliberately **symmetric**, so isolation tests cannot pass by accident.

## 8. Isolation test suite

`tests/test_saas_isolation.py` and `tests/test_saas_schema_structure.py`, built on
`tests/saas_test_support.py`. Test framework: **stdlib `unittest`**, matching the existing
`tests/test_agent.py` and `tests/test_order.py`. No new test dependency.

**How identity is simulated.** Each test connects as the migration role and then switches
identity *inside a transaction*, exactly as the platform does at runtime:

| Acting as | How |
|---|---|
| Backend / bot | `SET LOCAL ROLE app_backend` + `set_config('app.tenant_id', <uuid>, true)` |
| Dashboard user | `SET LOCAL ROLE authenticated` + `set_config('request.jwt.claims', '{"sub":"<user id>"}', true)` |

The second form is precisely what Supabase's PostgREST does for a dashboard request, so these
tests exercise the real RLS path rather than a simulation of it. Every helper rolls its
transaction back, so the suite never mutates seeded data.

**Required assertions 1–8** (`TenantIsolationTests`):

| # | Requirement | Test |
|---|---|---|
| 1 | Tenant 001 reads its own records | `test_1_tenant_001_can_read_its_own_records` |
| 2 | Tenant 002 reads its own records | `test_2_tenant_002_can_read_its_own_records` |
| 3 | Tenant 001 cannot read Tenant 002 | `test_3_tenant_001_cannot_read_tenant_002_records` |
| 4 | Tenant 002 cannot read Tenant 001 | `test_4_tenant_002_cannot_read_tenant_001_records` |
| 5 | Tenant 001 cannot update Tenant 002 | `test_5_tenant_001_cannot_update_tenant_002_records` |
| 6 | Tenant 001 cannot delete Tenant 002 | `test_6_tenant_001_cannot_delete_tenant_002_records` |
| 7 | Tenant 001 cannot insert as Tenant 002 | `test_7_tenant_001_cannot_insert_a_row_claiming_tenant_002` |
| 8 | No context exposes no tenant rows | `test_8_query_without_tenant_context_exposes_no_tenant_rows`, `test_8b_backend_cannot_write_without_tenant_context` |

Tests 3–8 iterate over **every** tenant-scoped table from one table-driven list, so a table
added later without isolation coverage fails the suite rather than slipping through.

**Required assertions 9–11** (`tests/test_saas_schema_structure.py`):

| # | Requirement | Test |
|---|---|---|
| 9 | RLS enabled on every Phase 1 table | `test_rls_is_enabled_on_every_phase_1_table` |
| 10 | RLS forced where required | `test_rls_is_forced_where_required`, `test_not_forced_tables_are_still_filtered_for_application_roles` |
| 11 | `tenant_id` exists and is NOT NULL | `test_tenant_id_exists_and_is_not_null_on_every_tenant_owned_table`, `test_only_the_documented_exception_allows_a_null_tenant_id` |

**Additional coverage** beyond the required eleven:

- Owner vs staff behaviour (`test_staff_member_can_read_but_not_write`).
- An unrelated authenticated user sees nothing (`test_outsider_sees_nothing_at_all`).
- Backend context confines the backend to exactly one tenant.
- Routing: own numbers resolve; unknown and disabled numbers return NULL; routing is identity
  not authorisation; the resolver is unavailable to dashboard sessions.
- Constraints: `phone_number_id` globally unique; one live subscription per tenant; a
  cancelled subscription does not block a new one.
- Audit log: append works in context; platform-level insert is refused; UPDATE/DELETE refused
  for both application roles; platform rows invisible to tenants.
- Tenant context: `set_config(..., true)` does not outlive its transaction; an empty tenant id
  raises; an end-to-end query as the real `app_backend` role works.
- Grants: `app_backend` is neither superuser nor `BYPASSRLS`; no UPDATE/DELETE grant on
  append-only tables; `anon` has nothing; `tenants` is not writable by dashboard sessions.

Run the full Phase 1 suite against a migrated and seeded PostgreSQL database. The test
count grows with coverage; CI verifies at least one SaaS test executes and rejects skipped
SaaS tests.

## 9. Instructions for running the migrations locally

Two options. **Option A** needs no Docker; on this machine I used exactly these steps.

### Option A — local PostgreSQL binaries (no Docker)

```powershell
# 1. Get PostgreSQL (16+). Either install it normally, or unpack the zip binaries:
#    https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64-binaries.zip

$pg = "<extract-dir>\pgsql\bin"

# 2. Initialise a throwaway data directory (do this once)
& "$pg\initdb.exe" -D "$env:TEMP\pgdata-saas" -U postgres -A trust -E UTF8

# 3. Start it (any free port; 5433 avoids clashing with an existing 5432)
& "$pg\pg_ctl.exe" -D "$env:TEMP\pgdata-saas" -o "-p 5433" -l "$env:TEMP\pglog.txt" start

# 4. Create the database
& "$pg\psql.exe" -U postgres -p 5433 -h 127.0.0.1 -c "create database saas_platform"
```

### Option B — Docker

```powershell
docker run -d --name saas-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=saas_platform -p 5433:5432 postgres:16
```

### Then, for either option

```powershell
# 5. Install the PostgreSQL driver
pip install -r requirements.txt

# 6. Point the runner at the database
$env:SAAS_DATABASE_URL      = "postgresql://postgres@127.0.0.1:5433/saas_platform"
$env:SAAS_TEST_DATABASE_URL = $env:SAAS_DATABASE_URL

# 7. Apply migrations, the dev-only app role, and the seed data
python db/run_migrations.py --include-local --seed

# 8. Check what is applied
python db/run_migrations.py --status
```

Expected output of step 7:

```text
apply  0001_extensions_and_helpers.sql
...
apply  0018_catalogue_rls_and_grants.sql
local  0001_local_app_backend_login.sql
seed   0001_dev_seed.sql
seed   0002_cindy_bakes_catalogue.sql
migrations complete
```

**Re-running is safe.** Migrations already applied are reported as
`skip  <file> (already applied)`; `db/local/` and `db/seeds/` scripts are idempotent and
re-applied each time.

To start over from scratch:

```powershell
python db/run_migrations.py --reset --i-am-sure --include-local --seed    # localhost only
```

> `--reset` drops the `public` and `app` schemas. It requires `--i-am-sure` and is refused
> unless the database URL uses `localhost` or a loopback IP; Supabase and remote URLs are
> always rejected.

## 10. Instructions for running the isolation tests

```powershell
# Same connection string as above
$env:SAAS_DATABASE_URL      = "postgresql://postgres@127.0.0.1:5433/saas_platform"
$env:SAAS_TEST_DATABASE_URL = $env:SAAS_DATABASE_URL

# The Phase 1 isolation + structural suite
python -m unittest tests.test_saas_isolation -v
python -m unittest tests.test_saas_schema_structure -v

# Or both at once
python -m unittest discover -s tests -p "test_saas_*.py" -v
```

The suite must run against a migrated and seeded PostgreSQL database. CI fails if the SaaS
suite is not executed or any SaaS test is skipped.

To confirm the existing application is unaffected:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: the existing tests pass, and the SaaS classes report
`skipped 'No SAAS_TEST_DATABASE_URL / SAAS_DATABASE_URL configured...'` if you unset the
variables first.

**In CI** the same commands run against a `postgres:16` service container — see
`.github/workflows/saas-isolation.yml`. That workflow also runs the existing test suite as a
separate step, so a regression in either layer is caught.

## 11. Environment variables for local development

These are backend-only and must never be exposed as `VITE_*` values.

| Variable | Purpose | Example |
|---|---|---|
| `SAAS_DATABASE_URL` | Connection string for tenant-scoped backend queries | `postgresql://app_backend:password@host:5432/cindy_saas` |
| `SAAS_TEST_DATABASE_URL` | Connection string used by the isolation tests. CI requires this value; locally it falls back to `SAAS_DATABASE_URL` | `postgresql://postgres:postgres@127.0.0.1:5433/saas_platform` |

**Which role goes in `SAAS_DATABASE_URL` for a real environment:** `app_backend` — a
dedicated, non-owner role that is **subject to RLS**. Do **not** use Supabase's
`service_role` key for tenant-scoped application queries, and never put it in frontend code:
it bypasses RLS, which would make every Phase 1 policy decorative.

The existing application keeps using its own variables unchanged (`DATABASE_PATH` for SQLite,
the `WHATSAPP_*` set, `DASHBOARD_*`, `OPENAI_API_KEY`, and so on). No existing variable was
renamed, removed or repurposed.

## 12. How future WhatsApp `phone_number_id` routing will work

Target flow. **None of this is wired in Phase 1** — `whatsapp_webhook.py` is unchanged and
still handles production traffic exactly as before. Phase 1 delivers the *database model and
routing abstraction* this flow needs.

```text
WhatsApp webhook POST
        |
        v
metadata.phone_number_id                       (from the Cloud API payload)
        |
        v
app.resolve_tenant_by_phone_number_id(text)     (SECURITY DEFINER, returns one uuid)
        |
        v
tenant_id
        |
        v
tenant_transaction(tenant_id)                   (set_config, transaction-local)
        |
        v
tenant-scoped processing                        (RLS confines every query from here on)
```

**Steps in detail**

1. Meta delivers the webhook to the existing endpoint. The production verification flow
   (`hub.verify_token`, `X-Hub-Signature-256`) stays exactly as it is — Phase 1 does not
   touch it.
2. The handler reads `metadata.phone_number_id` from the payload. This is the tenant routing
   key, which is why `tenant_channels.phone_number_id` is **globally unique**.
3. `saas/routing.resolve_tenant_id_by_phone_number_id(...)` resolves it to a `tenant_id` via
   `app.resolve_tenant_by_phone_number_id()`. An unknown or disabled number returns `None`.
4. `None` means **quarantine and alert**, never "pick a tenant": the payload should be stored
   with no tenant attribution and surfaced for a human. Guessing is the one response that
   could deliver one business's customer message to another.
5. With a tenant id, `saas/tenant_context.tenant_transaction(tenant_id)` establishes context,
   and every subsequent query is confined to that tenant by RLS.
6. Tenant status (`active` / `suspended` / `inactive`) is checked **after** context is
   established, because `tenants` is FORCE-RLS'd and the resolver deliberately cannot read it.
   A suspended tenant's messages are still attributed and stored, but not answered.

**Why a `SECURITY DEFINER` function is needed at all.** An inbound webhook must identify its
tenant *before* any tenant context can exist, and RLS (correctly) blocks that lookup. It is
the only elevated read path in Phase 1, and it is deliberately narrow: it returns a single
uuid, never row data, only for an exact match on an **active** WhatsApp channel, and `EXECUTE`
is granted to `app_backend` only — not to dashboard sessions.

**Two things routing intentionally does not do:** it is not authorisation (it answers "whose
number is this?", not "may we serve them?"), and it cannot enumerate. Both are asserted in
`RoutingTests`.

**When this gets wired up** (a later phase, not now): the webhook handler calls the resolver,
starts a tenant transaction, and passes the tenant context into the bot service. The existing
SQLite write path continues in parallel until a cutover is explicitly planned.

## 13. Development data boundary

The seed creates synthetic tenant fixtures for local testing only. It contains no production
records, real phone-number identifiers, or access tokens. No customer, conversation, order,
or payment data is modeled in Phase 1.

## 14. What is deliberately NOT in Phase 1

No catalogue, customer, conversation, message, order, payment or usage tables. No dashboard.
No STK push. No subscription charging. No AI metering. No vector embeddings. No partitioning.
No job queue. No multi-currency. No webhook rewrite. No SQLite migration or removal. No change
to Railway, Vercel, WhatsApp credentials or the existing verification flow.

Each belongs to a later phase, and Phase 1 stops here deliberately: the goal was a secure,
tested multi-tenant foundation before anything is built on top of it.

---

*Phase 1 complete. Related background and the wider schema direction:
[`DATABASE_PLAN.md`](./DATABASE_PLAN.md).*