# WhatsApp AI configuration: loading an existing client and saving one change at a time

This document describes how the dashboard's **Rules / AI** tab (`/tenants/<id>/rules`) reads a
client's saved configuration and how it writes a change, and the partial-update contract that
makes "change the greeting only" safe.

## 1. How configuration is stored

One row per tenant in `public.tenant_settings`, guarded by row level security through
`app.current_tenant_id()`. Six columns carry the configuration:

| Column | Type | Contents |
| --- | --- | --- |
| `timezone` | text | IANA timezone (the dashboard form uses `Africa/Nairobi`) |
| `currency` | text | ISO 4217 code |
| `configuration` | jsonb | `identity`, `config` (WhatsApp API), `knowledge_base` (RAG) |
| `bot_persona` | jsonb | `tone`, `bot_name`, `max_chars`, `max_lines`, `emoji` |
| `business_hours` | jsonb | `greeting`, `timezone`, `human_hours` (per day), `human_offline_message`, `bot_hours` |
| `business_rules` | jsonb | description, pricing/discount/stock rules, payment methods, delivery, return policy, sellable categories, `order_flow_derived`, `escalation`, `spam_rule`, WhatsApp logic |

`catalogue_version` and `updated_at` are bookkeeping; the `tenant_settings_set_updated_at`
trigger bumps `updated_at` on every write.

There is exactly one configuration format. The catalogue is **not** part of it: products and
variants live in `public.products` / `public.product_variants` and are managed from the
Catalogue tab, so a configuration save can never rewrite inventory.

WhatsApp access tokens and the app secret are **not** stored here. A channel holds a
`credentials_ref` that resolves to a server environment variable
(`WHATSAPP_ACCESS_TOKEN_<TENANT_ID>`), so no credential belongs in these documents.

## 2. How it is loaded

```
GET /tenants/<id>/rules
        |
        +-- tenant_service.get_tenant(...)         platform API: verifies the administrator
        |                                          may manage this tenant (404 otherwise)
        |
        +-- bot_config.load_configuration(...)     TenantAdmin.get_settings(): one RLS-scoped
        |                                          row, read as app_backend for this tenant
        |
        +-- bot_config.field_values(stored)        flat field map the section forms render
        |
        +-- tenant_service.mask_documents(stored)  the raw JSON editors, credential keys masked
        |
        +-- tenant_service.bot_config_preview()    the read-only "Bot JSON output" block
```

* `GET /tenants/<id>/rules/config` returns the same data as JSON (fields, section registry,
  version, redacted documents), so the state the form shows is inspectable and testable.
* **Nothing is defaulted.** A setting the tenant never saved is `None`/`[]`, and the form
  renders it as *Not set* with the conventional value only as a placeholder. An untouched
  field can therefore never look saved.

## 3. How the runtime reads it

`saas/bot.py::TenantBotContextLoader.load()` reads the `tenant_settings` row **afresh for
every inbound message** (`platform_admin/routes/__init__.py`, the WhatsApp webhook, and the
Bot Playground). There is no cache and no second copy of the configuration, so a saved change
takes effect on the bot's next reply with nothing to invalidate.
`tests/test_tenant_config_partial_updates.py` asserts exactly that.

## 4. The partial-update contract

```
POST  /tenants/<id>/rules/<section>     (the section forms)
PATCH /tenants/<id>/rules/<section>     (JSON; CSRF token in X-CSRF-Token)
```

A section builder in `platform_admin/services/bot_config.py` returns a **patch** containing
only the keys the request actually carried:

* **absent** -> never written, so it cannot be reset to a default, `""` or `null`;
* **present and empty** -> removed from the stored document (an explicit clear; "unset" is a
  real state, which is what lets one field be changed on a client that was never fully
  configured);
* **present and non-empty** -> validated against the same whitelist and choice sets the
  onboarding form uses; an unknown choice or a malformed time, number, phone number or URL is
  rejected and nothing is written.

`config_merge.deep_merge()` merges the patch into the stored document key by key, so
`business_rules.escalation.whatsapp` can change while `after_tries` and `transfer_to` survive.
Two small list operations cover single-item edits that a recursive merge cannot express:
`ItemUpdate` (edit or remove one FAQ by index) and `ItemRemoval` (remove one document by name).
`ta.update_settings(**updates)` writes only the columns the patch named, so an unrelated
document cannot be touched even by accident.

Concurrency: each form carries `config_version` (the `updated_at` it was rendered from). A save
whose version no longer matches is refused with `409` and the stored configuration is untouched.

## 5. Sections

| Section | Endpoint suffix | Writes |
| --- | --- | --- |
| `identity` | `/rules/identity` | `configuration.identity` |
| `persona` | `/rules/persona` | `bot_persona` (the tone also sets `max_chars`/`max_lines`/`emoji`) |
| `hours` | `/rules/hours` | `business_hours` (greeting, per-day human hours, offline message) |
| `rules` | `/rules/rules` | `business_rules` (description, pricing, stock, payment, delivery, escalation) |
| `logic` | `/rules/logic` | `business_rules` (photo / not found / price asked / visibility / spam) |
| `api` | `/rules/api` | `configuration.config` |
| `knowledge` | `/rules/knowledge` | `configuration.knowledge_base` (website, scrape options, FAQ list, documents, extra info) |
| `faq` | `/rules/faq` | one FAQ, by `faq_index` |
| `document` | `/rules/document` | one document, by `document_name` |

A submit button named `focus` narrows a save to one branch of a section, which is how the
field-level buttons work: `focus=greeting`, `focus=out_of_stock`, `focus=escalation.whatsapp`,
`focus=followup_template_name`, `focus=website_link`, `focus=human_hours.saturday`.

`order_flow_derived` is never asked for: it is recomputed only when `payment_methods` or
`delivery` is part of the patch.


## 6. New-client onboarding

The original single-form workflow is unchanged and still reachable: the **Save every section
at once** button submits all sections as one `form_kind=whatsapp_onboarding` POST to
`/tenants/<id>/rules`, which keeps the original mandatory fields (display name, business type,
tone, greeting, business description) and the original strict validation. A new client can also
be configured section by section instead.

## 7. Credential handling

* The raw JSON editors render stored documents with credential-looking values replaced by
  `[redacted]` (`tenants.mask_documents`, reusing the audit redactor). `update_rules` restores
  the stored value for any key still carrying that mask, so masking cannot destroy a credential.
* The read-only preview and the read API are redacted with the same helper the bot uses before
  configuration reaches a prompt.
* A section save only ever writes its own whitelisted, non-secret fields, so a key the section
  does not manage (for example a token an operator pasted into `configuration.config`) survives
  untouched.

## 8. UI

`platform_admin/templates/tenant_rules.html` keeps every existing setting and adds:

* one `<form>` per section, each with its own **Save ...** button and its own error area;
* field-level buttons (`Save greeting only`, `Save Monday`, `Save escalation number only`,
  `Save template name only`, `Save website URL only`, `Save out-of-stock rule only`);
* a per-FAQ editor with **Save this FAQ** / **Remove this FAQ**, and a per-document **Remove**;
* a *Saved* / *Unsaved changes* indicator per section (`platform_admin/static/tenant_rules.js`,
  a progressive enhancement: the page works without JavaScript);
* validation errors beside the section that failed, with the rejected values still on screen;
* fields the client has never configured marked *Not set*.

## 9. Tests

```
python -m unittest discover -s tests -p "test_tenant_config_partial_updates.py"
```

Covers: loading saved values into the form and the read API; unset settings shown as *Not set*
with no fabricated defaults; single-field and single-day saves; nested-field saves preserving
siblings; omitted vs empty values; multi-value groups; invalid values rejected with the stored
configuration unchanged; a failed audit write rolling the change back; FAQ and document
single-item edits; website/extra-info changes preserving documents and FAQs; catalogue
untouched; credentials never rendered and never destroyed; tenant isolation and CSRF; stale
version rejected; the bot runtime seeing the change; the original onboarding path still working.

## 10. Deployment

No database migration is required: the change is service, route, template and static-asset only.
The new static file `platform_admin/static/tenant_rules.js` is served from the app, which the
existing `Content-Security-Policy: script-src 'self'` already permits.

