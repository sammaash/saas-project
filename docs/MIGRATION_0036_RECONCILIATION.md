# Migration 0036 divergence — reconciliation proposal

**Status: decision taken — Option A implemented locally; nothing applied to any remote
database.** `db/migrations/0036_whatsapp_quotations.sql` now holds variant A's exact bytes
(checksum `9dd2c780deaecfad`), and the new objects arrive in the forward migration
`db/migrations/0037_quotation_briefs_and_deliveries.sql` (checksum `8b50f1e5308e8e23`).
No ledger row was edited, no checksum validation was bypassed, and no remote database was
queried or changed. See §7 for the decision and §5 for the audit that is still outstanding.

## 1. Two different files share one version string

| | Variant **A** (committed) | Variant **B** (this working tree) |
|---|---|---|
| Where | `644d578`, on `feature/whatsapp-quotations` and `fix/whatsapp-quotations-hardening` | uncommitted file in this checkout |
| Non-blank lines | 56 | 211 |
| Statements | 10 | 47 |
| `sha256[:16]` | `9dd2c780deaecfad` | `aa89f2d6be3a64a6` |
| `quotations` changes | `created_by_user_id` nullable, `source` / `customer_wa_id` / `idempotency_key` columns, three check constraints, one unique constraint, four column comments | identical DDL (one comment reworded) |
| New tables | none | `public.quotation_briefs`, `public.quotation_deliveries` (+ indexes, RLS forced, policies, grants, triggers) |

**Ancestry** (`git log --all -- db/migrations/0036_whatsapp_quotations.sql` shows only `644d578`):

```
platform-admin-dashboard (ca09523, production deploy target)
        └── 644d578  "feat: add WhatsApp-origin formal quotations"   ← variant A introduced
             └── cedd66f  fix/whatsapp-quotations-hardening (+13 vs HEAD)

feature/cindy-bakes-menu (fd8ab4a, this checkout's HEAD)  ← no 0036
        └── 644d578 …  feature/whatsapp-quotations (+12 vs HEAD)     ← variant A
```

Variant A is committed history on two quotation branches; variant B is newer, uncommitted
work (menu briefs + delivery bookkeeping) written on top of this branch. Neither
`platform-admin-dashboard` nor `staging` contains `0036` at all.

## 2. Why this cannot be left alone

`db/run_migrations.py` keys the ledger on the file name and treats a migration as immutable:

```python
if applied[path.name] != digest:
    raise SystemExit(f"{path.name} was modified after it was applied ...")
```

The check runs **before** anything pending is applied, so on any database that recorded
variant A, deploying variant B aborts the whole migration run (`SystemExit`), and
`--status` reports `CHECKSUM MISMATCHES`. Even if the ledger were bypassed, A never created
`quotation_briefs` / `quotation_deliveries`, so the quotation tools would fail at their first
write.

## 3. What is known about existing databases

| Database | Applied | `0035` checksum | `0036` checksum |
|---|---|---|---|
| `saas_cindy_menu_local` (local, disposable) | 36 | `06fac1777fa3caaa` | `aa89f2d6be3a64a6` (**B**) |
| `saas_cindy_fresh_verify` (local, disposable) | 36 | `06fac1777fa3caaa` | `aa89f2d6be3a64a6` (**B**) |
| `saas_cindy_verify` (local, earlier staging rehearsal) | 34 | — | — |
| `saas_phase1_test` (local) | 32 | — | — |
| `saas_0037_verify_test` (local, disposable; Option A verification) | 37 | `06fac1777fa3caaa` | `9dd2c780deaecfad` (**A**) |
| staging (the project `.env.staging` names) | **does not exist** — the host does not resolve | — | — |
| production (the database the live Railway service uses) | **not readable from here** | — | — |

No database reached from this workstation showed variant A before the reconciliation. Whether
a developer laptop, a throwaway Supabase project or a Railway service ran the
WhatsApp-quotations branch is **unknown**, and it cannot be answered without read-only access
to those systems. See the audit result at the end of §5.

## 4. Options

| # | Option | Works on a clean DB | Works on a DB with variant A | Verdict |
|---|---|---|---|---|
| A | **Forward-only**: restore `0036` to variant A's exact bytes and add `0037_quotation_briefs_and_deliveries.sql` with the new tables | yes (36 + 0037) | yes (0037 only) | **chosen** — see §7 |
| B | Keep variant B as the only `0036` | yes (36) | **no** — migration run aborts | acceptable only if A never touched any database, including every developer machine |
| C | Delete `0036` and re-issue the whole thing as `0037` | yes | leaves an orphan ledger row; the 0036 checksum can no longer be verified | rejected — loses the applied history for no benefit over A |
| D | Update the ledger by hand (`update saas_schema_migrations set checksum = …`) and create the missing objects manually | — | "works" | **rejected** — unverifiable, unauditable, and the next `--status` cannot tell what actually ran |

Option A keeps one truth: migrations are append-only, every environment converges, and a
database that has variant A needs no surgery. Its cost is that the new tables arrive in
`0037`, so the schema document, the checksum list and the applied count become 37 rather
than 36.

## 5. Read-only audit before choosing

Run against every candidate database (local, staging, any Railway/Supabase project someone
may have used for the quotation branch):

```sql
select version, checksum from public.saas_schema_migrations
 where version = '0036_whatsapp_quotations.sql';
-- 9dd2c780deaecfad = variant A | aa89f2d6be3a64a6 = variant B | no row = clean

select to_regclass('public.quotation_briefs'),
       to_regclass('public.quotation_deliveries');
-- both NULL = variant A or clean | both present = variant B

select column_name from information_schema.columns
 where table_schema = 'public' and table_name = 'quotations'
   and column_name in ('source', 'customer_wa_id', 'idempotency_key');
```

Interpretation:

* **No `0036` row** → clean; either option applies.
* **`9dd2c780deaecfad`** → variant A; only option A is safe.
* **`aa89f2d6be3a64a6`** → variant B; option B is safe, and option A must not be applied there.

### Audit result (read-only, 2026-10-26)

| Candidate | Outcome |
|---|---|
| staging (the project `.env.staging` names) | **No such host** — the project was never created, so there is no ledger to read |
| production (the database behind the live Railway service) | **Not reachable**: every Postgres port (5432/6543) times out from this workstation, and no role below the owner can read `public.saas_schema_migrations` anyway |
| local disposable databases | the only readable ledgers; all of them recorded variant **B**, none recorded A |

The audit therefore **could not clear variant A**, which is why option B was not chosen: it is
safe only if A never ran anywhere, and that cannot be proven from here. Option A is safe for
every state that *can* be confirmed and for the one that cannot, so it was taken.

These queries must still be run from a host with approved read-only access **before** any
environment is migrated, because a database that recorded variant B cannot accept option A in
place and must be rebuilt instead.

## 6. What the chosen option means for the staging runbook

* **Option A (chosen)** — `docs/STAGING_ENVIRONMENT.md` §5.3 applies `0035`, `0036` (A) and
  `0037`: 37 files, `0036` checksum `9dd2c780deaecfad`, `0037` checksum `8b50f1e5308e8e23`.
* **Option B (rejected)** — 36 files, `0036` checksum `aa89f2d6be3a64a6`. It would abort the
  whole migration run on any database that had recorded variant A.

Either way the same review gate applies: commit the migration file, then apply it to
staging from the operator shell, then deploy code. Production stays untouched.

## 7. Decision taken — Option A

1. **A or B?** **Option A.** The audit in §5 could not clear variant A: staging has no
   database at all, and the production ledger is not readable from a workstation (see the
   audit result in §5). Option A is the only strategy that is safe for a clean database, for
   a database that recorded variant A and for the confirmed variant B databases, so the risk
   of an unreachable environment holding A is removed rather than accepted.
2. **Who confirms the remote databases?** Still open. It needs approved read-only access from
   a host that can reach the Postgres port; it cannot be settled from this checkout. Until it
   is, no environment is migrated.
3. **Where does the quotation branch land?** The reconciliation is now in the file itself:
   `0036` is variant A, so those 12 commits sit on top of a `0036` that already matches their
   history and the merge is a no-op for the migration. The new tables come from `0037`, which
   that branch does not contain.
4. **Implementation state.** Done locally and verified against a disposable database:
   37 migrations applied, `0036` = `9dd2c780deaecfad`, `0037` = `8b50f1e5308e8e23`, RLS
   enabled **and forced** on both new tables with the tenant-predicate policies for
   `app_backend` alone and no grant to `anon`/`authenticated`/`service_role`.
   `REQUIRED_SCHEMA` now names both tables, so the deploy order is migration first, then code.

Until the audit in §5 is completed by someone with approved access, this checkout applies
nothing to any remote database.

## 8. Follow-up — done, and what remains

Done with this decision:

* `REQUIRED_SCHEMA` in `platform_admin/config.py` now names `public.quotation_briefs` and
  `public.quotation_deliveries`, so a database that has not applied `0037` fails the boot check
  with the missing table name instead of letting the first customer quotation fail at runtime.
  The deploy order is therefore migration first, then code — and a `0037` rollback must be
  paired with a code rollback, which `docs/STAGING_ENVIRONMENT.md` §7 states.
* Structural tests assert the ledger checksums, RLS (enabled **and** forced), the
  tenant-predicate policies, the `app_backend`-only grants, the indexes and the `updated_at`
  triggers.
* Migration-runner tests prove that a database recording variant A applies `0037`, that a
  database recording variant B is refused **before** any pending migration runs, and that
  `0037` refuses to adopt a pre-existing table.

Remaining: the read-only audit in §5, then commit, then apply to staging from the operator
shell, then deploy. Production stays untouched until that sequence is authorised.

## Appendix — how the checksum is computed

`sha256(sql_text)[:16]`, where the file is read in text mode. Python's universal-newline
reading normalises CRLF, so the digest is identical on Windows and Linux checkouts; verified
on both reconciled files:

```text
0036_whatsapp_quotations.sql   (variant A, restored)
    sha256(raw bytes)          9dd2c780deaecfad
    sha256(normalised LF text) 9dd2c780deaecfad

0037_quotation_briefs_and_deliveries.sql   (new)
    sha256(raw bytes)          8b50f1e5308e8e23
    sha256(normalised LF text) 8b50f1e5308e8e23
```

The two digests coincide here only because both files are stored with LF endings
(`.gitattributes` sets `* text=auto eol=lf`). The digest the ledger records is always the
normalised-text one, which is why a Windows checkout and a Linux deploy agree.

Do not re-encode, reformat or re-wrap an applied migration: any byte change other than a
line ending produces a different digest and a refused run.
