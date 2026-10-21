# Staging menu release preparation

**Status:** prepared locally on 21 October 2026. No staging resource was created, no code was
pushed or deployed, no migration was applied, and no WhatsApp message was sent.

**Integration commit:** `0c796c4f2e0e1b4128ae24afac6a667f2c8010a7`

**Verdict:** the integration is technically ready for staging review, but staging is **not
ready to receive it**. The available staging configuration fails the isolation preflight and
must not be connected to.

## 1. Resource inventory

| Resource | Evidence | Status |
|---|---|---|
| Railway production project | `lucky-beauty` / `saas-project` / `production` | Production; do not use for staging |
| Railway project `industrious-elegance` | Separate workspace project | Exists |
| Service `saas-staging` in that project | Latest deployment `8edbb10b…` is `SUCCESS`, image `postgres-ssl:18`, with a PostgreSQL data volume | A database service, **not** the application web service |
| Staging application web service | No separate app service or deployment is evidenced | **Not provisioned / not verified** |
| Staging Supabase project | Runbook requires a separate project, but no verified staging project or safe staging credential is available | **Not verified**; the available env file is unsafe |
| `staging` branch | Local `f267816`; no `origin/staging` | Local only; staging deployment target not established |

The existing `industrious-elegance` / `saas-staging` service must not be assumed to satisfy the
staging application contract. It is a PostgreSQL service, while the application expects
Supabase Auth, the documented role/credential model, and a separate Railway web service.
Whether to use it at all is an owner decision.

The current `.env.staging` file was checked with the preflight and returned:

```text
STAGING PREFLIGHT: REFUSED
4 failure(s), 3 warning(s)
```

It must not be used to query or migrate any database. Its failures include a non-application
`postgres` role for `SAAS_DATABASE_URL`, production-targeting platform/Supabase URLs, an
operator DSN in the application environment, and WhatsApp sending enabled. No credentials or
connection strings are reproduced here.

## 2. Provisioning checklist — owner approval required

Do not create or resize any resource until the owner approves cost and ownership.

- [ ] Choose the staging database: a **separate Supabase project** (recommended by the runbook)
      or an explicitly approved alternative. Do not reuse production.
- [ ] Create or confirm a separate Railway project and web service for staging.
- [ ] Create a staging Supabase Auth administrator; never reuse a production Auth UUID.
- [ ] Set `app_backend` and `saas_platform_admin` logins with staging-only passwords.
- [ ] Keep the staging operator credential in the approved operator environment only.
- [ ] Configure the staging web service with the staging-only values in §3.
- [ ] Set `WHATSAPP_SEND_ENABLED=0`; use no production WhatsApp credentials or numbers.
- [ ] Configure `/login` as the staging healthcheck.
- [ ] Establish the `staging` branch as the staging deployment target; do not point it at the
      production branch.
- [ ] Record the actual production project reference in the approved secret/inventory source
      and populate `SAAS_PRODUCTION_PROJECT_REF` from there. Do not copy it into this tracked
      document.

## 3. Staging-only environment template

Fill placeholders from the approved staging/owner inventory. Do not commit the filled file.

```dotenv
# Staging web service only.
SAAS_ENVIRONMENT=staging
# Actual production reference, sourced from the approved production inventory.
SAAS_PRODUCTION_PROJECT_REF=<PRODUCTION_REF_FROM_APPROVED_INVENTORY>

# Staging Supabase project, session-mode pooler, port 5432.
SAAS_DATABASE_URL=postgresql://app_backend.<STAGING_REF>:<STAGING_APP_PASSWORD>@<STAGING_POOLER_HOST>:5432/postgres
SAAS_PLATFORM_DATABASE_URL=postgresql://saas_platform_admin.<STAGING_REF>:<STAGING_PLATFORM_PASSWORD>@<STAGING_POOLER_HOST>:5432/postgres

SUPABASE_URL=https://<STAGING_REF>.supabase.co
SUPABASE_ANON_KEY=<STAGING_ANON_KEY>
# Optional only for legacy HS256 projects; prefer JWKS.
# SUPABASE_JWT_SECRET=<STAGING_JWT_SECRET>
PLATFORM_SECRET_KEY=<NEW_RANDOM_SECRET_AT_LEAST_32_CHARS>

# Safe staging default; only change with a dedicated test number and explicit approval.
WHATSAPP_SEND_ENABLED=0
# Optional staging-only values; never copy production values.
# WHATSAPP_VERIFY_TOKEN=<STAGING_TOKEN>
# WHATSAPP_APP_SECRET=<STAGING_SECRET>
# WHATSAPP_GRAPH_API_VERSION=v26.0
# WHATSAPP_ACCESS_TOKEN_<STAGING_TENANT_UUID>=<STAGING_TEST_TOKEN>

# Optional; omit to leave AI replies disabled.
# OPENAI_API_KEY=<STAGING_KEY>
# OPENAI_MODEL=<STAGING_MODEL>

# Never set these on the staging web service:
# SAAS_OPERATOR_DATABASE_URL  -> approved operator shell only
# SAAS_TEST_DATABASE_URL       -> local/CI only
# PLATFORM_DEV_MODE=0
# PLATFORM_INSECURE_COOKIES=0
```

Rules:

- `SAAS_DATABASE_URL` must use `app_backend`, never `postgres` or `service_role`.
- `SAAS_PLATFORM_DATABASE_URL` must use `saas_platform_admin`.
- Both application URLs and `SUPABASE_URL` must resolve to the staging project, not the
  production reference.
- `SAAS_OPERATOR_DATABASE_URL` belongs only in the approved operator shell; it must be absent
  from Railway web-service variables.
- `SAAS_TEST_DATABASE_URL` must not be set on a deployed service.
- Do not copy any production database, WhatsApp, Supabase, AI, or platform secret.

## 4. Safe configuration validation

`db/staging_preflight.py` is deliberately offline: it parses configuration and performs no
database connection. It validates environment declaration, production-project exclusion,
application roles, split projects, WhatsApp state, operator/test DSN placement, and insecure
flags.

The script gives the **process environment precedence** over `--env-file`. A production-loaded
shell can therefore override the staging file. Validate from a clean process that contains only
ordinary OS variables and the staging file:

```bash
env -i PATH="$PATH" HOME="$HOME" \
  python db/staging_preflight.py --env-file .env.staging
```

On Windows, use a new restricted Python process that does not inherit any `SAAS_*`,
`SUPABASE_*`, `PLATFORM_*`, `WHATSAPP_*`, or `OPENAI_*` variables. Do not run the preflight
from a shell that has production `.env` values loaded.

Required result:

```text
STAGING PREFLIGHT: PASS
```

If it says `REFUSED`, do not query the migration ledger, apply migrations, or deploy. Never
print the env file while investigating a failure.

The current `.env.staging` has already returned `REFUSED`; it points platform/Supabase
configuration at production and uses a privileged application role. It must be replaced with a
verified staging-only file before this validation is rerun.

## 5. Migration 0035: backup, status, apply, and rollback

Migration file:

```text
db/migrations/0035_menu_state_and_reply_correlation.sql
checksum sha256[:16] = 06fac1777fa3caaa
```

The staging branch already contains `0031`–`0034`. The integration adds only `0035`; applying
it to staging must be a separate owner-approved operation.

### 5.1 Backup first

- Take a staging Supabase snapshot/backup or PITR checkpoint before anything else.
- Record the backup timestamp and verify that a restore is possible.
- Do not use a production dump, production backup, or production credentials.
- The staging database must contain only synthetic data.

### 5.2 Status check

Use the staging **operator** DSN from the approved operator shell, never the Railway web
service. Prefer an env-file/secret-manager injection; do not put the DSN on a command line and
do not echo it.

```bash
export SAAS_DATABASE_URL='<staging operator DSN from approved secret source>'
python db/run_migrations.py --status
```

Expected before applying:

- `0031`–`0034` applied with the expected checksums;
- `0035_menu_state_and_reply_correlation.sql` pending;
- no unexpected, modified, or partially applied records.

Do not run `--include-local` or `--seed` against Supabase. They install development passwords
and fixture data and are refused by the runner for Supabase hosts.


### 5.3 Apply

Only after the backup, status check, and configuration preflight pass:

```bash
python db/run_migrations.py
```

Then verify the ledger and schema objects:

```sql
select version, checksum
from public.saas_schema_migrations
where version = '0035_menu_state_and_reply_correlation.sql';

select column_name
from information_schema.columns
where table_schema = 'public'
  and table_name = 'messages'
  and column_name = 'reply_to_provider_message_id';

select indexname
from pg_indexes
where schemaname = 'public'
  and indexname in (
    'messages_outbound_reply_unique',
    'conversations_menu_state_expiry_idx'
  );
```

Expected ledger checksum: `06fac1777fa3caaa`.

`0035` adds nullable/constant-default columns, validating CHECK constraints, and
**non-concurrent** indexes to existing tables. Use a maintenance window and statement/lock
timeouts; do not assume the local test timing applies to staging.

### 5.4 Rollback — no down migration

`0035` has **no down migration**. A code rollback must happen first; then an operator may
choose to restore the verified backup or manually remove the schema objects. Manual removal is
destructive:

```sql
-- Staging operator only, after code rollback and explicit approval.
drop index if exists public.messages_outbound_reply_unique;
drop index if exists public.conversations_menu_state_expiry_idx;

alter table public.conversations
  drop constraint if exists conversations_menu_state_object;
alter table public.messages
  drop constraint if exists messages_reply_to_provider_check;

alter table public.conversations
  drop column if exists menu_state,
  drop column if exists menu_state_expires_at,
  drop column if exists menu_state_version;
alter table public.messages
  drop column if exists reply_to_provider_message_id;

delete from public.saas_schema_migrations
where version = '0035_menu_state_and_reply_correlation.sql';
```

Consequences:

- Drops all staging menu state and reply-correlation data in the affected columns.
- The ledger row must not be removed until the schema objects are removed.
- A partial migration can leave some objects; inspect each `if exists` target first.
- Restoring the backup is safer than manual drops.
- Never run this procedure against production.
- The existing §7 rollback notes cover only `0034`; this section is the `0035` procedure.


## 6. Branch and push gate

The local `staging` branch is at `f267816`, which is an ancestor of the integration commit
`0c796c4`. A push would be a fast-forward and is technically conflict-free.

It is still blocked:

- staging application service is not confirmed;
- the available staging env configuration is refused by preflight;
- the staging migration ledger is unverified;
- there is no `origin/staging` branch;
- pushing the staging branch may trigger a deployment.

Do not push. The next action is an owner decision to provision or correct staging, followed by
a clean preflight, backup, and migration approval. After that, a separate approval can cover
the branch push and deployment.

## 7. Local evidence

- Integration `0c796c4` is intact and contains no `.load_seed.py`, `verify_admin_identity.py`,
  or dirty `docs/DATABASE_PLAN.md` edit.
- The full local suite passed: **686 tests, OK, 1 skipped**.
- Migrations `0031`–`0035` applied successfully only to a disposable local PostgreSQL 16
  database; that is not staging proof.
- No staging or production database was contacted.
- No push, deploy, restart, variable update, credential change, migration, menu enablement, or
  WhatsApp message occurred.
