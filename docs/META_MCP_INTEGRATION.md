# Meta WhatsApp Business Tools MCP — development setup and integration boundary

**Status:** configured in the development environment; **unauthenticated** (OAuth not yet
completed). No production asset, webhook, template, phone number or environment variable was
touched. No message was sent.

**Server:** `https://mcp.facebook.com/whatsapp_business_tools`
**Transport:** Streamable HTTP (`"type": "http"`)
**Auth:** OAuth 2.0 + PKCE via Facebook Login for Business, browser flow, **no client secret**

---

## 1. What MCP is, and is not, in this repository

MCP is a **development/operator tool**. It is deliberately **not** part of the platform
runtime, and this is enforced by the repository's own tests in
`tests/test_platform_admin_units.py::WhatsAppIntegrationSurfaceTests`:

| Guarantee | Test |
|---|---|
| No repo-root `.mcp.json`; `requirements.txt` mentions no `mcp`/`facebook`/`meta` | `test_no_mcp_configuration_or_dependency` |
| No MCP code in `platform_admin/services/whatsapp.py` or `platform_admin/config.py` | `test_no_mcp_or_secret_manager_impersonation_code_remains` |
| `platform_admin/meta` package does not exist | `test_the_meta_package_is_gone` |
| Only the verified `/webhooks/whatsapp` route exists (no `/webhooks/meta`) | `test_the_dashboard_exposes_only_the_verified_webhook_route` |

That is why the configuration lives in a **workspace-scoped, gitignored** file
(`.vscode/mcp.json`, and `.vscode/` is in `.gitignore`) rather than in tracked project files.
VS Code merges workspace servers with the user-level servers in
`%APPDATA%\Code\User\mcp.json`, so the pre-existing `supabase` server is untouched.

**Consequence:** MCP can be removed or revoked with zero effect on the deployed platform.
Nothing in `platform_admin/`, `saas/` or `db/` imports it.

## 2. Verified authentication facts (probed from the live endpoint)

These were read directly from Meta's own discovery documents — not taken from blog posts:

```text
POST https://mcp.facebook.com/whatsapp_business_tools
  -> HTTP 401 Unauthorized
  WWW-Authenticate: Bearer
      resource_metadata="https://mcp.facebook.com/.well-known/oauth-protected-resource/whatsapp_business_tools",
      scope="business_management whatsapp_business_management whatsapp_business_messaging"

GET  https://mcp.facebook.com/.well-known/oauth-protected-resource/whatsapp_business_tools
  -> HTTP 200 {"resource":"https://mcp.facebook.com/whatsapp_business_tools",
               "authorization_servers":["https://www.facebook.com/whatsapp_business_tools"],
               "scopes_supported":["business_management","whatsapp_business_management",
                                   "whatsapp_business_messaging"],
               "bearer_methods_supported":["header"]}

GET  https://www.facebook.com/.well-known/oauth-authorization-server/whatsapp_business_tools
  -> HTTP 200 {"issuer":"https://www.facebook.com/whatsapp_business_tools",
               "authorization_endpoint":"https://www.facebook.com/v26.0/dialog/oauth",
               "token_endpoint":"https://graph.facebook.com/v26.0/oauth/access_token",
               "response_types_supported":["code"],
               "grant_types_supported":["authorization_code","refresh_token"],
               "code_challenge_methods_supported":["S256"],
               "token_endpoint_auth_methods_supported":["none"],
               "registration_endpoint":"https://mcp.facebook.com/.well-known/register/whatsapp_business_tools"}
```

What this proves, and why it matters for secret handling:

* `code_challenge_methods_supported: ["S256"]` — **PKCE is required** (authorization code flow).
* `token_endpoint_auth_methods_supported: ["none"]` — Meta treats this as a **public client**.
  There is **no client secret**: nothing for anyone to paste into chat or commit.
* `registration_endpoint` is published — **dynamic client registration is supported**, so the
  MCP client registers itself. No manual app registration is needed for the *OAuth client*.
* The token is obtained by the MCP client and stored in the OS credential store. It never
  appears in `mcp.json`, in this repository, or in this document.

> **Note on "Meta app".** OAuth client registration (above) is separate from Meta's
> requirement that the *user* administer a Meta app with the WhatsApp use case connected to
> the business. The latter is a business prerequisite (see §5), not a credential.

## 3. Server availability

The endpoint is reachable and its discovery documents are published (HTTP 200). Meta states
the server is in **beta and gradual rollout**, and that the interface and tool set may change.
It may therefore not yet be enabled for a given Meta account — that is rollout, not
misconfiguration. This was reported consistently by third-party coverage of the September 2026
announcement; it is recorded here as *reported*, not *verified by us*.
## 4. Tool capabilities — verified vs unverified

**The tool list has NOT been enumerated.** `tools/list` returns `401` without OAuth, so no
tool name or signature has been confirmed first-hand. Do not treat any name below as verified.

| Capability area | Status | Basis |
|---|---|---|
| Endpoint reachable; Streamable HTTP; OAuth + PKCE + DCR; three scopes | **VERIFIED** | live probes in §2 |
| Requires OAuth before any tool call | **VERIFIED** | `401` on `initialize` and `tools/list` |
| Discovering WABAs / businesses | **REPORTED, unverified** | third-party coverage of Meta's docs |
| Listing phone numbers and registration status | **REPORTED, unverified** | third-party coverage |
| Registering / verifying a phone number (one-time code) | **REPORTED, unverified** | third-party coverage |
| Creating and managing message templates; approval status | **REPORTED, unverified** | third-party coverage |
| Configuring / inspecting webhook subscriptions | **REPORTED, unverified** | third-party coverage |
| Sending a test message (one recipient per call) | **REPORTED, unverified** | third-party coverage |
| Exact tool names | **UNKNOWN** | needs an authenticated `tools/list` |
| Reading *incoming* messages / delivery receipts | **REPORTED absent** | MCP sets the webhook address but does not receive events |
| Analytics / reporting tools | **REPORTED absent** | third-party coverage |
| Scheduling / delayed send | **REPORTED absent** | third-party coverage |

Unverified claims will be replaced with the real list the moment OAuth completes; the first
authenticated action should be `tools/list`, which is read-only.

### Stated prerequisites (third-party, to be confirmed on connect)

1. `MANAGE` (admin) role on the business.
2. Your **own** Meta app with the WhatsApp use case, connected to that business, on which you
   are also an admin. Being admin of the business alone is not sufficient. This is the usual
   blocker when a WABA was onboarded through a BSP — the app belongs to the provider.
3. The WhatsApp Business Cloud API Terms of Service accepted for that business. **Until an
   admin accepts, sending messages and registering numbers are blocked.**
## 5. Integration boundary with our architecture

MCP sits **outside** the platform. It is a configuration channel used by an operator in an
agent session; it is not a runtime data path, and it must not become one.

```text
   agent session (developer machine, browser OAuth)
        |  Meta MCP  -- scopes: business_management / whatsapp_business_management /
        |                        whatsapp_business_messaging
        v
   Meta business assets (WABA, phone number, templates, webhook subscription)
        |                                    ^
        |  identifiers copied by a human      |  NO programmatic push into the platform
        v                                    |
   app.platform_create_channel / platform_update_channel   <-- the only write path
        ^                                    (SECURITY DEFINER, require_platform_admin())
        |
   platform_admin/services/channels.py   (validates non-secret identifiers only)
        |
   public.tenant_channels  { phone_number_id, waba_id, display_phone_number,
                             credentials_ref = 'tenants/<tenant-uuid>/whatsapp' }
        |
   platform_admin/services/whatsapp.py   (inbound routing + GATED outbound)
```

Rules that follow from the existing design, which any future integration must respect:

1. **No direct Meta calls from our services.** The platform never provisions Meta assets:
   `channels.py` says so explicitly ("The platform never calls Meta"). Meta-side creation is
   MCP/console work.
2. **Only non-secret identifiers cross the boundary.** `tenant_channels` stores
   `phone_number_id`, `waba_id` and `display_phone_number`. The access token is resolved at
   send time from `WHATSAPP_ACCESS_TOKEN_<TENANT_ID>` and is never stored or logged.
3. **Tenant identity is derived server-side.** Nothing MCP returns is ever inserted as a raw
   `tenant_id`. Writes go through `app.platform_*`, which re-checks
   `app.require_platform_admin()` and enforces the `credentials_ref` scope check
   (`tenants/<this tenant>/whatsapp`), so a WABA or number belonging to one business cannot be
   attached to another.
4. **Outbound stays gated.** `WHATSAPP_SEND_ENABLED` plus a per-tenant token is required
   regardless of MCP. Connecting MCP does not enable sending in our application.
5. **Our webhook remains ours.** `/webhooks/whatsapp` with signature verification and
   provider-message-id deduplication is the inbound path. MCP can *set* a webhook address on
   Meta's side, which is precisely the dangerous operation: pointing the production number at
   a development endpoint would silently break live message handling.
6. **MCP must not be imported by `platform_admin/`, `saas/` or `db/`.** If a future feature
   needs Meta data at runtime it belongs behind a provider interface — the same injection
   style already used for `Authenticator` / `SaasBotProvider` in `create_app`. The MCP client
   stays in tooling, not in the request path.
## 6. Security and tenant-isolation considerations

* **No secret ever enters the repository or this conversation.** OAuth is a browser flow; the
  grant is stored by the MCP client in the OS credential store. `mcp.json` carries no headers,
  no token and no client secret — and §2 proves Meta issues none for this client type.
* **Least scope.** Only three scopes are requested, all business-management oriented. The
  consent screen lets the user choose which businesses and apps the session may act on, so
  access can be narrowed to the Cindy Bakes business before anything else is exposed.
* **Revocable.** Access can be withdrawn at facebook.com → Settings → Business Integrations
  without touching our platform.
* **Blast radius is Meta-side, not platform-side.** A compromised MCP session can affect the
  business's Meta assets; it cannot reach tenant data, because it has no route into
  PostgreSQL and holds no platform credential.
* **Tenant isolation is unaffected.** MCP cannot create tenants, set `tenant_id`, or bypass
  RLS. Provisioning and channel binding remain `SECURITY DEFINER` database functions with
  `require_platform_admin()`, and RLS is forced on every tenant-owned table.
* **The real risk to manage is the webhook address and the live number**, not data leakage:
  both are production assets and are out of scope for any write without explicit approval.

## 7. Recommended next step for onboarding Cindy Bakes

**Phase A — read-only (safe once OAuth is done):**

1. Complete OAuth in VS Code (§8).
2. Call `tools/list` and replace §4 with the verified tool set.
3. List businesses/WABAs and confirm which account owns the Cindy Bakes assets — this decides
   the §4 prerequisite 2 question (whether the WABA sits on an app we administer).
4. Inspect the phone number and its registration status, the templates, and the webhook
   subscription.
5. Confirm against what this platform already holds for tenant `cindy-bakes`, which now
   records the real identifiers supplied by the client:
   `phone_number_id 1222930064245010`, `waba_id 2526681404500702`, display
   `+254747595334`, Business Portfolio ID `2803809630006435` (reference only — our schema
   has no column for it). The useful checks are the ones our database cannot make itself:
   whether the number is actually **registered** for the Cloud API, its quality/limit tier,
   the existing **webhook subscription target**, and the template list with approval states.
6. Re-run the existing preflight, which reports the eight go-live prerequisites without
   calling the provider:

   ```bash
   python db/tenant_onboarding.py whatsapp --tenant-slug cindy-bakes
   ```

**Phase B — writes (each needs explicit approval):**

1. Create a *separate* registered test number and point it at the staging webhook — never at
   production.
2. Record the real identifiers with
   `python db/tenant_onboarding.py channel --tenant-slug cindy-bakes …`, which performs the
   database-mandated disable → update → activate sequence.
3. Only after the inbound path is proven on the test number, decide about production.

**Explicitly out of scope without approval:** changing the production webhook address,
registering or deregistering the live number, submitting templates to Meta, enabling outbound
sending, and sending any message to a real customer.

**Where MCP fits, honestly:** it is well suited to *inspecting* and *setting up* Meta assets
and poorly suited to *running* them — it does not receive webhook events, has no analytics,
and sends one recipient per call. Our platform already owns inbound processing, conversation
history and order/payment state, so the durable integration stays where it is: in the
application, behind the existing provider seam.

## 8. Runbook: completing OAuth (user action required)

Interactive browser authorisation cannot be performed on your behalf.

1. Open this workspace in VS Code (the configuration is workspace-scoped).
2. Command Palette → **MCP: List Servers** (or the MCP view in the Chat sidebar); confirm
   `whatsapp-business-tools` appears as an HTTP server.
3. Select it → **Start** / **Sign in**. VS Code reads the `401` `WWW-Authenticate` header,
   registers a client through dynamic client registration, opens the Facebook consent screen
   and completes PKCE.
4. On the consent screen choose **only the Cindy Bakes business and its app**, and grant the
   three requested scopes.
5. Tell me whether the server shows as connected. I will then run the read-only enumeration
   and replace §4 with the verified tool list.

If the server does not appear or refuses to authorise, that is most likely Meta's gradual
rollout (§3) or a missing prerequisite in §4 — the server reports which.

To revoke at any time: facebook.com → Settings → Business Integrations.

## 9. Re-verifying the authentication facts without any credential

The §2 evidence is reproducible from a clean checkout. This sends no credential and no
write, and is what was used to produce this document:

```bash
curl -i -X POST https://mcp.facebook.com/whatsapp_business_tools \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}'

curl -s https://mcp.facebook.com/.well-known/oauth-protected-resource/whatsapp_business_tools
curl -s https://www.facebook.com/.well-known/oauth-authorization-server/whatsapp_business_tools
```

Expected: `401` with a `WWW-Authenticate` header naming the resource metadata and the three
scopes, then the two discovery documents shown in §2.

To confirm the workspace configuration is still secret-free and parses:

```bash
python -c "import json,pathlib; d=json.loads(pathlib.Path('.vscode/mcp.json').read_text()); print(d)"
python -m pytest tests/test_platform_admin_units.py -q   # guards against credentials
```