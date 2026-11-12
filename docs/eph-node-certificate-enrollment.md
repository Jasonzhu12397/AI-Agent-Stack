# EPH node certificate enrollment

EPH Agents generate their private key and CSR locally. The private key must never
leave the EPH node or be included in the NoCloud seed.

## Runtime exchange

1. The platform creates an EPH AgentNode and returns its enrollment token once.
2. The EPH bootstrap process generates a client key and CSR locally.
3. It sends the CSR to `POST /api/v1/agent-runtime/nodes/{node_id}/certificate/`
   with the enrollment token in the Authorization Bearer header.
4. The API locks the AgentNode row, verifies the token hash and EPH node type,
   then submits the CSR to the configured Vault PKI role.
5. On successful signing, the API stores only the SHA-256 fingerprint, replaces
   the one-time enrollment-token hash with the hash of a new node-bound runtime
   token, and returns the client certificate, CA chain, and runtime token. The
   private key is never returned.
6. Replaying the enrollment token cannot issue another certificate. Certificate
   rotation uses the separately authorized certificate lifecycle, not the
   enrollment endpoint.

The endpoint fails closed with HTTP 503 when Vault workload identity or the PKI
signer is unavailable. The endpoint does not fall back to self-signed
certificates, shared keys, or a locally generated CA. If signing is unavailable,
the original enrollment token remains valid for retry.

## Vault configuration

Configure the existing Vault workload identity settings:

- `BMC_VAULT_ADDR`: HTTPS Vault origin.
- `BMC_VAULT_JWT_PATH`, `BMC_VAULT_JWT_ROLE`: workload identity JWT file and
  Vault JWT auth role.
- `BMC_VAULT_JWT_TENANT_CLAIM`, `BMC_VAULT_JWT_PROJECT_CLAIM`: claims that
  scope the API workload identity to the AgentNode tenant and project.
- `BMC_VAULT_CA_BUNDLE`: optional trusted CA bundle for Vault TLS.

EPH PKI trust configuration:

For the cloud Compose deployment, set `EPH_PKI_TRUSTED_CA_HOST_PATH` in
`.env.cloud` to the absolute host path of that PEM bundle. Compose mounts it
read-only at `/etc/ssl/certs/eph-trusted-root.pem` and sets
`EPH_PKI_TRUSTED_CA_BUNDLE` to that in-container path. Keep the trust bundle
outside the repository and restrict host file permissions to the API service
operator. If the host path is unset, Compose mounts an empty `/dev/null`
source and certificate enrollment fails closed.

- `EPH_PKI_TRUSTED_CA_BUNDLE`: required filesystem path to an independently
  provisioned PEM bundle containing trusted root CA certificates for the EPH
  issuing hierarchy. Never populate this file from the Vault response.
  Enrollment fails closed if the bundle is missing, unreadable, malformed, or
  does not validate the returned issuing chain.

The API validates CA basic constraints, `keyCertSign`, certificate validity,
issuer ordering, every intermediate signature, and the final chain against
this independent trust bundle. The validated issuing chain and root are
returned to the EPH node.

EPH PKI settings:

- `EPH_PKI_MOUNT`: PKI secrets-engine mount, default `pki`.
- `EPH_PKI_ROLE`: signing role, default `agent-nodes`.
- `EPH_PKI_TTL`: requested certificate lifetime, default `24h`.

The role must issue a leaf certificate suitable only for TLS client
authentication. Configure its certificate profile explicitly:

- `ext_key_usage` must include `ClientAuth` (do not rely on an implicit Vault
  default).
- `key_usage` must include `DigitalSignature`.
- `client_flag=true` and `server_flag=false`.
- The leaf must have `basicConstraints CA=false`.
- The role maximum TTL must be bounded by the platform certificate policy;
  the API also rejects certificates that are not currently valid.

The API rejects a signed leaf without the client-auth extended key usage,
digital-signature key usage, non-CA basic constraints, the exact node-bound
common name, or a currently valid time window. A Vault signing response that
does not meet this profile is treated as an invalid issuance; the enrollment
token is not consumed.

The Vault PKI role must be provisioned out-of-band with a restrictive allowed
domain such as `agents.internal`, hostname enforcement, no arbitrary names,
no IP SANs, and a maximum TTL no greater than the organization's node-agent
certificate policy. Vault policy must allow the platform workload identity to
sign only through this role. The API supplies the node-bound common name
`agent-node-{node_uuid}.agents.internal`; Vault remains the cryptographic
authority and validates the CSR signature.

## Security boundaries

- The CSR is limited to 16 KiB and must be a PEM PKCS#10 certificate request.
- Only EPH nodes can use this endpoint; Target Agent enrollment is separate.
- The one-time token is consumed only after successful signing and database
  binding; the returned runtime token is bound to the same node record.
- The node private key is generated and retained by the node.
- The database stores a certificate fingerprint and runtime-token hash, never
  private key material or a plaintext runtime token.
- The audit event records the fingerprint and node identity, not the CSR,
  enrollment token, certificate body, or private key.
- If Vault is unavailable, enrollment remains pending and the one-time token
  remains usable for a retry. No certificate is issued by a fallback path.

## Certificate renewal and activation

An enrolled EPH node renews its certificate without consuming a second
enrollment token. The renewal API requires the currently active mTLS identity
and the existing node-bound runtime bearer token:

- `POST /api/v1/agent-runtime/nodes/{node_uuid}/certificate/renew/` with
  exactly one `csr_pem` field.
- The API signs the node-bound CSR through the configured Vault PKI role and
  stores only the pending certificate fingerprint and issue timestamp.
- The response contains the new leaf certificate and validated CA chain. It
  does not contain a private key or a replacement bearer token.
- The old certificate remains active while the pending certificate is staged.
  A second renewal is rejected while a non-expired pending certificate exists.
- The node must install the new certificate and locally generated matching
  private key, establish a fresh mTLS connection, and call
  `POST /api/v1/agent-runtime/nodes/{node_uuid}/certificate/activate/` with an
  empty JSON object.
- Activation accepts only the pending fingerprint presented by the trusted
  mTLS ingress and atomically promotes it. The previous fingerprint stops
  authenticating immediately after the transaction commits.
- Pending certificates expire after ten minutes. An expired pending identity
  cannot be activated; the active certificate remains unchanged.
- Repeating activation with the already activated certificate is idempotent,
  allowing recovery when the activation response is lost.

The API endpoints and database state implement the server-side staged
activation protocol. The node-side certificate file rotation and fresh TLS
handshake orchestration must be deployed before enabling unattended renewal
in production.

## EPH Agent automatic certificate rotation

The EPH Agent enables automatic renewal when its active client certificate has
six hours or less remaining. The agent creates a new P-256 key and CSR inside
the EPH node, sends only the CSR over the existing mTLS connection, validates
the returned node identity, key match, issuer chain, and fingerprint, and
stages the new key and certificate in the local `agent-control-plane-mtls`
Kubernetes Secret.

The node Agent ServiceAccount receives a namespace-scoped Role limited to
`get`, `patch`, and `update` on that single Secret. It does not receive
permission to list or read other Secrets. The pending key is retained in that
EPH-local Secret so a container restart can resume activation. It is never
sent to the Cloud Agent or certificate issuer.

The agent establishes the activation request using a fresh TLS context loaded
from the pending certificate and private key. Only after the activation API
confirms the new mTLS identity does it promote the pending keypair to the
active `tls.crt` and `tls.key` Secret data. If the process restarts after the
server activates but before the Secret promotion completes, it retries
activation; the server's activation response is idempotent.

Configuration in the EPH Agent Deployment:

- `AGENT_TLS_ROTATION_ENABLED=true` enables automatic renewal.
- `AGENT_TLS_ROTATION_RENEW_BEFORE_HOURS=6` sets the renewal threshold.
- `AGENT_TLS_ROTATION_SECRET_NAME=agent-control-plane-mtls` selects the only
  Secret the ServiceAccount may update.
- `POD_NAMESPACE` is populated through the Kubernetes downward API.

An expired pending request is discarded locally after nine minutes, leaving
the old active certificate untouched. The API's pending activation expires
after ten minutes, so the agent can safely request a fresh certificate after
the old pending record expires. If Cloud signing, Kubernetes Secret access, or
the new mTLS handshake fails, the agent does not promote the new certificate.

### EPH node-side renewal trust validation

Before staging a renewed client certificate, the EPH Agent validates the
certificate subject, local private-key match, validity window, issuer signature,
and every CA signature in the returned chain. Every CA must assert
`BasicConstraints(ca=true)` and `KeyUsage(keyCertSign=true)`; the terminal
root must be a valid self-signed CA and match a self-signed CA root already
present in the active certificate bundle. An active client leaf certificate is
never accepted as a trust anchor. The agent also enforces CA path-length
constraints and rejects expired or not-yet-valid CA certificates.
