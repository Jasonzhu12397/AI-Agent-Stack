# Settings and account control center — delivery record

Branch: `feature/settings-and-billing`. Baseline: `docs/gui-parity-acceptance.md` §"Settings and account control center".
Contract: `docs/settings-center-contract.md`. This file records what actually shipped, what proves it, and what is deliberately out of scope.

## 1. Navigation inventory → code

All 14 items in the acceptance document are routed Vue pages backed by real Django endpoints. Slug order is the navigation order.

| # | Nav item | Slug | Vue page | Primary endpoints |
|---|---|---|---|---|
| 1 | Account Management | `account` | `apps/web-studio/src/settings/account/` | `GET/PATCH /api/v1/users/me/`, `GET /api/v1/users/me/identities/`, `GET /api/v1/users/me/sessions/`, `POST .../sessions/<key>/revoke/` |
| 2 | Standard Plan | `plan` | `src/settings/plan/` | `GET /api/v1/tenants/<t>/billing/plans/`, `GET .../billing/capabilities/` |
| 3 | Subscription & Usage Management | `subscription` | `src/settings/subscription/` | `GET .../billing/subscription/`, `GET .../billing/usage/`, `GET/POST .../billing/orders/`, `GET .../orders/<id>/status/`, `POST .../orders/<id>/close/`, `POST .../orders/<id>/refund/`, `GET .../billing/reconciliation/` |
| 4 | General | `general` | `src/settings/general/` | `GET/PATCH .../settings/general/`, `GET/PATCH .../projects/<p>/settings/general/` |
| 5 | Plugins & Connectors | `connectors` | `src/settings/connectors/` | `GET .../connectors/`, `POST .../connectors/<slug>/install/`, `PATCH .../installations/<id>/`, `POST .../health/`, `POST .../test/` |
| 6 | Appearance | `appearance` | `src/settings/appearance/` | `GET/PUT/PATCH /api/v1/users/me/preferences/appearance/` |
| 7 | Notification Settings | `notifications` | `src/settings/notifications/` | `GET/PATCH .../settings/notifications/` |
| 8 | Work Mode Settings | `work-mode` | `src/settings/work-mode/` | `GET/PATCH .../settings/work-mode/` |
| 9 | Keyboard Shortcuts | `shortcuts` | `src/settings/shortcuts/` | `GET /api/v1/meta/shortcuts/`, `GET/PATCH /api/v1/users/me/preferences/shortcuts/` |
| 10 | Data Controls | `data-controls` | `src/settings/data-controls/` | `GET/PATCH .../settings/data-controls/`, `GET/POST .../data-controls/exports/`, `POST .../exports/<id>/expire/` |
| 11 | Archived Content | `archive` | `src/settings/archive/` | `GET .../projects/<p>/archive-items/`, `POST .../archive-items/<type>/<id>/restore/`, `DELETE .../archive-items/<type>/<id>/` |
| 12 | Privacy & Permissions | `privacy` | `src/settings/privacy/` | `GET .../privacy/grants/`, `DELETE .../privacy/grants/<id>/`, `GET .../privacy/sessions/`, `POST .../privacy/deletion-requests/` |
| 13 | Help & Feedback | `help` | `src/settings/help/` | `GET/POST .../feedback/` |
| 14 | About | `about` | `src/settings/about/` | `GET /api/v1/meta/about/` |

Shared shell: `src/router/{index.ts,guard.ts}`, `src/settings/routes.ts`, `src/settings/layout/{SettingsLayout.vue,SettingsNav.vue}` — persistent left navigation, Account group (3 items) then General group (11 items), `aria-current` selected state, responsive at 1280 / 900 / 640.

## 2. What each page guarantees

- Four states on every page: loading, empty, error, denied. `load_state_maps_auth_failure_to_denied` style assertions live in the per-page logic tests; `503` is never rendered as an empty state.
- Bilingual: `src/i18n/settings/<slug>.zh.ts` merged into the app dictionary; `settings-labels.test.mjs` fails on a missing or conflicting translation, so no page can ship an untranslated label.
- Server-side RBAC and tenant isolation: tenant/project settings PATCH requires admin, non-members get 404, cross-tenant reads are 404, user-local settings are self-only. Auditing: every write records an audit event with `section` / `changed_keys` / `fields` and never stores setting values or secrets in metadata.
- Secrets are references only: `env://` or `vault://`; plaintext is rejected with `CONNECTOR_SECRET_NOT_REFERENCE`; reads return only `configured` + `masked_hint`.

## 3. Hard requirement A — 14 pages

- Backend page matrix `tests/test_settings_pages_acceptance.py`: 44 cases = 14 pages × 3 tiers (anonymous 401 + envelope, non-member 404 + envelope, member 2xx + primary fields) + 2 integrity cases (`test_slug_set_matches_contract_doc`, `test_matrix_has_one_case_per_slug`). A missing page cannot pass.
- Frontend per-page logic tests: `settings-account-logic`, `settings-plan`, `settings-subscription`, `settings-general-logic`, `settings-connectors`, `settings-appearance`, `settings-notifications`, `settings-work-mode`, `settings-shortcuts`, `settings-data-controls`, `settings-archive`, `settings-privacy`, `settings-help`, `settings-about` — 14 files, one per page, plus `settings-guard`, `settings-routes`, `settings-labels` for the shell. `settings-routes.test.mjs` also asserts every lazy component target exists on disk.
- Error envelope contract: `core/api_errors.py` `ErrorEnvelopeMixin` maps `NotAuthenticated`→401 `AUTHENTICATION_REQUIRED`, `AuthenticationFailed`→401 `AUTHENTICATION_FAILED`, `PermissionDenied`→403 `PERMISSION_DENIED`, `NotFound`→404 `NOT_FOUND` (no existence leak), other `APIException`→own status with `default_code` uppercased, unexpected→500 without internals. Asserted by `tests/test_api_error_envelope.py` across billing / connectors / data-controls / archive / privacy, including "no top-level `detail` key".

## 4. Hard requirement B — token budget cannot be overspent

`ModelGateway` is the single mandatory front door: `_require_guard()` rejects a missing or unauthorized guard before any network call with `TOKEN_GUARD_REQUIRED`, so a hand-constructed gateway that skips reservation fails instead of silently burning tokens. Call sites wired: `ModelTestView` (AdHocTokenGuard, exhaustion → 429 `TENANT_TOKEN_BUDGET_EXCEEDED` with `details.period` plus a failure audit), `rag_runtime` ingest/retrieve, worker `execute_agent_run`, and both `workflow_engine` nodes (RunTokenGuard).

- `tests/test_token_guard_enforcement.py`: `test_gateway_requires_guard`, `test_bypassing_reservation_fails_before_network`, `test_model_test_view_reserves_and_429_on_exhaustion`, `test_embedding_reserves_tokens`, `test_no_unmetered_gateway_construction_in_source` (source scan: every `ModelGateway(` must carry `guard=`).
- `tests/test_token_budget_concurrency.py`: two threads released together by `threading.Barrier(2)`, reservation under `select_for_update` — `test_concurrent_reservation_allocates_remaining_once`, `test_concurrent_reservation_no_double_spend`, `test_settle_beyond_reservation_rejected`, `test_release_restores_capacity`. A Postgres row-lock variant exists and is skipped locally (no Postgres on this machine); it runs in CI.
- Usage is read from the settled ledger, not estimated: `GET .../billing/usage/` returns `period_start` / `period_end` plus `history` rows `{period, source, consumed_tokens}` aggregated from `SETTLED` usage rows; released/unsettled rows never count. Proven by `tests/test_billing_usage_payload.py` (including `sum(consumed) == monthly_used` for the same period, and `history == []` on an empty ledger).

## 5. Hard requirement C — WeChat Pay + Alipay only, region gate, no fake payment

- Channels: only `wechat` and `alipay`; anything else → 400 `PAYMENT_CHANNEL_NOT_SUPPORTED`.
- Region gate is server-side, read from `ModelProvider.deployment_region` (no hard-coded vendor list): empty/unset fails closed, `overseas` → 422 `PURCHASE_REGION_NOT_SUPPORTED` plus an audit event. Asserted by `test_purchase_region_not_supported_and_audited` and `test_purchase_region_unclassified_fails_closed`.
- No configured provider: `GET .../billing/capabilities/` → `available:false, reason_code:"PAYMENT_PROVIDER_NOT_CONFIGURED"`, `POST .../billing/orders/` → 409 with the same code. `paid` is never synthesized on any path.
- Callbacks: `AllowAny` but fail-closed signature verification, failure → 400 `PAYMENT_SIGNATURE_INVALID` + audit; duplicate and out-of-order callbacks are idempotent and never regress state (`test_duplicate_callback_applied_once`, `test_out_of_order_callback_does_not_regress`). Fake provider registers only under explicit `BILLING_FAKE_PROVIDER_ENABLED`.
- Entitlements: `EntitlementGrant` is append-only; `TenantTokenBudget` is updated only through grant rows, once per order (`test_paid_order_grants_entitlement_once`, `test_grant_updates_tenant_budget_via_ledger`).

## 6. Gate results on `f2ddbaa4`

| Gate | Result |
|---|---|
| Backend full suite (`DATABASE_URL=sqlite:///:memory:`) | 1974 passed, 16 skipped, 0 failed |
| Backend page matrix | 44 passed |
| Error envelope | 11 passed |
| Usage payload | 5 passed |
| `ruff check .` (repo root, tests included) | All checks passed |
| `manage.py makemigrations --check --dry-run` | No changes detected (chain 0034 → 0036 → 0037 → 0038 → 0039 → 0040 → 0041 → 0042 → 0043) |
| Frontend `npm test` | 133 passed, 0 failed |
| `vue-tsc --noEmit` | 0 errors |
| `npm run build` | success |

Migration ownership: `0036`/`0037` settings + account/feedback, `0038` data export & deletion, `0039` archive action, `0040` provider deployment region, `0041`/`0042` billing plan/subscription/order/entitlement, `0043` token usage scope.

## 7. Known limits (deliberate, not silently faked)

- WeChat Pay v3 and Alipay adapters are protocol skeletons: order JSON, RSA2/AES-GCM verification and decryption, query/refund entry points are implemented, but no real outbound HTTP transport and no merchant secrets are wired (`vault://` resolves as "not configured" in-process).
- Session timestamps are real, not derived: `core/session_activity.py` keeps a `SessionActivity` row per session key (created_at, last_activity, revoked_at; no IP or user agent), populated on `user_logged_in`, refreshed by `SessionActivityMiddleware` with one conditional UPDATE per 60 s, and closed on logout/revoke. Account and privacy session lists report `created_at` / `last_activity` with `*_source = session_metadata`; a session that predates the table still reports the derived creation time and an explicit `unavailable` last activity instead of a fabricated value.
- `GET .../privacy/sessions/` is live (list + revoke); the privacy page's primary `grants/` endpoint remains live as well.
- `identities/` reports only local password method (`has_usable_password`); no external identity provider bindings exist in this codebase, so none are invented.
- Postgres-specific concurrency and integration paths are skipped locally (no Postgres/Redis/docker on this machine) and are exercised only in CI.
- Error-envelope unification now also covers tenant/project membership management (`TENANT_ADMIN_REQUIRED`, `PROJECT_ADMIN_REQUIRED`, `MEMBERSHIP_INPUT_INVALID`, `MEMBERSHIP_ROLE_INVALID`, `OWNER_ROLE_REQUIRED`, `MEMBERSHIP_EXISTS`, `PROJECT_MEMBER_NOT_IN_TENANT`, `LAST_OWNER_PROTECTED`, `LAST_PROJECT_ADMIN_PROTECTED`); no membership endpoint emits DRF `detail`. The token-budget views never used `detail` either.
- The subscription page also renders the read-only `/billing/reconciliation/` window (orders, refund slices, unavailable channels, discrepancies), so a tenant admin can see provider drift without leaving the console.
