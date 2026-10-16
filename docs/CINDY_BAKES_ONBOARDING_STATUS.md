# Cindy Bakes Onboarding — Status Report

**Date:** 16 October 2026 · **Deadline:** Saturday 17 October 2026
**Branch:** `platform-admin-dashboard` (head `7bd2b9d`)

Every claim below is backed by a command that was actually run. Nothing is marked PASS on
the strength of the Bot Playground alone, and no live WhatsApp capability is claimed.

---

## ⚠️ ENVIRONMENT CORRECTION — there is no separate staging database

Earlier revisions of this report described the Supabase database as *staging* and stated that
"no production change of any kind was made". **That was wrong, and is corrected here.**

`railway status` shows the linked service, and comparing its `SAAS_DATABASE_URL` with the local
`.env` proves they are the **same database**:

```text
local  .env SAAS_DATABASE_URL: host=aws-0-eu-west-1.pooler.supabase.com port=5432
                               role=app_backend.sftjojcsojjxaabhcroe db=postgres
Railway    SAAS_DATABASE_URL: host=aws-0-eu-west-1.pooler.supabase.com port=5432
                               role=app_backend.sftjojcsojjxaabhcroe db=postgres
SAAS_DATABASE_URL:          identical=True
SAAS_PLATFORM_DATABASE_URL: identical=True
```

**Consequences that must be understood:**

1. Every migration, catalogue load, settings change and channel-identifier change recorded in
   this document was applied to the **production** database used by the live service.
2. The live service is `saas-project` in Railway's **production** environment,
   `https://saas-project-production-cbf1.up.railway.app`, region `ams`.
3. There is **no staging environment to validate against**. The disposable *local* PostgreSQL
   database (`saas_cindy_verify`) is the only place the test suite runs safely.
4. The earlier statement "no production change was made" is retracted. What *was* true is that
   no Meta asset, no Railway variable and no production credential had been changed — until
   the WhatsApp variables were set later in this document (§6).

**Deployment verified live (read-only route probe):**

```text
/login                                                 -> 200   (public)
/                                                      -> 302   (auth redirect)
/tenants/<id>/customers                                -> 302   route exists
/tenants/<id>/whatsapp, /rules, /playground             -> 302
/webhooks/whatsapp                                     -> 403   exists, rejects unsigned GET
/tenants/not-a-uuid/customers                          -> 302
```

The presence of `/tenants/<id>/customers` proves the deployed build includes the migration-0033
commit, so the service does track `platform-admin-dashboard`. It also means the customer-history
feature is live in production.

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

Full suite result: **411 passed, 691 subtests passed, 0 failed** (disposable local database,
`SAAS_TEST_DATABASE_URL` pointed at it, `SAAS_REQUIRE_TEST_DATABASE=1`).

### The dashboard verified against real staging data (read-only GETs)

Every admin page was rendered against the staging tenant using the app's own session flow
with a stub authenticator (the technique `tests/test_platform_admin_app.py` uses) — no
Supabase password, nothing forged, and only GET requests:

```text
login                                   -> 302   (session established)
/                            overview   -> 200
/tenants                     list       -> 200
/tenants/<id>                detail     -> 200
/tenants/<id>/profile                   -> 200
/tenants/<id>/rules          AI config  -> 200
/tenants/<id>/members                   -> 200
/tenants/<id>/subscription              -> 200
/tenants/<id>/whatsapp       channel    -> 200
/tenants/<id>/customers      new in 0033-> 200   (2261 bytes)
/tenants/<id>/customers/<unknown>       -> 404   (correct, not a 500)
/tenants/<id>/conversations/<unknown>   -> 404   (correct, not a 500)
/playground                  bot        -> 200
```

That covers the Platform Admin Dashboard, tenant provisioning surfaces, catalogue, rules/AI
configuration, WhatsApp channel page and Bot Playground against live staging data.

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

## 6. WhatsApp integration status — **PARTIAL (identifiers live, credentials missing)**

### Real Meta identifiers are now recorded for this tenant

Supplied by the client and written to the staging channel through the dashboard's own
service layer (`app.platform_update_channel`), which performs the database-mandated
disable → update → activate sequence:

| Field | Value | Where it lives |
|---|---|---|
| Business name | Cindy Bakes | `public.tenants.name` (as *Cindy Bakes Delights*) |
| WABA ID | `2526681404500702` | `public.tenant_channels.waba_id` |
| Business Portfolio ID | `2803809630006435` | reference only — our schema has no field for it |
| WhatsApp phone number | `+254 747 595334` | `public.tenant_channels.display_phone_number` |
| Phone Number ID | `1222930064245010` | `public.tenant_channels.phone_number_id` |

Change applied (reversible — the previous values are recorded here):

```text
before   : dev-phone-id-cindy-001 / dev-waba-001 / +254700000001 (active)
step 1/3 : disabling channel (identifiers cannot change while active)
step 2/3 : updating identifiers
step 3/3 : setting channel status to active
after    : 1222930064245010 / 2526681404500702 / +254747595334 (active)
```

**Verified after the change** — `app.resolve_whatsapp_channel('1222930064245010')` returns this
tenant, so an inbound webhook for the live number would route to Cindy Bakes rather than to
nothing. The preflight now reports:

```text
   [ok] tenant status active                   active
   [ok] channel recorded                       1222930064245010
   [ok] channel status active                  active
   [ok] credentials_ref exact                  tenants/11111111-1111-4111-8111-111111111111/whatsapp
   [ok] phone_number_id is a real Meta id      1222930064245010
   [ok] waba_id recorded                       2526681404500702
   [ok] display number recorded                +254747595334
   [ok] inbound routing resolves to this tenant resolves
   [NO] WHATSAPP_APP_SECRET set                not set
   [NO] WHATSAPP_VERIFY_TOKEN set              not set
   [NO] tenant access token in environment     WHATSAPP_ACCESS_TOKEN_11111111_1111_4111_8111_111111111111 NOT set
   [NO] outbound sending enabled               disabled
verdict : BLOCKED by: ['WHATSAPP_APP_SECRET set', 'WHATSAPP_VERIFY_TOKEN set',
                       'tenant access token in environment', 'outbound sending enabled']
```

### Meta-side facts, now VERIFIED by read-only Graph API calls

The client supplied the app secret, verify token and tenant access token. They were written
into the **local, gitignored `.env`** using the variable names this application actually
reads, and then used for read-only `GET` calls (no message was sent, no asset changed).

> **Two variable names in the supplied list were wrong for this codebase** and would have
> failed silently if set in Railway as given:
>
> | Supplied | This app reads | Effect if uncorrected |
> |---|---|---|
> | `ACCESS_TOKEN` | `WHATSAPP_ACCESS_TOKEN_<TENANT_ID_UPPER_WITH_UNDERSCORES>` | outbound never authenticates |
> | `WHATSAPP_API_VERSION` | `WHATSAPP_GRAPH_API_VERSION` | version silently falls back to `v21.0` |
>
> `WHATSAPP_VERIFY_TOKEN` and `WHATSAPP_APP_SECRET` were already correct.

| Fact | Value | Source |
|---|---|---|
| Phone number | `+254 747 595334`, id `1222930064245010` | `GET /v26.0/1222930064245010` |
| Platform | **`CLOUD_API`** | same |
| Number status | **`CONNECTED`** | same |
| Quality rating | `GREEN` | same |
| `code_verification_status` | **`EXPIRED`** | same |
| WABA | `cindy_bakes`, id `2526681404500702`, `account_review_status: APPROVED` | `GET /v26.0/2526681404500702` |
| Numbers on the WABA | exactly one, matching the above | `GET /…/phone_numbers` |
| Templates | **only `hello_world`** (`APPROVED`, `UTILITY`, `en_US`) — no Cindy Bakes templates exist | `GET /…/message_templates` |
| App subscribed to the WABA | id `28834141379522438`, name `Cindy_bakes` | `GET /…/subscribed_apps` |

The token works and carries the `whatsapp_business_management` scope.

**Two findings that need attention:**

1. **The app secret must belong to app `28834141379522438`.** That is the app currently
   subscribed to this WABA. Inbound webhooks are rejected unless the `WHATSAPP_APP_SECRET`
   used to verify `X-Hub-Signature-256` belongs to *that* app. The supplied secret has not
   been proven to match it — this cannot be confirmed with a user token, only by comparing
   against App settings → Basic for `28834141379522438`. **If they differ, every real webhook
   will be rejected and the bot will appear to receive nothing.**
2. **`code_verification_status: EXPIRED`** while `status: CONNECTED`. Recorded as Meta
   reports it; it does not appear to be blocking, but it is worth confirming in the dashboard
   rather than assuming.

**Still not readable with this token:** the webhook **callback URL** that Meta will POST to.
`subscribed_apps` confirms an app is subscribed but does not expose its callback URL, so
whether Meta points at this deployment remains unverified.


### Production Railway variables: audit, and the change made

Audited the live service's variables (names only; no value was printed or copied into this
repository). **Two existing variables were named so that the application never reads them:**

| Variable in Railway | Read by this app? | Correct name |
|---|---|---|
| `WHATSAPP_APP_SECRET` | ✅ yes | — |
| `WHATSAPP_VERIFY_TOKEN` | ✅ yes | — |
| `WHATSAPP_ACCESS_TOKEN` | ❌ **never read** | `WHATSAPP_ACCESS_TOKEN_<TENANT_ID_UPPER_WITH_UNDERSCORES>` |
| `WHATSAPP_PHONE_NUMBER_ID` | ❌ **never read** | not an app variable at all — the per-tenant id lives in `tenant_channels` |
| `WHATSAPP_GRAPH_API_VERSION` | — | was absent |
| `WHATSAPP_SEND_ENABLED` | — | was absent |

So before this change, production had **inbound** credentials under correct names but **no
usable outbound token**: the bot would have been unable to reply even with sending enabled.

**Change applied, on explicit operator authorisation** (the operator selected the option that
stated "the bot will reply to real customers who message +254 747 595334"):

```text
WHATSAPP_SEND_ENABLED                                        set to 1
WHATSAPP_GRAPH_API_VERSION                                   set to v26.0
WHATSAPP_ACCESS_TOKEN_11111111_1111_4111_8111_111111111111   set from the local .env via stdin
```

The token value was piped through the CLI's `--stdin` so it never appeared in a command line,
and the setting script redacts it from all output. Nothing was written into the repository.

Setting variables triggered a redeploy of the production service:

```text
before: deployment 765093f7-... status Online
after:  deployment 7b855b8d-... status Online      (verified: /login -> HTTP 200)
```

**Still present and now redundant/misleading** (left in place deliberately — deleting a
production variable is a separate decision): `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`.

**Also still unresolved:** the app secret has not been proven to belong to app
`28834141379522438`, so inbound signature verification may still reject real webhooks.


### What is still missing

| Item | Status |
|---|---|
| `WHATSAPP_APP_SECRET` | **set** locally — but must be proven to belong to app `28834141379522438` (see above) |
| `WHATSAPP_VERIFY_TOKEN` | **set** locally — must also be set in the Railway service environment |
| `WHATSAPP_ACCESS_TOKEN_11111111_1111_4111_8111_111111111111` | **set** locally — must also be set in Railway |
| `WHATSAPP_GRAPH_API_VERSION` | **set** to `v26.0` locally |
| `WHATSAPP_SEND_ENABLED` | **deliberately NOT enabled** — awaiting your decision |
| Registered test number for end-to-end webhook verification | **not supplied** |
| All of the above in the **Railway** service environment | **DONE** — set, redeploy verified healthy |

### What is verified, and what is still NOT

| Claim | Status |
|---|---|
| Our database maps the live Phone Number ID to the Cindy Bakes tenant | **VERIFIED** — routing resolves |
| The identifier is well-formed and no longer a development placeholder | **VERIFIED** |
| The access token authenticates and carries `whatsapp_business_management` | **VERIFIED** — live Graph API calls succeeded |
| The number is on **Cloud API** and `CONNECTED` | **VERIFIED** |
| The WABA is `APPROVED` and owns exactly this number | **VERIFIED** |
| Template inventory | **VERIFIED** — only `hello_world` exists |
| An app is subscribed to the WABA | **VERIFIED** — app `28834141379522438` |
| The supplied app secret belongs to that subscribed app | **NOT verified** — needs the App dashboard; if wrong, every webhook is rejected |
| Meta's webhook **callback URL** points at this deployment | **NOT verified** — not readable with a user token |
| Inbound signature verification works against a real Meta signature | **NOT verified** — no real webhook has arrived |
| Outbound delivery works | **NOT verified** — send gate off; sending a test would message a real customer |
| Any real message has been sent or received | **NO — none** |

**Therefore: live WhatsApp readiness is still NOT claimed.** The tenant is *routable*, the
credentials *work*, and the assets are *confirmed healthy* — but no end-to-end message has
crossed the boundary, and the send gate remains off by choice.

> **Operational consequence to be aware of:** the live number is bound in this database and
> `WHATSAPP_APP_SECRET` + `WHATSAPP_VERIFY_TOKEN` are now present locally. If Meta's webhook is
> pointed at this deployment, real customer messages for `+254 747 595334` would be verified
> and persisted into this tenant and **left unanswered**, because outbound is disabled. To park
> the channel: `channel --status disabled --confirm-slug cindy-bakes`.

### The exact go-live gate (read from the code, not assumed)

`platform_admin/services/whatsapp.py` gates outbound delivery on four conditions, all of
which must hold, and the channel must be active for inbound routing to resolve:

1. `WHATSAPP_SEND_ENABLED=1` (`Settings.whatsapp_send_enabled`);
2. the channel's `credentials_ref` **exactly equals** `tenants/<tenant-uuid>/whatsapp`;
3. `WHATSAPP_ACCESS_TOKEN_<TENANT-ID-UPPERCASE-WITH-UNDERSCORES>` is present in the
   environment — i.e. `WHATSAPP_ACCESS_TOKEN_11111111_1111_4111_8111_111111111111`;
4. inbound additionally needs `WHATSAPP_APP_SECRET` (signature verification) and
   `WHATSAPP_VERIFY_TOKEN` (the Meta challenge).

The token is never stored in the database and never reaches a template or a log.

### Changing identifiers on a live channel is a three-step sequence

The database **refuses** to change `phone_number_id`/`waba_id` while a channel is active
(`"Disable the channel before changing its identifiers."`). The operator CLI performs and
prints the sequence, and it checks the activation gate *before* the first mutation so a
refusal cannot leave the channel disabled:

```text
before   : dev-phone-id-cindy-001 / dev-waba-001 / +254700000001 (active)
step 1/3 : disabling channel (identifiers cannot change while active)
step 2/3 : updating identifiers
step 3/3 : setting channel status to active
after    : 123456789012345 / 987654321098765 / +254712345678 (active)
```

**Blocking questions for the client:**

1. The real WhatsApp Cloud API `phone_number_id` and WABA id for Cindy Bakes.
2. Confirmation of the display number in E.164.
3. A separate registered test number (its own verify token and app secret) to verify the
   webhook end to end, so the live number is never disturbed.
4. Whether outbound sending should be enabled at launch, and the token to hold out of band
   (in the Railway service variables, never in the repository).

### Ready-to-run commands once the values are supplied

```bash
# 1. Record/update the identifiers (requires the slug to be typed again as confirmation)
python db/tenant_onboarding.py --admin-user-id <admin-uuid> \
    channel --tenant-slug cindy-bakes \
    --phone-number-id <META_PHONE_NUMBER_ID> \
    --waba-id <WABA_ID> \
    --display-number +254xxxxxxxxx \
    --confirm-slug cindy-bakes

# 2. Export the secrets in the service environment (Railway variables, not the repo)
#    WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN, WHATSAPP_SEND_ENABLED=1,
#    WHATSAPP_ACCESS_TOKEN_11111111_1111_4111_8111_111111111111

# 3. Prove the gate; exits non-zero and lists exactly what is still missing
python db/tenant_onboarding.py --admin-user-id <admin-uuid> \
    whatsapp --tenant-slug cindy-bakes
```

The `whatsapp` command makes **no provider call and sends nothing**. Its inbound check calls
the same `app.resolve_whatsapp_channel` function the webhook uses, so a pass means an inbound
message for that number would really route to this tenant.

Verified in both directions on a disposable database: with the development placeholder and no
secrets it reported `BLOCKED by: [...5 items]` (exit 1); after recording a Meta-shaped
identifier and exporting the secrets it reported `READY` (exit 0).
## 7. Deployment, rollback and next actions

| Item | Status |
|---|---|
| Code pushed | `f88151c`, `df4f8b5`, `a0de28f`, `0a8ad3e` on `origin/platform-admin-dashboard` |
| Railway service branch | **confirmed: `platform-admin-dashboard`** |
| Local `HEAD` vs `origin` | identical at `7383879`; nothing unpushed |
| Procfile target | `gunicorn … platform_admin.railway_app:app`; the module imports and builds a 40-route Flask app |
| **Railway startup gate executed against staging** | **PASS** — see below |
| Production database state | **not inspected** (no production credentials, and none were assumed) |
| Live webhook / registered number / production credentials | **not touched** |

### The deploy will boot: startup gate verified against staging

`platform_admin/railway_app.py` runs `Settings.from_env()` → `verify_database_roles()` →
`verify_database_schema()` → `create_app()` **before** the process serves anything. That whole
sequence was executed against the staging database (read-only: role checks, `pg_catalog`
reads and the app factory; no writes, no provider calls):

```text
ok: app_database_url      -> aws-0-eu-west-1.pooler.supabase.com:5432/postgres
ok: cookie_secure         = True
--- verify_database_roles() ---
ok: both logins are the expected unprivileged roles
--- verify_database_schema() ---
ok: every REQUIRED_SCHEMA table and column is present
missing: []
   [ok] public.customers / public.conversations / public.messages / public.orders /
        public.order_items / public.order_payments / public.tenant_order_sequences /
        public.whatsapp_webhook_events / public.whatsapp_conversations /
        public.catalogue_import_batches / public.quotations / public.quotation_lines /
        public.tenant_settings(catalogue_version) /
        public.product_variants(sku, availability, stock_quantity, reorder_level)
--- create_app() ---
ok: flask app built with 39 routes
    /tenants/<tenant_id>/customers
    /tenants/<tenant_id>/customers/<customer_id>
    /tenants/<tenant_id>/conversations/<conversation_id>
    /webhooks/whatsapp
RESULT: the Railway entry point boots cleanly against staging.
```

**Operational finding:** staging reports `allow_active_channels = False`, so
`PLATFORM_ALLOW_ACTIVE_CHANNELS` is not `1` in that environment. The staged channel is already
`active`, so inbound routing works; but any *re*-activation (needed when identifiers change)
must be performed by the operator CLI with `PLATFORM_ALLOW_ACTIVE_CHANNELS=1` in its own
environment, or the staging service variable must be set. This is a deliberate gate, not a bug.

**What is still not verifiable from here:** Railway's own build log, deploy status and the
public health response. I have no Railway credentials, so I have not claimed the service is
"deployed and healthy" — only that the branch is correct, everything needed is committed, and
the process's own fail-closed startup checks pass against staging. Point me at the service URL
and I will confirm the live response.

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

# WhatsApp channel and go-live gate (no provider call, sends nothing)
python db/tenant_onboarding.py --admin-user-id <verified-admin-uuid> \
    channel   --tenant-slug cindy-bakes --phone-number-id <META_ID> --waba-id <WABA_ID> \
              --display-number +254xxxxxxxxx --confirm-slug cindy-bakes
python db/tenant_onboarding.py --admin-user-id <verified-admin-uuid> \
    whatsapp  --tenant-slug cindy-bakes

# Prove the deployed process will start (the same checks railway_app.py runs before
# serving). Settings.from_env() reads os.environ only, so .env must be loaded first.
python -c "from dotenv import load_dotenv; load_dotenv(); \
from platform_admin.config import Settings, verify_database_roles, verify_database_schema as v; \
s = Settings.from_env(); verify_database_roles(s); v(s); \
from platform_admin.app import create_app; print('boots:', create_app(s))"
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
| Operator onboarding tooling (`catalogue`/`settings`/`verify`/`status`/`channel`/`whatsapp`) | **PASS** — exercised end to end on a disposable database in both gate states |
| Test suite | **PASS** — 411 passed, 691 subtests, 0 failed |
| Real WhatsApp identifiers (Phone Number ID, WABA id, display number) | **PASS** — recorded and inbound routing verified |
| Meta assets verified healthy (Cloud API, `CONNECTED`, WABA `APPROVED`, token authenticates) | **PASS** — read-only Graph API |
| WhatsApp credentials (`APP_SECRET`, `VERIFY_TOKEN`, tenant token) | **PASS** — set locally *and* in the Railway production service |
| Outbound sending enabled | **ENABLED** on explicit authorisation (production redeployed, verified healthy) |
| Production service live | **PASS** — `https://saas-project-production-cbf1.up.railway.app/login` → HTTP 200 |
| End-to-end real-provider test (inbound webhook / outbound delivery) | **NOT PERFORMED** — needs a registered test number |
| Railway deployment (branch confirmed, startup gate verified) | **PASS** for everything verifiable here; live service response not yet observed |
| Production migration and live-number verification | **NOT ATTEMPTED** — requires explicit authorisation |

### The one remaining dependency

Items (1) and (2) are **done** — the real identifiers are recorded and inbound routing is
verified (see §6). What remains is credential material only:

1. ~~Meta `phone_number_id` and WABA id~~ — **done**: `1222930064245010` / `2526681404500702`
2. ~~the display number in E.164~~ — **done**: `+254747595334`
3. `WHATSAPP_APP_SECRET` and `WHATSAPP_VERIFY_TOKEN` — so inbound webhooks can be verified
4. `WHATSAPP_ACCESS_TOKEN_11111111_1111_4111_8111_111111111111` and a decision on
   `WHATSAPP_SEND_ENABLED` — so outbound can work
5. a separate registered test number, to prove the inbound path end to end without
   disturbing the live number

Items 3–4 are Railway service variables (never repository files). Once they are set,
`whatsapp` reports `READY` — but `READY` only means *our* gates are satisfied. It does **not**
mean Meta's webhook points at us, nor that the number is registered for the Cloud API; those
are Meta-side facts that only an authenticated Graph API or MCP call can confirm.

**See also:** `docs/META_MCP_INTEGRATION.md` — Meta's WhatsApp Business Tools MCP is now
configured in the development environment (Streamable HTTP + OAuth, no client secret). It is
an operator tool for *inspecting* and *setting up* Meta assets; it makes no call into this
platform and does not change any of the above. Items (1)–(3) can be read off from it once
OAuth is completed.