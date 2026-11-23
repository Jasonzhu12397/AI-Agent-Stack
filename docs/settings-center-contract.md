# Settings & Billing implementation contract (frozen)

Source of truth for product scope: `docs/gui-parity-acceptance.md` → "Settings and account control center"
(14 navigation items, 9 acceptance tests, production backend rules). This document freezes the *technical*
interface so parallel work can proceed without re-negotiating shapes.

Nothing here authorizes inert controls: a page may only render a control whose API exists and is covered by a test.

## 0. Work isolation

Every worker owns a private `git worktree` + branch under
`/home/user/Doubao/chats/38454724073114370/`. Do not edit files outside your assigned worktree. Shared
writable files are limited to the one-line registration hooks listed in §5; the integrator resolves those.

Migration numbers are pre-allocated per worker (§5). Never renumber another worker's migration.

## 1. Backend HTTP conventions

Tenant-scoped resources:

```
/api/v1/tenants/<tenant_id>/settings/<section>/
/api/v1/tenants/<tenant_id>/projects/<project_id>/settings/<section>/
/api/v1/tenants/<tenant_id>/billing/...
/api/v1/tenants/<tenant_id>/data-controls/...
/api/v1/tenants/<tenant_id>/projects/<project_id>/archive-items/
/api/v1/tenants/<tenant_id>/connectors/...
/api/v1/tenants/<tenant_id>/feedback/
```

User-local resources:

```
/api/v1/users/me/
/api/v1/users/me/identities/
/api/v1/users/me/sessions/
/api/v1/users/me/preferences/<namespace>/
```

Non-tenant metadata:

```
/api/v1/meta/about/
/api/v1/meta/shortcuts/
```

Rules:

- Auth: session auth (`IsAuthenticated`). Tenant membership is mandatory on every tenant/project route:
  `get_object_or_404(Tenant.objects.filter(memberships__user=request.user), ...)` pattern already used by
  `core/token_budget_views.py`. Role checks via `core.permissions.IsTenantAdmin` / `core.membership_views
  .require_project_admin`. **403 for a member without the role, 404 for a tenant the user does not belong to.**
  Never express authorization as "the button is hidden".
- Success payloads are plain objects (lists wrapped as `{"results": [...]}` for pagination-free lists).
- Validation/business errors are exactly:

```json
{"error": {"code": "UPPER_SNAKE_CODE", "message": "human readable", "details": {"field": ["reason"]}}}
```

  HTTP status: 400 validation, 401 unauthenticated, 403 forbidden, 404 unknown scope, 409 state conflict,
  422 unsupported value/region, 429 rate/额度, 503 capability unavailable.

- The envelope is enforced by the shared `ErrorEnvelopeMixin` in
  `core/api_errors.py` (the `SettingsBaseView` family emits the same shape).
  DRF/Django exceptions are mapped as follows; `details` defaults to `{}` and
  field-level validation errors are placed there. The 404 message never
  discloses whether the target object exists, and unexpected exceptions stay
  500 without echoing internal details or tracebacks.

  | Exception | HTTP | `error.code` |
  |---|---|---|
  | `NotAuthenticated` | 401 | `AUTHENTICATION_REQUIRED` |
  | `AuthenticationFailed` | 401 | `AUTHENTICATION_FAILED` |
  | `PermissionDenied` | 403 | `PERMISSION_DENIED` |
  | `NotFound` / `Http404` | 404 | `NOT_FOUND` |
  | other `APIException` | its status code | `default_code` upper-cased |
  | unexpected exception | 500 | internal details are not echoed |
- Secrets never leave the server: credential fields are write-only and read back as
  `{"credential_ref": "env://...", "configured": true, "masked_hint": "…ABCD"}`.
- Security-sensitive mutations call `core.audit.record_audit_event(...)`; metadata must not contain secrets.
- Every mutation is idempotent per documented key and safe to replay.

## 2. Settings sections (durable, server-side)

`<section>` ∈ `general`, `notifications`, `work-mode`, `data-controls`, `privacy`.

- `GET` returns `{"section": "...", "scope": "tenant|project", "values": {...}, "effective": {...},
  "defaults": {...}, "updated_at": "..."}`; `PATCH` accepts `{"values": {...}}` partial merge and returns the
  same envelope. Unknown keys → 400 `SETTINGS_UNKNOWN_KEY`; wrong type/range → 400 `SETTINGS_INVALID_VALUE`
  with `details`.
- Storage is one row per (tenant[, project], section) holding a JSON `values` object; server-side defaults are
  code constants and are returned in `defaults` so the UI never invents one.
- Precedence (documented and tested): `project` value overrides `tenant` value; `user-local` overrides both
  for appearance/keyboard/notification-delivery only. `effective` is the folded result; `GET` must expose the
  winning layer per key.

User-local preferences (`/users/me/preferences/<namespace>/`), namespaces: `appearance`, `shortcuts`,
`notification-delivery`. Server copy is the durable record; browser `localStorage` may cache it, but a missing
or corrupt cache must fall back to the server value and then to `defaults` (deterministic, no silent blank UI).

## 3. Billing & purchase

- `Plan`: `code`, `name`, `description`, `interval` (`monthly|yearly`), `price_cents`, `currency` (`CNY`),
  `token_quota_daily`, `token_quota_monthly`, `features` (list), `region_scope` (`domestic`), `active`.
- `Subscription`: `tenant`, `plan`, `status` (`pending|active|past_due|cancelled|expired`), `started_at`,
  `current_period_start/end`, `cancel_at_period_end`, `renewal_mode` (`manual`; no card mandate exists).
- `Order`: `tenant`, `plan`, `status` (`created|pending|paid|failed|closed|refunded`), `amount_cents`,
  `currency`, `channel` (`wechat|alipay`), `provider_trade_no`, `paid_at`, `idempotency_key`.
  Transitions are only `created→pending→(paid|failed|closed)` and `paid→refunded`; repeated callbacks are
  no-ops keyed by `(channel, provider_trade_no, order_id)`; out-of-order callbacks never move state backwards.
- Successful provider responses are signature-verified (`PAYMENT_PROVIDER_RESPONSE_UNTRUSTED` on mismatch),
  and each order payload carries `refunded_cents` so the UI can show the refundable remainder.
- Closing an unpaid order is provider-first: the trade is queried, a settled trade is refused with
  **409 `ORDER_ALREADY_PAID`**, the trade is closed upstream, and a provider failure keeps the order
  `pending` with **502** and a retryable error code. A closed order therefore cannot still be paid.
- Each refund slice carries a server-generated, unique `refund_reference` (`refund-<order>-<random>`),
  replayed unchanged on retry, so a second partial refund is never mistaken for a provider replay.
- `EntitlementGrant`: append-only 额度发放记录 `tenant`, `subscription`, `order`, `period`, `tokens`,
  `expires_at`, `reason`. Buying a plan credits `TenantTokenBudget` **through a grant row**, never by writing
  the limit directly.
- Endpoints:

```
GET  .../billing/capabilities/        -> {"payment": {"available": bool, "channels": ["wechat","alipay"],
                                        "reason_code": "PAYMENT_PROVIDER_NOT_CONFIGURED"|null,
                                        "diagnostics": [{"channel","configured","missing":[slot],
                                        "required":[slot]}]}}   (slot names only, never a reference URI)
GET  .../billing/plans/               -> {"results":[Plan...]}      (each with purchasable: bool, reason_code)
GET  .../billing/subscription/        -> {"subscription": Subscription|null, "source": "authoritative"}
GET  .../billing/usage/               -> limits + used + period + history[]  (authoritative, from
                                        TenantTokenBudget / TenantTokenUsage / EntitlementGrant)
GET  .../billing/orders/  POST .../billing/orders/
POST .../billing/orders/<id>/close/   POST .../billing/orders/<id>/refund/
POST .../billing/payments/<channel>/callback/   (unauthenticated, signature-verified)
GET  .../billing/orders/<id>/         (replays the persisted payment instructions)
GET  .../billing/orders/<id>/status/  (read-only settlement poll: local status +
                                        provider_status/provider_error, never mutates)
```

- **No provider configured** (default in every environment): `GET capabilities` → `available:false`,
  `reason_code: "PAYMENT_PROVIDER_NOT_CONFIGURED"`; `POST orders` → **409/503** with
  `{"error":{"code":"PAYMENT_PROVIDER_NOT_CONFIGURED"}}`. Paid state can never be produced without a
  signature-verified callback from a configured provider. No endpoint fakes `paid`.
- **Region gating**: `ModelProvider.deployment_region` (`""` unclassified / `domestic` / `overseas`) decides
  purchasability. A plan/order whose models or plan scope include a non-`domestic` provider → **422**
  `{"error":{"code":"PURCHASE_REGION_NOT_SUPPORTED"}}` plus audit event. `""` fails closed. No vendor
  allow-list is hardcoded anywhere; the decision reads the field.
- Channels: only `wechat`, `alipay`. Any other channel value is rejected at serializer level
  (`PAYMENT_CHANNEL_NOT_SUPPORTED`) — it does not exist as an option server-side.

## 4. Payment adapters

`core/payments/` with `base.py` (`PaymentProvider` ABC: `create_payment`, `verify_callback`, `query_order`,
`refund`, `channel`), `wechat_v3.py`, `alipay.py`, `fake.py`, `registry.py`.

- Credentials only as `env://NAME` / `vault://mount/path#field` refs resolved through the platform credential
  resolver (`core.plugin_credentials.resolve_plaintext`), with `env` lookups confined to
  `PAYMENT_CREDENTIAL_ENV_PREFIXES` (default `WECHATPAY_,ALIPAY_`). Absence of a required ref keeps the
  provider out of the registry (→ capabilities unavailable). Onboarding sequence:
  `docs/payment-credentials.md`; self-check: `python manage.py check_payment_channels`.
- Readiness is declared per adapter (`SLOTS` / `REQUIRED_SLOTS`) and `is_configured()` derives from it, so
  diagnostics, the registry filter and the ops command cannot disagree.
- Signature verification is fail-closed: bad/missing signature → 400 `PAYMENT_SIGNATURE_INVALID` + audit.
- `fake.py` is a test double used by tests only, selected through explicit settings override, never by default.

## 5. File & migration ownership

| Worker | Owns (new files unless stated) | Migration numbers |
|---|---|---|
| settings-core | `core/settings_models.py`, `core/settings_views.py`, `core/settings_serializers.py`, `core/settings_urls.py`, `tests/test_settings_*.py` | 0036, 0037 |
| settings-data | `core/data_controls_models.py`, `core/data_controls_views.py`, `core/data_controls_urls.py`, `core/connector_settings_views.py`, tests | 0038, 0039 |
| billing | `core/billing_models.py`, `core/billing_views.py`, `core/billing_urls.py`, `core/payments/*`, tests | 0040–0042 |
| quota | `core/model_views.py`, `core/rag_runtime.py`, `core/token_budget.py`, `tests/test_token_budget_*.py` | 0043 if any |
| web-shell | `apps/web-studio/src/router/*`, `src/App.vue`, `src/main.ts`, `src/settings/layout/*`, `src/settings/shared/*`, `src/settings/routes.ts`, `src/settings/{account,general,help,about}/`, `src/i18n/index.ts`, `src/i18n/zh.ts`, `src/i18n/settings/index.ts`, `src/services/http.ts`, `apps/web-studio/tests/settings-*.test.mjs` | – |
| web-a | `src/settings/{plan,subscription,connectors,appearance,notifications}/`, `src/i18n/settings/{plan,subscription,connectors,appearance,notifications}.zh.ts`, its own tests | – |
| web-b | `src/settings/{work-mode,shortcuts,data-controls,archive,privacy}/`, matching i18n files, its own tests | – |

Registration hooks (each worker appends exactly its own lines, nothing else):

- `core/urls.py`: `path("", include("core.<module>_urls"))` appended at the end of `urlpatterns`.
- `core/models.py`: `from core import <module>  # noqa: F401,E402` appended at the very end so Django registers
  the models (`app_label` resolves to `core`).
- `platform_api/settings.py`: only if a worker needs an env-driven switch — use `os.environ.get` in code
  instead; do not add INSTALLED_APPS entries.

## 6. Frontend conventions

- Router: `/settings/:section` under `SettingsLayout.vue`, child routes from `src/settings/routes.ts`, lazy
  components, `meta: { requiresAuth: true, group, section }`. Route names `settings.<slug>`. Legacy views stay
  reachable (`/` keeps the current workspace shell) — the settings area must not break existing navigation.
- Slugs (fixed, in this order): `account`, `plan`, `subscription`, `general`, `connectors`, `appearance`,
  `notifications`, `work-mode`, `shortcuts`, `data-controls`, `archive`, `privacy`, `help`, `about`.
  Groups: `account` = {account, plan, subscription}; `general` = the rest in listed order.
- Each page lives in `src/settings/<slug>/index.vue` + `logic.ts` (pure functions: defaults folding,
  validation, state mapping, error-code → message key, list sorting) + optional `api.ts`. All network calls
  go through `src/services/http.ts` which throws `ApiError { status, code, message, details }`.
- Every page renders through `SettingsPageShell` and must handle `loading | empty | error | denied | ready`.
  A page with no data source for a control must show "capability unavailable" with a reason code, never a
  disabled control pretending to work and never a placeholder marketed as complete.
- i18n: English source strings in the SFC; `src/i18n/settings/<slug>.zh.ts` exports
  `Record<string, string>` (normalized English → Chinese). `src/i18n/settings/index.ts` merges them into the
  existing `zhCN` dictionary. Each page test asserts every `t()`-miss (no zh entry) count is zero for its keys.
- Styling: reuse `styles.css` tokens; responsive at 1280/900/640; visible focus states; `aria-current` on the
  active nav item; keyboard reachable nav.
- Tests are pure `node --test` over `.ts`/`.mjs` (no browser, no backend). Route test asserts: 14 routes,
  unique paths + names, group order, every lazy import resolves to an existing file, active state derived from
  `route.meta`. Page tests assert: defaults, validation failure codes, state mapping for 401/403/5xx,
  i18n completeness, destructive-action confirmation gates (logic-level).

## 7. Acceptance mapping (each row must have a named test)

| # | Requirement | Evidence |
|---|---|---|
| 1 | 14 items visible, correct route, active survives refresh | `settings-routes.test.mjs` + backend `test_settings_navigation_api` |
| 2 | direct route requires auth + server authorization | `test_settings_requires_auth`, `test_settings_forbidden_role`, router guard test |
| 3 | user/tenant/project isolation | `test_settings_cross_tenant_isolation`, `test_settings_cross_user_isolation`, `test_settings_precedence` |
| 4 | durable across restart | `test_settings_persist_across_reload` (fresh queryset / new client) |
| 5 | connectors via real API + audit | `test_connector_install_and_audit`, `test_connector_test_invocation_uses_gateway` |
| 6 | subscription/usage authoritative, unavailable without provider | `test_billing_capabilities_without_provider`, `test_billing_usage_reflects_token_budget` |
| 7 | export/archive/delete scope+confirm+idempotent+audit | `test_data_export_*`, `test_archive_restore_*`, `test_deletion_request_*` |
| 8 | appearance/notification/shortcut defaults+persistence+failure | `test_user_preferences_defaults`, page tests |
| 9 | loading/empty/error/denied everywhere | page tests + `test_settings_error_envelope_shape` |
| – | token 不烧超 | `test_token_budget_*` (single + concurrent) |
| – | 境外模型不可购买 / 无渠道 | `test_purchase_region_not_supported`, `test_purchase_channel_not_supported` |
