# Isolated staging environment

Staging exists to prove a change against a real deployment **without any path to production
data, credentials, WhatsApp identity or domains**. This document is the runbook for building
and using it. Nothing in it has been executed: no project, service, variable or migration has
been created.

## 1. Isolation model

| Concern | Production | Staging |
|---|---|---|
| Database | Supabase project A | **separate project B** (`SAAS_PRODUCTION_PROJECT_REF` names A) |
| Auth / API | project A keys | project B keys (new admin user) |
| Hosting | Railway service in the production project | **separate Railway project**, own variables, own domain |
| Deployment branch | `platform-admin-dashboard` | `staging` |
| WhatsApp | live WABA, live tokens | none, or a dedicated test number |
| Data | real tenants | synthetic tenant only; never a production dump |

Railway variables are per-service, and Supabase projects share nothing, so contamination
requires a deliberate copy. The application also refuses a staging boot that resolves to the
production project (§4).

## 2. Minimum resources

| Resource | Minimum | Why |
|---|---|---|
| Supabase project | 1 (free tier) | Postgres + Auth + PostgREST; the schema is a few MB and no storage objects are used |
| Supabase Auth user | 1 | the staging administrator (never reuse a production UUID) |
| Railway project | 1, new | variable, domain and deploy isolation |
| Railway service | 1 web service, 1 replica, 512 MB–1 GB | two gunicorn workers; no worker, cron or queue exists in this codebase |
| Meta number | 0 by default | sending stays disabled until a test number exists |

## 3. Environment contract

### 3a. Application service (Railway staging only)

| Variable | Required | Rule |
|---|---|---|
| `SAAS_ENVIRONMENT` | for staging | `staging`; unset means production |
| `SAAS_PRODUCTION_PROJECT_REF` | for staging | the **production** project reference that staging must never reach |
| `SAAS_DATABASE_URL` | yes | role `app_backend` on the staging project |
| `SAAS_PLATFORM_DATABASE_URL` | yes | role `saas_platform_admin` on the staging project |
| `PLATFORM_SECRET_KEY` | yes | new random ≥32 chars, different from production |
| `SUPABASE_URL` | yes | `https://<staging-ref>.supabase.co` |
| `SUPABASE_ANON_KEY` | yes | staging anon (public) key; `service_role`/`sb_secret_` keys are rejected |
| `SUPABASE_JWT_SECRET` | no | omit for projects using asymmetric signing (JWKS is used) |
| `WHATSAPP_SEND_ENABLED` | **explicit** | `0` (default and recommended) or `1` with a test number |
| `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` | no | staging-only values, never production's |
| `WHATSAPP_ACCESS_TOKEN_<TENANT_UUID>` | no | staging tenant only |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | no | separate key or omit (bot replies are then disabled) |
| `PLATFORM_TRUSTED_PROXIES` | no | `1` behind Railway |
| `PLATFORM_ALLOW_ACTIVE_CHANNELS` | no | `0` unless exercising inbound webhooks |
| `PLATFORM_SESSION_MINUTES` | no | `60` |

`PLATFORM_DEV_MODE` and `PLATFORM_INSECURE_COOKIES` must stay `0`; staging refuses to boot
otherwise.

### 3b. Operator shell (never on a Railway service)

| Variable | Purpose |
|---|---|
| `SAAS_OPERATOR_DATABASE_URL` | owner login on the staging project, used by `db/run_migrations.py` and `db/bootstrap_platform_admin.py` |

### 3c. Test-only

`SAAS_TEST_DATABASE_URL` is for local/CI runs. Never set it on a deployed service.

### 3d. Never copy from production

The production operator DSN, production `WHATSAPP_ACCESS_TOKEN_*` values, the production
`WHATSAPP_APP_SECRET` / `WHATSAPP_VERIFY_TOKEN`, the production `PLATFORM_SECRET_KEY`, any
production tenant UUID, any `service_role` key, and `SAAS_SUPABASE_MIGRATION_DATABASE_URL`
(no code reads it).

## 4. Fail-closed guarantees

With `SAAS_ENVIRONMENT=staging`, `Settings.from_env()` raises `ConfigError` **before any
connection is opened** when:

1. `SAAS_PRODUCTION_PROJECT_REF` is missing — the production project cannot be excluded;
2. `SAAS_DATABASE_URL`, `SAAS_PLATFORM_DATABASE_URL` or `SUPABASE_URL` resolves to that
   reference (pooled `role.<ref>` usernames, `db.<ref>.supabase.co` hosts and
   `<ref>.supabase.co` URLs are all recognised);
3. `WHATSAPP_SEND_ENABLED` is not set explicitly;
4. `SAAS_OPERATOR_DATABASE_URL` is present;
5. `PLATFORM_DEV_MODE` or `PLATFORM_INSECURE_COOKIES` is `1`.

The guard is **inert unless staging is declared**, so the existing production deployment
(`SAAS_ENVIRONMENT` unset) keeps booting unchanged. The pre-existing checks still apply in
both environments: dedicated roles only (no `postgres`/`service_role`), `https` Supabase URL,
non-secret anon key, role verification, and the `REQUIRED_SCHEMA` check that now includes
`public.handover_requests` (migration 0034).

`python db/staging_preflight.py` applies the same rules offline and explains every finding:

```bash
python db/staging_preflight.py --env-file .env.staging     # exit 0 = safe, 1 = refused
python db/staging_preflight.py --json                      # machine-readable
```

It opens no database connection and prints only roles, project references and variable
names — never a host, password or token.

## 5. Provisioning steps

**5.1 Create the staging Supabase project.** New project, same region as production. Record
its reference as `<STAGING_REF>`; record production's as `<PROD_REF>` (out of band).

**5.2 Take three connection strings** from the staging project (session mode, port 5432):

```
operator  postgresql://postgres:<owner-pw>@<staging-host>:5432/postgres
app       postgresql://app_backend.<STAGING_REF>:<app-pw>@<staging-host>:5432/postgres
platform  postgresql://saas_platform_admin.<STAGING_REF>:<platform-pw>@<staging-host>:5432/postgres
```

**5.3 Apply the schema** (operator DSN, from a workstation — never from Railway). The
repository holds 34 migrations; `0034_handover_requests.sql` is the newest and must be applied
before the dashboard code boots:

```bash
export OP='postgresql://postgres:<owner-pw>@<staging-host>:5432/postgres'
python db/run_migrations.py --database-url "$OP" --status   # expect 0033 newest, 0034 pending
python db/run_migrations.py --database-url "$OP"            # applies 0034
python db/run_migrations.py --database-url "$OP" --status   # expect 34 applied
```

Do **not** pass `--include-local` or `--seed`: they install known development passwords and
fixture data, and the runner refuses them for Supabase hosts by design.

Confirm the ledger and the known checksum (`sha256(file)[:16]` of
`0034_handover_requests.sql` is `e21b4e80d299a056`):

```sql
select version, checksum from public.saas_schema_migrations
 where version = '0034_handover_requests.sql';
select to_regclass('public.handover_requests');
```

**5.4 Give the two application roles a login** (operator DSN, once). The migrations create
`app_backend` as NOLOGIN and `saas_platform_admin` without a password; without this the app
cannot start:

```sql
alter role app_backend         with login password '<strong-random-A>';
alter role saas_platform_admin with login password '<strong-random-B>';
```

Verify: `select rolname, rolcanlogin, rolsuper, rolbypassrls from pg_roles where rolname in
('app_backend','saas_platform_admin');` — both login, neither superuser nor BYPASSRLS.

**5.5 Create the staging administrator.** Add a user in staging Supabase Auth, sign in once,
read the verified `sub`, then:

```bash
python db/bootstrap_platform_admin.py --user-id <staging-auth-uuid> --confirm-verified \
       --database-url "$OP"
```

**5.6 Create the Railway project and service.** New Railway project → deploy
`sammaash/saas-project` from the `staging` branch → add the variables from §3a → healthcheck
path `/login` (no dedicated health endpoint exists) → deploy.

Boot log must show no `ConfigError`. `python db/staging_preflight.py` must print
`STAGING PREFLIGHT: PASS` when run with the same variables.


## 6. Migration, deployment and verification runbook

1. **Preflight** — `python db/staging_preflight.py --env-file <staging env>` → `PASS`.
2. **Schema** — `--status` on the staging operator DSN: no pending migration before deploying
   code that needs it.
3. **Deploy** the reviewed commit to the `staging` branch; Railway builds that service only.
4. **Boot** — logs free of `ConfigError`; `GET /login` returns 200.
5. **Sign in** as the staging administrator; `/tenants` reachable.
6. **Synthetic tenant** — create one through `/tenants/new` (no real business data, no seed).
7. **Handover checks** — the tab renders, and `select count(*) from public.handover_requests`
   returns 0 without error.

| Scenario | Safe staging method | Expected |
|---|---|---|
| Successful notification | dedicated test tenant, staff number you control, `WHATSAPP_SEND_ENABLED=1` | row `notified`, staff message received |
| Notification failure | `WHATSAPP_SEND_ENABLED=0`, or a staff number outside Meta's 24-hour window | row `failed` + reason; customer told the request was *logged*, never *shared* |
| Duplicate webhook delivery | post the same signed payload twice | second response `duplicate: true`, still one row, one notification |
| Tenant isolation | two synthetic tenants; dashboard on tenant A | tenant B's requests never listed; status change on B refused |

No real customer is contacted at any point: use your own number for the staff contact and a
synthetic sender for inbound payloads.

## 7. Rollback

1. **Code** — redeploy the previous Railway deployment in the staging project.
2. **Schema** — only after the code rollback: `drop table if exists public.handover_requests;`
   then `delete from public.saas_schema_migrations where version = '0034_handover_requests.sql';`
   This discards staging handover rows only; 0034 alters no existing table.
3. **Project** — a staging Supabase project can be deleted outright; nothing in production
   references it.

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

## 9. Open decisions before provisioning

| # | Decision | Recommendation |
|---|---|---|
| 1 | Railway topology: new project vs service inside the existing one | **new project** |
| 2 | Supabase plan/region | free tier, same region as production |
| 3 | WhatsApp: none vs dedicated test number | **none first**; add a test number only for the delivery scenario |
| 4 | Tenant data: synthetic via dashboard vs staging seed file | synthetic tenant; no new seed to maintain |
| 5 | Who holds the staging owner and role passwords | your secret manager, rotated on exposure |
| 6 | Healthcheck | `/login` now; add `/healthz` later if desired |

## 10. Known limitations

* **Production notification delivery is still unverified.** The handover code is proven by
  tests and will be proven in staging; a real staff message must be observed once before
  claiming it works in production.
* Meta's 24-hour window applies to staff notifications. If the staff number has not messaged
  the business number within 24 hours, delivery fails and the request is marked `failed`.
* This document deliberately contains no credentials, project references or tenant UUIDs.

