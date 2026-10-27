# Isolated staging environment

Staging exists to prove a change against a real deployment **without any path to production
data, credentials, WhatsApp identity or domains**. This document is the runbook for building
and using it. Nothing in it has been executed: no project, service, variable or migration has
been created.

## 1. Isolation model

| Concern | Production | Staging |
|---|---|---|
| Database | Supabase project A (`<PROD_REF>`) | **Railway PostgreSQL 17**, its own project `saas-staging`, environment `staging` |
| Auth / API | Supabase project A keys | **separate Supabase project** (`<STAGING_AUTH_REF>`) — **Auth only**; new admin user, never a production UUID |
| Hosting | Railway service `saas-project` in the production project | **separate Railway project `saas-staging`**, own variables, own domain |
| Network | public app, private database | application service and database share **one project and one environment**, so the DSNs use the private internal hostname only; the database has **no public access and no TCP proxy** |
| Deployment branch | `platform-admin-dashboard` | `staging` |
| WhatsApp | live WABA, live tokens | none, or a dedicated test number; **sending disabled by default** |
| Data | real tenants | synthetic tenant only; never a production dump |

The two databases are different engines in different projects: staging is a Railway PostgreSQL
instance, production's is Supabase. There is no shared instance, credential, network or project
to contaminate, and Railway variables are per-service. The application also refuses a staging
boot that resolves to the production Supabase project (§4) — but read §4 carefully: **that
check proves nothing about the Railway database.**

## 2. Minimum resources

| Resource | Minimum | Why |
|---|---|---|
| Railway project | 1, new — `saas-staging` | variable, domain, network and deploy isolation; contains its own environment `staging` |
| Railway database service | 1, **PostgreSQL 17**, private only | holds the schema (a few MB); no public access and no TCP proxy |
| Railway application service | 1 web service, 1 replica, 512 MB–1 GB | `gunicorn --workers 2` from the `Procfile`; no worker, cron or queue exists in this codebase |
| Supabase project | 1 (free tier) | **Auth only** — the dashboard signs in through Supabase Auth; no application data is stored there |
| Supabase Auth user | 1 | the staging administrator (never reuse a production UUID) |
| Meta number | 0 by default | sending stays disabled until a test number exists |

The application service and the database must live in the **same Railway project and the same
environment**. Railway's private network is scoped to one project *and* one environment, and it
is the only supported way to reach the database without exposing it publicly.

## 3. Environment contract

### 3a. Application service (Railway staging only)

| Variable | Required | Rule |
|---|---|---|
| `SAAS_ENVIRONMENT` | for staging | `staging`; unset means production |
| `SAAS_PRODUCTION_PROJECT_REF` | for staging | the **production** project reference that staging must never reach |
| `SAAS_STAGING_DB_HOST` | for staging | the **private hostname the staging database must use**; both DSNs are matched against it, case-insensitively |
| `SAAS_DATABASE_URL` | yes | role `app_backend` on the **staging Railway database**, reached over its private internal hostname |
| `SAAS_PLATFORM_DATABASE_URL` | yes | role `saas_platform_admin` on the **same** Railway instance, over the **same** internal hostname |
| `PLATFORM_SECRET_KEY` | yes | new random ≥32 chars, different from production |
| `SUPABASE_URL` | yes | `https://<STAGING_AUTH_REF>.supabase.co` — the **Auth-only** staging project |
| `SUPABASE_ANON_KEY` | yes | staging anon (public) key; `service_role`/`sb_secret_` keys are rejected |
| `SUPABASE_JWT_SECRET` | no | omit for projects using asymmetric signing (JWKS is used) |
| `WHATSAPP_SEND_ENABLED` | **explicit** | `0` (default and recommended) or `1` with a test number |
| `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` | no | staging-only values, never production's |
| `WHATSAPP_ACCESS_TOKEN_<TENANT_UUID>` | no | staging tenant only |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | no | separate key or omit (bot replies are then disabled) |
| `PLATFORM_TRUSTED_PROXIES` | no | `1` behind Railway |
| `PLATFORM_ALLOW_ACTIVE_CHANNELS` | no | `0` unless exercising inbound webhooks |
| `PLATFORM_SESSION_MINUTES` | no | `60` |

Both DSNs must use the Railway **private internal hostname** (the form
`<service>.railway.internal`, port 5432) — the database has no public access. Use the *same*
hostname form in both DSNs: the offline preflight treats two different host shapes as a
split-project configuration and refuses it. Never put the owner/`postgres` login in either DSN;
the boot guard rejects `postgres`, `service_role` and `supabase_admin`.

`PLATFORM_DEV_MODE` and `PLATFORM_INSECURE_COOKIES` must stay `0`; staging refuses to boot
otherwise.

### 3b. Operator shell (never on a Railway service)

| Variable | Purpose |
|---|---|
| `SAAS_OPERATOR_DATABASE_URL` | owner login on the **staging Railway database**, used by `db/run_migrations.py` and `db/bootstrap_platform_admin.py`; never present on a Railway service |

### 3c. Test-only

`SAAS_TEST_DATABASE_URL` is for local/CI runs. Never set it on a deployed service.

### 3d. Never copy from production

The production operator DSN, production `WHATSAPP_ACCESS_TOKEN_*` values, the production
`WHATSAPP_APP_SECRET` / `WHATSAPP_VERIFY_TOKEN`, the production `PLATFORM_SECRET_KEY`, the
production Supabase anon key (staging Auth is its own project), any production tenant UUID, any
`service_role` key, and `SAAS_SUPABASE_MIGRATION_DATABASE_URL` (no code reads it).

## 4. Fail-closed guarantees

With `SAAS_ENVIRONMENT=staging`, `Settings.from_env()` raises `ConfigError` **before any
connection is opened** when:

1. `SAAS_PRODUCTION_PROJECT_REF` is missing — the production project cannot be excluded;
2. `SAAS_DATABASE_URL`, `SAAS_PLATFORM_DATABASE_URL` or `SUPABASE_URL` resolves to that
   reference (pooled `role.<ref>` usernames, `db.<ref>.supabase.co` hosts and
   `<ref>.supabase.co` URLs are all recognised);
3. `SAAS_STAGING_DB_HOST` is missing, or either DSN has no hostname, or either DSN points at a
   host other than the one that variable names (compared case-insensitively);
4. either DSN carries a query string or a URL fragment — see below;
5. `WHATSAPP_SEND_ENABLED` is not set explicitly;
6. `SAAS_OPERATOR_DATABASE_URL` is present;
7. `PLATFORM_DEV_MODE` or `PLATFORM_INSECURE_COOKIES` is `1`.

**Why the database host is declared.** The project-reference comparison in (2) is *vacuous for a
non-Supabase database*: a Railway PostgreSQL DSN carries no project reference, so that check
would pass for **any** host. `SAAS_STAGING_DB_HOST` closes that gap — both DSNs must name that
exact hostname, so a staging boot cannot point at the legacy staging instance, another tenant's
database, or any other host by mistake. Comparing each DSN against the declared host also
guarantees the two DSNs agree with one another. The reference check still guards `SUPABASE_URL`
(Auth). `db/staging_preflight.py` reports a missing reference as a `WARN` (`project`) and any
host problem as a `FAIL` (`db-host`).

**Why a DSN may not carry a query string.** libpq reads connection parameters from the URI
query string, so `?host=` and `?hostaddr=` redirect the connection to a different server (and
`?user=` overrides the login) while `urlsplit(dsn).hostname` — the value the host check in (3)
reads — keeps reporting the hostname the DSN appears to name. A DSN carrying either would
therefore satisfy the host check and still connect somewhere else, so staging refuses the whole
class instead of trying to enumerate the dangerous parameter names. Nothing in the staging
contract needs one: the database is reached over the private internal hostname with the default
libpq settings, and `.env`, `.env.example` and `.env.staging` contain no query parameter or
fragment. A hosted database that genuinely required `?sslmode=…` would need a narrow,
target-neutral allowlist here; no such requirement has appeared. Parameter *names* — never
values — appear in the message, and `db/staging_preflight.py` reports the same condition as a
`FAIL` (`dsn-overrides`).

The guard is **inert unless staging is declared**, so the existing production deployment
(`SAAS_ENVIRONMENT` unset) keeps booting unchanged. The pre-existing checks still apply in
both environments: dedicated roles only (no `postgres`/`service_role`), `https` Supabase URL,
non-secret anon key, role verification, and the `REQUIRED_SCHEMA` check that now includes
`public.handover_requests` (migration 0034) and the two quotation tables
`public.quotation_briefs` / `public.quotation_deliveries` (migration 0037). A database
without them now fails the boot check with the missing table name instead of failing the
first customer quotation at runtime, so `0037` must be applied **before** the code that
reads those tables is deployed.

`python db/staging_preflight.py` applies the same rules offline and explains every finding:

```bash
python db/staging_preflight.py --env-file .env.staging     # exit 0 = safe, 1 = refused
python db/staging_preflight.py --json                      # machine-readable
```

It opens no database connection and prints only roles, project references and variable
names — never a host, password or token.

## 5. Provisioning steps

**5.1 Create the Railway project, its database, and the Supabase Auth project.** New Railway
project `saas-staging` with environment `staging`. Add a **PostgreSQL 17** service, **private
only** — do not enable Public Access and do not add a TCP proxy. Separately create one Supabase
project for **authentication only**; record its reference as `<STAGING_AUTH_REF>` and
production's as `<PROD_REF>` (both out of band). The application service (§5.6) goes in the
**same project and environment** as the database.

**5.2 Take three connection strings** from the Railway database. Both application DSNs use the
**private internal hostname**; the owner DSN is for the operator shell only:

```
operator  postgresql://postgres:<owner-pw>@<STAGING-DB-INTERNAL-HOST>:5432/railway
app       postgresql://app_backend:<app-pw>@<STAGING-DB-INTERNAL-HOST>:5432/railway
platform  postgresql://saas_platform_admin:<platform-pw>@<STAGING-DB-INTERNAL-HOST>:5432/railway
```

**Verify the database identity before migrating** — read-only, and never skipped. The
project-reference guard cannot do this for you (see §4):

```sql
set default_transaction_read_only = on;
select current_database(), current_user, inet_server_addr(), version();
select name, default_version from pg_available_extensions where name = 'pgcrypto';
select to_regclass('public.saas_schema_migrations');   -- NULL on a never-migrated database
```

Confirm the server is the new staging instance and **not** the production Supabase project, and
that `version()` reports PostgreSQL 17. The `pgcrypto` row must exist: migration
`0001_extensions_and_helpers.sql` runs `create extension if not exists pgcrypto`, which **errors
if the extension is unavailable**. Resolve that before applying anything.

Record that hostname as `SAAS_STAGING_DB_HOST` on the service. Both DSNs must use it, and the
boot refuses to start when it is missing or when either DSN points somewhere else (§4).

**5.3 Apply the schema** (operator DSN — **never** from the Railway web service). Because the
database is private, the operator shell must run **inside** the staging project's network (a
Railway shell) or through a temporary TCP proxy that is removed immediately afterwards. The
repository holds 37 migrations; `0037_quotation_briefs_and_deliveries.sql` is the newest and
must be applied before the dashboard code boots. On a database that has never been migrated,
`--status` prints `no migrations recorded yet` and lists every file as pending:

```bash
export OP='postgresql://postgres:<owner-pw>@<STAGING-DB-INTERNAL-HOST>:5432/railway'
python db/run_migrations.py --database-url "$OP" --status   # expect 0035, 0036 and 0037 pending
python db/run_migrations.py --database-url "$OP"            # applies 0035, 0036 and 0037
python db/run_migrations.py --database-url "$OP" --status   # expect 37 applied, no mismatches
```

`0036` is the committed variant A file (checksum `9dd2c780deaecfad`); it only adds the
WhatsApp columns to `quotations`. The quotation tables themselves arrive in `0037`. Neither
file may be edited: the runner refuses a migration whose bytes changed after it was applied,
and `docs/MIGRATION_0036_RECONCILIATION.md` explains why the decision was made this way.

**Forbidden for staging, without exception:** `--include-local`, `--seed`,
`--allow-nonlocal-dev` and `--reset`. The first two install known development passwords and
fixture data; `--allow-nonlocal-dev` is the override that would let them through on a
non-loopback host; `--reset` drops the `public` and `app` schemas and is restricted to loopback
by the runner. Staging data is created through the dashboard as a synthetic tenant, never from a
seed file.

Confirm the ledger and the known checksums (`sha256(file)[:16]`):

```sql
select version, checksum from public.saas_schema_migrations
 where version in ('0035_menu_state_and_reply_correlation.sql',
                   '0036_whatsapp_quotations.sql',
                   '0037_quotation_briefs_and_deliveries.sql');
-- 0035_menu_state_and_reply_correlation.sql  06fac1777fa3caaa
-- 0036_whatsapp_quotations.sql               9dd2c780deaecfad
-- 0037_quotation_briefs_and_deliveries.sql   8b50f1e5308e8e23
select to_regclass('public.quotation_briefs'), to_regclass('public.quotation_deliveries');
select relname, relrowsecurity, relforcerowsecurity from pg_class
 where oid in ('public.quotation_briefs'::regclass, 'public.quotation_deliveries'::regclass);
```

`0035` adds only a nullable correlation column and two `conversations.menu_state*` columns,
and `0036`/`0037` are additive (new nullable columns, then two new tables), so all three are
safe to apply before the code that reads them. `0037` carries the quotation tables the
customer quotation workflow needs, and `REQUIRED_SCHEMA` now names them, so a database that
boots without `0037` fails at startup with the missing table name rather than at the first
quotation write.

If the runner stops with `0036_whatsapp_quotations.sql was modified after it was applied`,
a database already recorded a different `0036` file. Do not edit the ledger by hand: the
options and their consequences are set out in `docs/MIGRATION_0036_RECONCILIATION.md`.

**5.4 Give the two application roles a login** (operator DSN, once, **after §5.3**). The roles
are created *by the migrations*, not by hand, so this step can only run once they have been
applied:

| Role | Created by | Attributes as migrated | Still needed |
| --- | --- | --- | --- |
| `app_backend` | `0002_roles.sql` | `NOLOGIN` (`NOBYPASSRLS`) | `LOGIN` **and** a password |
| `saas_platform_admin` | `0013_phase1_security_hardening.sql` | `NOLOGIN NOINHERIT NOBYPASSRLS` | a password only — `0015_platform_provisioning_login.sql` already grants `LOGIN` |

```sql
alter role app_backend         with login password '<strong-random-A>';
alter role saas_platform_admin with password '<strong-random-B>';
```

`0015` already grants `LOGIN` to `saas_platform_admin`, so only its password is set here;
repeating `with login` is harmless and idempotent. `app_backend` needs both. Without both
statements the application cannot start.

**Verify the operator's privileges first.** `alter role` needs the executing login to hold
`CREATEROLE`, or `ADMIN`/membership on the target role — on a managed instance that is usually
the provider's owner login behind `SAAS_OPERATOR_DATABASE_URL`. Check before running the
statements above:

```sql
select current_user, rolsuper, rolcreaterole from pg_roles where rolname = current_user;
select rolname, rolcanlogin, rolsuper, rolbypassrls from pg_roles
 where rolname in ('app_backend', 'saas_platform_admin');
```

The second query is also the verification afterwards: both roles login, neither is superuser nor
`BYPASSRLS`. Migrations `0002`, `0013` and `0015` make their own `ALTER ROLE … NOBYPASSRLS`
attempts inside `exception when insufficient_privilege then raise notice` blocks, so a missing
privilege there yields a notice rather than a failed migration — this query, not the migration
output, is what confirms the outcome.

**5.5 Create the staging administrator.** Add a user in staging Supabase Auth, sign in once,
read the verified `sub`, then:

```bash
python db/bootstrap_platform_admin.py --user-id <staging-auth-uuid> --confirm-verified \
       --database-url "$OP"
```

**5.6 Create the Railway application service.** Inside the project created in §5.1, add a service
deploying `sammaash/saas-project` from the **`staging`** branch → add the variables from §3a →
healthcheck path `/login` (no dedicated health endpoint exists) → deploy. It must sit in the same
project **and the same environment** as the database, or the private DSNs will not resolve.

Boot log must show no `ConfigError`. `python db/staging_preflight.py` must print
`STAGING PREFLIGHT: PASS` when run with the same variables.


## 6. Migration, deployment and verification runbook

1. **Preflight** — `python db/staging_preflight.py --env-file <staging env>` → `PASS`. A
   `project` `WARN` is expected: a Railway DSN names no Supabase project reference.
2. **Database identity** — run the read-only checks in §5.2. The server must be the new staging
   instance and `pgcrypto` must be available. **Stop here if either check fails.**
3. **Schema** — `--status` on the staging operator DSN, apply through `0037`, then `--status`
   again (§5.3): **37 applied, 0 mismatches, 0 pending** before any code is deployed.
4. **Deploy** the reviewed commit to the `staging` branch; Railway builds that service only.
5. **Boot** — logs free of `ConfigError`; `GET /login` returns 200.
6. **Sign in** as the staging administrator; `/tenants` reachable.
7. **Synthetic tenant** — create one through `/tenants/new` (no real business data, no seed).
8. **Handover checks** — the tab renders, and `select count(*) from public.handover_requests`
   returns 0 without error.
9. **Quotation checks with sending off** — `WHATSAPP_SEND_ENABLED=0` (the default). Section H of
   the selected tenant's rules tab saves with the box unchecked; the menu then shows six options
   and a typed `quote` is answered with "aren't available … our team can prepare a written
   quotation". Enabling the box shows seven options, and a quotation request is refused with the
   staff-handover message and **writes nothing**.
10. **Controlled WhatsApp scenarios** — only after explicit approval, with a dedicated test number
    and sending temporarily enabled (table below). Afterwards restore
    `WHATSAPP_SEND_ENABLED=0` and uncheck section H.

| Scenario | Safe staging method | Expected |
|---|---|---|
| Successful notification | dedicated test tenant, staff number you control, `WHATSAPP_SEND_ENABLED=1` | row `notified`, staff message received |
| Notification failure | `WHATSAPP_SEND_ENABLED=0`, or a staff number outside Meta's 24-hour window | row `failed` + reason; customer told the request was *logged*, never *shared* |
| Duplicate webhook delivery | post the same signed payload twice | second response `duplicate: true`, still one row, one notification |
| Tenant isolation | two synthetic tenants; dashboard on tenant A | tenant B's requests never listed; status change on B refused |
| Quotation, flag off | synthetic tenant with no `configuration.quotations` | no quotation row; a typed `quote` offers staff instead of a price |
| Quotation, flag on | section H checked, `WHATSAPP_SEND_ENABLED=1`, your own number, catalogue loaded | item/size/quantity collected, catalogue-priced summary, `quotations.source = 'whatsapp'` with one `quotation_briefs` row |
| Quotation PDF | ask for the PDF after the summary | document received; one `quotation_deliveries` row `sent`; replaying the same inbound payload sends nothing |
| Quotation PDF failure | `WHATSAPP_SEND_ENABLED=0`, or a deliberately bad access token | delivery row `failed` with a code, customer told staff will send it, no duplicate |
| Unpriceable request | ask for an item that is not in the catalogue | staff handover with the details collected, no invented price |

No real customer is contacted at any point: use your own number for the staff contact and a
synthetic sender for inbound payloads.

## 7. Rollback

**Default: roll the application back only, and keep the migration ledger and schema.** All three
migrations are additive (nullable columns, then two new tables), so earlier code runs correctly
against the newer schema — including code that predates `REQUIRED_SCHEMA` naming the quotation
tables.

1. **Application** — redeploy the previous Railway deployment. Do not touch the database.
2. **Neutralise the feature** — uncheck section H for the tenant and set
   `WHATSAPP_SEND_ENABLED=0`. The feature then advertises nothing and writes nothing, with the
   schema still in place.
3. **Verify** — `python db/run_migrations.py --database-url "<operator DSN>" --status` must still
   report **37 applied, 0 mismatches**. The ledger is preserved, and that check is the proof.
4. **Escalate only if a migration is provably broken** — prefer fixing forward with a new
   migration. Destructive schema rollback is **not** a routine operation: it is a last resort,
   performed only after the code rollback, in reverse order (`0037` before `0036`, because
   `REQUIRED_SCHEMA` names both tables and the dashboard refuses to boot without them).
5. **Corrupted staging database** — delete and recreate it. It holds synthetic data only, so this
   is cheaper and safer than repairing it.

**Never:** edit `public.saas_schema_migrations` by hand, delete a ledger row to force a re-apply,
or bypass the runner's immutability check. A refused run means a migration file changed after it
was applied; see `docs/MIGRATION_0036_RECONCILIATION.md`.

<details>
<summary>Exceptional, last-resort schema rollback (reference only — not the default)</summary>

- **`0037`** — delete the tenant's quotation rows, then
  `drop table if exists public.quotation_deliveries, public.quotation_briefs;` and remove the
  `0037` ledger row. Must precede the `0036` step.
- **`0036`** — drop `source`, `customer_wa_id`, `idempotency_key` from `public.quotations`
  (restore `created_by_user_id not null` first, which requires that no WhatsApp-issued row
  remains), then remove the `0036` ledger row.
- **`0035`** — drop the inbound-correlation and `menu_state*` columns; rarely needed, and leaving
  the ledger row in place is the safer default.
- **`0034`** — `drop table if exists public.handover_requests;` then remove its ledger row.
- **Project** — the staging Railway project and the staging Supabase Auth project can both be
  deleted outright; nothing in production references them.
</details>

## 8. Branch and release flow

```
feature work ──► platform-admin-dashboard   (production deploy target: pushing it deploys production)
                        │
                        └──► staging        (staging deploy target)
```

The `staging` branch was created from the reviewed commit so the two branches start identical.
Promotion is explicit: merge/cherry-pick onto `platform-admin-dashboard` once staging has
passed. Pushing `platform-admin-dashboard` still deploys production, so staging work must never
land there by accident.

## 9. Decisions (settled) and what remains open

Settled for this architecture:

| # | Decision | Chosen |
|---|---|---|
| 1 | Railway topology | **new, isolated project `saas-staging`**, environment `staging`, holding both the database and the application service |
| 2 | Staging database | **Railway PostgreSQL 17**, private networking only — no public access, no TCP proxy |
| 3 | Supabase | **one project, Auth only** (no application data stored there); free tier is sufficient |
| 4 | Deployment branch | `staging` |
| 5 | Healthcheck | `/login` now; add `/healthz` later if desired |
| 6 | Tenant data | synthetic tenant created through the dashboard; no staging seed file |

Still open:

| # | Decision |
|---|---|
| 7 | WhatsApp: none vs a dedicated test number — **none** until a controlled test is approved; sending stays `0` |
| 8 | Who holds the staging owner DSN and the two role passwords, and the rotation policy |
| 9 | ~~Optional hardening for non-Supabase database identity~~ — **implemented**: staging now requires `SAAS_STAGING_DB_HOST` and matches both DSNs against it (see §4) |

## 10. Known limitations

* **Production notification delivery is still unverified.** The handover code is proven by
  tests and will be proven in staging; a real staff message must be observed once before
  claiming it works in production.
* Meta's 24-hour window applies to staff notifications. If the staff number has not messaged
  the business number within 24 hours, delivery fails and the request is marked `failed`.
* **The boot guard verifies the Railway database by declared hostname.** `SAAS_STAGING_DB_HOST`
  must match both DSNs (§4); the Supabase project-reference check alone cannot identify a
  non-Supabase host. The read-only identity check in §5.2 is still the only control that
  inspects the *server*, so keep running it.
* **Supabase free-tier projects pause after a week of inactivity**, which would break staging
  sign-in. Either keep the Auth project exercised, or budget a paid plan if staging must be
  always available.
* **PostgreSQL 17 is chosen deliberately.** The suite is proven on PostgreSQL 16 and 17 only;
  moving staging to 18 requires re-running the migration and full-suite validation on an 18
  instance first.
* **The Railway CLI link is a foot-gun.** The working directory used for this project is linked
  to the *production* project. Always confirm the target with `railway status` before any
  Railway command, and never deploy from a production-linked directory.
* This document deliberately contains no credentials, project references or tenant UUIDs.

