# Payment Channel Credentials

Billing supports exactly two channels: WeChat Pay v3 (`wechat`) and Alipay
(`alipay`). Every merchant secret is referenced by URI and resolved at call
time; the platform never stores a secret value, never returns one in a response
and never writes one to audit metadata, a receipt or a log line.

This runbook is what an operator follows to connect a real merchant account. It
describes the code as merged, not a plan: the adapters perform real v3 RSA
signing, certificate-serial trust checks, AES-256-GCM resource decryption and
Alipay RSA2 signing, but no deployment in this repository has a merchant
account attached yet, so field names and status enumerations still need one
pass against the live provider during onboarding.

## Reference URI

A payment credential reference uses the same syntax and the same resolver as
plugin connectors, so there is one reference format on the platform:

```
env://WECHATPAY_APPID
env://WECHATPAY_PRIVATE_KEY
vault://merchant/wechatpay#private_key
```

- `env://NAME` reads one process environment variable.
- `vault://mount/path#field` reads `<mount>/data/<path>` from the deployment
  secret store (KV v2) and takes `field` from the returned secret object. The
  default field, when the fragment is omitted, is `value`. A secret field may
  hold either a plain string (used as the secret) or a credential contract
  object (`{"type": "bearer", "token": ...}`, `{"type": "header", ...}`,
  `{"type": "query", ...}`), whose value is flattened to the secret string.
- The reference is validated before dispatch: non-empty, no whitespace, at most
  2048 characters, a valid scheme, and — for `vault` — a mount and path built
  from `[A-Za-z0-9._-]` without `.`/`..` segments.

## Who may read the environment

`settings.PAYMENT_CREDENTIAL_ENV_PREFIXES` (environment variable
`PAYMENT_CREDENTIAL_ENV_PREFIXES`, comma-separated) confines payment `env://`
lookups to channel prefixes. The default is `WECHATPAY_,ALIPAY_`, so
`env://SECRET_KEY` or a plugin-owned `env://PLUGIN_SECRET_*` variable can never
become a merchant key. A deployment that names its variables differently
overrides the list rather than relaxing the check.

## WeChat Pay v3 slots

| Slot | Default reference | Required |
| --- | --- | --- |
| `appid` | `env://WECHATPAY_APPID` | yes |
| `mchid` | `env://WECHATPAY_MCHID` | yes |
| `api_v3_key` | `env://WECHATPAY_API_V3_KEY` | yes |
| `serial_no` | `env://WECHATPAY_CERT_SERIAL_NO` | yes |
| `private_key` | `env://WECHATPAY_PRIVATE_KEY` | yes |
| `platform_cert` | `env://WECHATPAY_PLATFORM_CERT` | yes |
| `notify_url` | `env://WECHATPAY_NOTIFY_URL` | no |
| `base_url` | `env://WECHATPAY_BASE_URL` | no |

`private_key` is the merchant PKCS#8 PEM private key used for
`WECHATPAY2-SHA256-RSA2048` request signing; `platform_cert` is the WeChat Pay
platform certificate whose serial must match the certificate serial presented in
the `Wechatpay-Serial` response header. `api_v3_key` decrypts the
AES-256-GCM `resource` block of callbacks and refund notifications.

Optional slots fall back, not fail: a missing `notify_url` or `base_url` keeps
the channel usable (`base_url` defaults to `https://api.mch.weixin.qq.com`).

## Alipay slots

| Slot | Default reference | Required |
| --- | --- | --- |
| `app_id` | `env://ALIPAY_APP_ID` | yes |
| `private_key` | `env://ALIPAY_PRIVATE_KEY` | yes |
| `public_key` | `env://ALIPAY_PUBLIC_KEY` | yes |
| `notify_url` | `env://ALIPAY_NOTIFY_URL` | no |
| `gateway` | `env://ALIPAY_GATEWAY` | no |

`private_key` signs the sorted request content with RSA2; `public_key` verifies
async notifications. Alipay refunds are synchronous responses — the adapter
declares no asynchronous refund notification, and no such path is simulated.

## Readiness, and why checkout is unavailable

Readiness is declared by each adapter through `SLOTS` and `REQUIRED_SLOTS`, and
`PaymentProvider.is_configured()` derives from them. A channel is registered for
checkout only when every required slot resolves; a channel with one unresolved
required slot is absent from the registry and cannot produce a paid order.

Three surfaces report the same truth:

1. `GET /api/v1/tenants/<tenant_id>/billing/capabilities/` returns
   `payment.available`, `payment.channels`, `payment.reason_code` and
   `payment.diagnostics` — a per-channel entry with `configured`, `missing`
   (slot names) and `required` (slot names). Slot names only: no reference URI,
   no value.
2. `POST .../billing/orders/` answers `409 PAYMENT_PROVIDER_NOT_CONFIGURED` when
   no channel is usable, so an order can never be created that nothing can pay.
3. `python manage.py check_payment_channels` prints each channel's status,
   the usable channels and the effective environment prefixes, and exits non-zero
   with a named cause when no channel resolves. `--allow-none` turns it into a
   report-only probe for a scheduled monitor.

The plan page in the studio renders the same diagnostics per channel, so a tenant
operator sees which merchant credential is missing rather than a generic
"payment unavailable".

## Onboarding sequence

1. Store the merchant material in the deployment secret store:
   `vault://merchant/wechatpay#private_key`,
   `vault://merchant/wechatpay#platform_cert`,
   `vault://merchant/wechatpay#api_v3_key`, and the remaining slots; enable
   `PLUGIN_VAULT_ADDR` / `PLUGIN_VAULT_TOKEN` (or a token resolver) so the vault
   provider can read them. Never inline a PEM, key or certificate in a manifest,
   a database row or a compose file.
2. Set the notify URLs to the deployment's public callback endpoints:
   `.../billing/payments/wechat/callback/` and
   `.../billing/payments/alipay/callback/`. These endpoints are unauthenticated
   by design and authenticate by signature only.
3. Run `python manage.py check_payment_channels` and require exit code 0 for at
   least one channel before enabling checkout.
4. Create one order in a sandbox merchant account, pay it, and confirm the
   callback moves the order to `paid` exactly once, grants the entitlement once
   and writes `billing.order.<target>` in the audit trail.
5. Refund a slice and confirm the reclaim ledger moves
   `TenantTokenBudget.monthly_limit` down once for that `refund_reference`.

## Failure semantics

- Signature failures, an untrusted platform certificate serial, an unparseable
  body and a failed decrypt all fail closed with `PAYMENT_SIGNATURE_INVALID` (or
  the specific provider error code) and an audit record; state does not move.
- A callback amount that does not match the order is rejected and audited
  (`PAYMENT_AMOUNT_MISMATCH`).
- A repeated callback applies once: the order row lock plus the unique grant and
  reclaim references make replay idempotent, so replay can neither pay twice nor
  reclaim twice.
- Upstream transport failures surface as `PAYMENT_PROVIDER_UPSTREAM_ERROR` /
  `PAYMENT_PROVIDER_UNAVAILABLE`; the order is left in its current state for
  reconciliation to catch, never marked paid.
- `ABNORMAL` and `CLOSED` refund notifications are recorded but never move the
  order state or reclaim quota.

## Boundaries

- Overseas models stay unpurchasable: `core/billing_views.py` reads
  `ModelProvider.deployment_region`, treats an empty value as fail-closed and
  refuses `overseas` with `PURCHASE_REGION_NOT_SUPPORTED` plus an audit event.
  The region gate is not bypassable by asking for a different channel.
- No third channel exists. A request naming any other channel is refused with
  `PAYMENT_CHANNEL_NOT_SUPPORTED`; it is not a hidden option.
- The test double (`FakeProvider`) is registered only when
  `BILLING_FAKE_PROVIDER_ENABLED` is explicitly set by a test, never by a
  deployment default.
