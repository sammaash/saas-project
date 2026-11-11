# Channel credentials (Phase 1a)

How a per-channel WhatsApp credential is stored, read, rotated and revoked, and what
an operator must do before migration `0040_channel_credentials.sql` is applied.

**Scope.** This document describes Phase 1a only. Embedded Signup, managed
provisioning and channel onboarding are **not implemented**; a channel is still
created by a platform administrator entering identifiers by hand, and a channel with
no stored credential keeps using the server environment variable described below.

## 1. The model in one paragraph

A credential belongs to **one channel** and is stored as AES-256-GCM **ciphertext** in
`public.channel_credentials`. The key never reaches PostgreSQL, so a database dump on
its own cannot be turned into a token. No application role holds any privilege on the
table: the only read path is `app.channel_credential(uuid)`, a `SECURITY DEFINER`
function that requires tenant context and returns at most one row for one channel.
Everything else — writing, rotating, revoking — goes through two platform functions
that require a verified platform administrator and derive the tenant from the channel.

## 2. Objects

| Object | Owner | Purpose |
|---|---|---|
| `public.channel_credentials` | migration/owner role | Ciphertext, nonce, `key_ref`, fingerprint, lifecycle timestamps |
| `app.channel_credential(uuid)` | `saas_platform_provisioner` | The only read path. One row, tenant-gated |
| `app.platform_store_credential(...)` | `saas_platform_provisioner` | Encrypt-and-store, rotating any live credential of the same kind |
| `app.platform_revoke_credential(...)` | `saas_platform_provisioner` | Soft revocation + audit |

Row-level security is **enabled and deliberately not forced**. Forcing it would subject
the table owner (the migration role) to RLS, and because the owner has no policy a
later data migration would silently read and update **zero** rows. This is the opposite
of `public.tenant_channels`, which is not forced because a `SECURITY DEFINER` helper
*owned by the table owner* reads it.

## 3. Environment variables

Names only; values are never committed and never logged.

| Variable | Meaning |
|---|---|
| `CHANNEL_CREDENTIAL_KEYS` | JSON object mapping a key reference to a base64-encoded **32-byte** key, e.g. `{"v1":"<32 bytes, base64>"}`. Multiple keys are allowed so a rotation can overlap. |
| `CHANNEL_CREDENTIAL_ACTIVE_KEY_REF` | Which reference new writes use. May be omitted when exactly one key is configured; required for two or more. An unknown reference is a configuration error, never a silent fallback. |

The keyring is read lazily, and only when a vault-managed credential is involved. A
deployment with no keyring configured still works for channels that use the manual
environment-variable token, which is why Phase 1a can ship before any channel is
migrated to the vault.

## 4. Manual environment-variable compatibility (unchanged)

Channels created before Phase 1a have no vault row and keep working exactly as before:

* the token is read from `WHATSAPP_ACCESS_TOKEN_<TENANT_UUID>` where the UUID's hyphens
  are replaced by underscores;
* `tenant_channels.credentials_ref` must still be exactly
  `tenants/<tenant_id>/whatsapp` (enforced by the `0022` check constraint);
* every send passes the channel's id as well, so resolution is channel-scoped, but a
  channel without a vault row falls through to the environment variable.

The distinction is deliberate and is the whole point of the accessor returning inactive
rows:

| Database state | Behaviour |
|---|---|
| No row for the channel | Manual path (environment variable) |
| Live row | Vault path |
| Row with `revoked_at` set | **Refuses to send.** It does not fall back to the environment |
| Row with `expires_at` in the past | **Refuses to send.** Same reason |

A revoked credential must never keep working because an environment variable happens
to be present.

## 5. Key rotation (no Meta involvement)

1. Add the new key to `CHANNEL_CREDENTIAL_KEYS` and deploy; point
   `CHANNEL_CREDENTIAL_ACTIVE_KEY_REF` at it. Existing rows still decrypt with the old
   `key_ref`, so nothing breaks.
2. Re-encrypt existing rows, one tenant at a time, with an operator script that
   decrypts with the row's `key_ref` and re-encrypts with the active key
   (`credential_vault.lookup` → `encrypt_secret` → `platform_store_credential`, or a
   dedicated rotation script once written). `rotated_from` records the predecessor.
3. Verify: `select key_ref, count(*) from public.channel_credentials where revoked_at is null group by 1;`
   should show a single key.
4. Remove the old key from the keyring after the next deploy.

**Credential rotation** (a new Meta token for the same channel) is `store_credential`
alone: it revokes the live row of that kind and inserts the new one in a single
transaction, so the partial unique index `(channel_id, kind) where revoked_at is null`
is never violated and there is no window with two live tokens or none.

## 6. Revocation

`revoke_credential(channel_id, kind, reason)` sets `revoked_at` and writes
`credential.revoked` to `audit_log`. Rows are never deleted: they are the evidence that
a credential existed and when it stopped being valid. There is no `DELETE` grant on the
table at all.

After revocation the channel's sends fail with a 503 that names the reason, and the
failure is visible in `messages.error_code` on the affected rows.


## 7. Applying migration 0040 — prerequisites and checks

**Do not apply this migration as part of this change.** Application is a separate,
approved deployment step.

Before applying:

1. A restore point exists for the target database (see `docs/STAGING_ENVIRONMENT.md`).
2. The database has migrations `0001`–`0039` recorded in `public.saas_schema_migrations`
   with matching checksums. Read them from the ledger, or with
   `python db/run_migrations.py --database-url <DSN> --status`. The committed runner
   accepts two kinds of target only: approved loopback development databases, and the
   validated staging Railway target (`postgres.railway.internal:5432/railway`, reachable
   only from inside the staging project's private network). `--status` performs reads
   only, but it exits 0 even when it prints `CHECKSUM MISMATCHES` — read its output,
   not just the exit code.
3. `CHANNEL_CREDENTIAL_KEYS` is not required to apply the migration (it governs the
   application), but it must be configured before any channel is vault-managed.

Apply through the guarded runner only, then verify:

```sql
-- 1. RLS on, and deliberately not forced
select relrowsecurity, relforcerowsecurity
  from pg_class where oid = 'public.channel_credentials'::regclass;   -- (true, false)

-- 2. exactly one policy, for the function owner
select policyname, roles::text from pg_policies
 where tablename = 'channel_credentials';                             -- 1 row: {saas_platform_provisioner}

-- 3. no application role may read the table
select has_table_privilege('app_backend',   'public.channel_credentials', 'SELECT'), -- false
       has_table_privilege('authenticated', 'public.channel_credentials', 'SELECT'), -- false
       has_table_privilege('anon',          'public.channel_credentials', 'SELECT'), -- false
       has_table_privilege('saas_platform_provisioner', 'public.channel_credentials', 'SELECT'); -- true

-- 4. function ownership
select p.proname, pg_get_userbyid(p.proowner) as owner
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'app'
   and p.proname in ('channel_credential', 'platform_store_credential',
                     'platform_revoke_credential');                   -- owner = saas_platform_provisioner

-- 5. R27: the grant without which every send fails with 42501
select has_function_privilege('saas_platform_provisioner',
                              'app.current_tenant_id()', 'EXECUTE');  -- true

-- 6. no role has BYPASSRLS (0013 cannot enforce this on hosted PostgreSQL)
select rolname, rolbypassrls from pg_roles
 where rolname in ('saas_platform_provisioner', 'saas_platform_admin'); -- both false
```

If check 6 fails, **stop**: the design does not require `BYPASSRLS`, and granting it
would make the vault's RLS decorative.

## 8. R31 — data migrations against FORCE-RLS tables

Twenty-two tables in this schema (including `customers`, `conversations`, `messages`
and `orders`) are `FORCE`-RLS and owned by the migration role. A data migration that
runs as a **non-superuser owner** against one of them is filtered by RLS and updates or
reads **zero rows, silently** — an `UPDATE` that changes nothing and a verification
`SELECT` that returns nothing, which reads as "already correct".

Rules for any migration from `0040` onward that performs statement-level
`INSERT`/`UPDATE`/`DELETE`:

1. State the expected affected row count in the file, e.g. `-- expected rows: 3`.
2. Assert it: `GET DIAGNOSTICS n = ROW_COUNT;` and raise when it differs.
3. Document which role the migration runs as and why it can see the intended rows
   (superuser, a temporary owner policy, or a tenant-scoped `app_backend` block).

The rule is enforced by
`tests/test_channel_credentials_migration.py::R31DataMigrationTests`, which ships with
the later credential-vault application release rather than the migration-only release
this document accompanies. A migration that
legitimately affects no rows is allowed to say so explicitly.

## 9. Known limitations in Phase 1a

| Limitation | Consequence |
|---|---|
| No durable quarantine table | An unroutable channel is acknowledged with **200** and recorded as a structured, PII-free log line (`whatsapp_quarantine`), never in the database. Meta retries a non-200 for up to seven days, which is why the 404 was removed. A queryable record needs an approved migration. |
| Media messages are still skipped | A customer sending an image gets no reply. The rest of the batch is no longer lost, but the media is not processed or stored. |
| `WHATSAPP_SEND_ENABLED` is still global | There is no per-channel send gate; opening sending opens it for every channel in the process. |
| No connection timeout on `tenant_transaction` | A black-holed database blocks the request instead of failing fast. Tests must patch every service that opens a transaction, and a `connect_timeout` in the DSN is advisable operationally. |
| `tenant_channels` lifecycle columns do not exist yet | `connection_status`, `connection_method` and the rest belong to migration 0041, deliberately outside this phase. |

## 10. Tests

The tests below accompany the credential-vault application code and are part of the
later application release; they are not included in the migration-only release this
document accompanies.

```bash
python -m pytest tests/test_credential_vault.py tests/test_channel_credentials_migration.py -q
```

`test_credential_vault.py` covers the keyring, round trip, tampering, rotation,
revocation, expiry, the absence of plaintext in messages and reprs, and the SQL shape
of store/revoke. `test_channel_credentials_migration.py` asserts the privilege model
from the migration text, because database-backed assertions need a database this
environment does not provide.
