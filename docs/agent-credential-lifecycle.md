# Agent Node Credential Lifecycle

Agent nodes authenticate to the runtime API with a per-node bearer credential. The control plane stores only the SHA-256 digest; the raw token is returned once during enrollment or credential rotation.

## Enroll

A tenant owner or administrator creates an EPH or Target Agent node:

`POST /api/v1/tenants/{tenant_id}/projects/{project_id}/agent-nodes/`

The response contains `enrollment_token`. Deliver it to the intended host through a protected provisioning channel, configure the Agent with `AGENT_TOKEN`, and do not put it in Git, command payloads, logs, or support bundles.

## Rotate

A tenant owner or administrator can rotate the node credential:

`POST /api/v1/agent-nodes/{node_id}/credentials/rotate/`

The response contains the replacement `enrollment_token` exactly once. The previous token becomes invalid immediately, the node is marked offline, and the Agent must be restarted with the new token before it can heartbeat or poll commands again.

Rotation is rejected with HTTP 409 while the node has a running command. This prevents credential replacement from silently orphaning an active execution lease. Wait for the command to finish or reconcile its execution outcome first. Queued commands remain queued for the newly authenticated Agent.

## Revoke

`POST /api/v1/agent-nodes/{node_id}/revoke/`

Revocation is terminal. A revoked node cannot rotate its credential or authenticate again; enroll a replacement node instead.

## Operational requirements

- Use HTTPS with certificate validation for all Agent-to-control-plane traffic.
- Keep each EPH and Target Agent credential separate.
- Store the token in a root-readable secret file or an approved secret manager; avoid shell history and process arguments.
- Restrict access to the node enrollment and rotation APIs to tenant owners and administrators.
- Treat a lost token as compromised and rotate it; use revocation if the node itself is no longer trusted.
- Credential rotation does not stop a process already executing on the host. The running-command guard is therefore mandatory, and infrastructure operations with unknown outcomes require explicit reconciliation.


## BMC Redfish identity verification

The Fleet machine BMC configuration endpoint stores only credential and trust-bundle references. A separate POST request to /api/v1/tenants/{tenant_id}/projects/{project_id}/fleet/machines/{machine_id}/bmc-config/ performs read-only Redfish verification.

The built-in Vault KV v2 resolver is selected automatically when BMC_VAULT_ADDR and either BMC_VAULT_TOKEN or BMC_VAULT_TOKEN_RESOLVER are configured and BMC_CREDENTIAL_RESOLVER is unset. Set BMC_VAULT_NAMESPACE when using Vault Enterprise namespaces and BMC_VAULT_CA_BUNDLE when Vault uses a private CA. Secret references use vault://<mount>/<path>; the resolver reads them only below tenants/<tenant_id>/projects/<project_id>/ within the selected KV v2 mount. Both tenant_id and project_id are mandatory and validated before token resolution or network access. The credential secret must contain username and password, and an optional trust-bundle secret must contain pem or certificate. BMC_VAULT_TIMEOUT_SECONDS controls the bounded request timeout. An explicit BMC_CREDENTIAL_RESOLVER dotted Python callable overrides the built-in resolver.

For multi-tenant production, configure BMC_VAULT_TOKEN_RESOLVER as a dotted Python callable accepting keyword arguments tenant_id and project_id and returning a non-empty, whitespace-free, scope-limited Vault token. The resolver must obtain or exchange credentials using a trusted workload identity and enforce the requested tenant/project binding. A configured scoped-token resolver takes precedence over BMC_VAULT_TOKEN; an invalid result fails closed and never falls back to the global token. BMC_VAULT_TOKEN is retained only for development or a single-trust-boundary deployment and is not multi-tenant isolation. Path prefixing alone is not an authorization boundary: enforce matching Vault ACLs and restrict token policies to the corresponding tenant/project prefixes. Vault ACL deployment and token-exchange integration must be independently configured and tested. The platform API does not persist or log returned secret values.

The current verifier intentionally accepts literal private BMC IP addresses only, uses TLS certificate verification, disables proxy environment inheritance and redirects, applies short connection/read timeouts, limits each JSON response to 1 MiB and the Systems collection to eight members, and reads only /redfish/v1/Systems. DNS hostnames are rejected during verification to avoid DNS rebinding until a pinned-address transport is available.

Identity is marked verified only when the BMC reports exactly one System and both its UUID and serial match the Agent's previously collected DMI UUID and serial. Missing evidence remains candidate; mismatches and duplicate identities become conflict. This is a read-only identity check, not a hardware attestation or a provisioning authorization.

For production, use a dedicated Vault policy/token per control-plane trust boundary and grant only required tenant/project secret prefixes. The built-in resolver fails closed when Vault configuration or tenant/project scope is missing.


## Vault policy template for tenant/project isolation

The optional policy template at `deploy/vault/policies/bmc-credentials.hcl` grants read-only access to the BMC credential and Redfish trust-bundle prefixes derived from Vault identity entity metadata. Bind `tenant_id` and `project_id` metadata to the authenticated workload identity through a trusted administrator-controlled auth role or entity; never accept those metadata values from an untrusted Agent request. Attach the policy only to a token whose identity has those verified metadata values. Review the KV v2 mount name and secret layout before deployment. The template is not installed automatically and is not evidence of a deployed Vault policy.

The resolver now validates the Vault HTTPS origin, credential reference, tenant/project scope, and optional trust-bundle reference before invoking the scoped token resolver. Invalid configuration therefore cannot trigger a token-exchange side effect.


## Built-in Vault JWT workload identity

The built-in JWT login flow is enabled by `BMC_VAULT_JWT_PATH` and `BMC_VAULT_JWT_ROLE`. The token file must contain a short-lived workload JWT whose configured tenant and project claims match the request. The API performs a Vault JWT auth login against `/v1/auth/{BMC_VAULT_JWT_AUTH_MOUNT}/login`, then uses the returned short-lived Vault client token for KV v2 reads. The JWT signature and issuer/audience validation are performed by Vault's JWT auth role; the local claim check is only an additional scope mismatch guard and is not cryptographic verification.

Configure the Vault JWT auth role with a trusted issuer, explicit audience, bound subject/claims, short token TTL, and a policy restricted to the corresponding tenant/project secret paths. The JWT claims must be minted by a trusted identity provider, not copied from Agent request fields. A custom `BMC_VAULT_TOKEN_RESOLVER` takes precedence; the built-in JWT flow is used when no custom resolver is configured and a JWT path is present. Static `BMC_VAULT_TOKEN` is the final compatibility fallback and must not be used as a multi-tenant authorization boundary.


### Vault JWT auth role and ACL deployment example

The following is an operator-run example, not an automatic deployment. Replace the issuer, audience, tenant and project values with values issued by the trusted identity provider. Create a separate Vault JWT role for each tenant/project scope, or use an equivalent trusted auth broker that issues scope-bound Vault tokens.

1. Enable the JWT auth method and configure its trusted issuer/JWKS source:

```sh
vault auth enable -path=jwt jwt
vault write auth/jwt/config \
  oidc_discovery_url="https://identity.example.com" \
  bound_issuer="https://identity.example.com"
```

2. Install the policy after reviewing the KV v2 mount names. This example assumes BMC credentials are in the `secret` KV v2 mount and Redfish CA bundles are in the `pki` KV v2 mount:

```sh
vault policy write bmc-credentials deploy/vault/policies/bmc-credentials.hcl
```

3. Create a role whose JWT claims are validated by Vault and copied into entity metadata for the policy template:

```sh
vault write auth/jwt/role/bmc-reader-tenant-a-project-a \
  role_type=jwt \
  user_claim=sub \
  bound_audiences="ai-agent-studio" \
  bound_claims='{"tenant_id":"tenant-a","project_id":"project-a"}' \
  claim_mappings='{"tenant_id":"tenant_id","project_id":"project_id"}' \
  token_policies="bmc-credentials" \
  token_ttl="5m" \
  token_max_ttl="10m"
```

4. Configure the API with `BMC_VAULT_ADDR`, `BMC_VAULT_JWT_PATH`, `BMC_VAULT_JWT_ROLE`, and the claim names. Mount the short-lived JWT file from the trusted workload identity provider as read-only. Do not put a long-lived JWT or Vault token in the image, source control, or Agent command payloads.

The JWT auth role's issuer, audience, signature algorithm, bound claims, and TTL are the security boundary. The API's local claim comparison is defense in depth only. Test that a JWT for tenant A/project A cannot read tenant B/project B secrets, that missing claims fail login, and that token expiry is enforced. The policy template relies on Vault entity metadata populated by `claim_mappings`; if those metadata keys are absent, the policy must not be treated as tenant isolation. Use the actual KV v2 mount names in the policy and credential references. This example has not been applied to a live Vault server.


## Agent-to-control-plane TLS and mutual TLS

The node Agent uses the operating system trust store by default. For a private control-plane CA, set `AGENT_TLS_CA_FILE` to a PEM CA bundle mounted read-only into the Agent. To enable mutual TLS, also set both `AGENT_TLS_CERT_FILE` and `AGENT_TLS_KEY_FILE` to the node-specific client certificate and private key. The certificate and key must be mounted as files from a secret manager or protected provisioning channel; never bake them into the image or commit them to Git.

The client TLS context always requires certificate verification and hostname verification. A partial client certificate/key configuration fails closed, invalid certificate material fails closed, and any custom TLS configuration requires an HTTPS control-plane URL. Each Agent should receive a unique client certificate with a short validity period and revocation/rotation procedure. The control-plane ingress or service mesh must independently require and validate client certificates, map the verified certificate identity to the enrolled Agent node, and reject revoked or unmapped certificates. Client-side mTLS support alone does not enforce server-side mTLS; until ingress enforcement and certificate-to-node binding are deployed and tested, the per-node bearer credential remains an additional application authentication factor.
