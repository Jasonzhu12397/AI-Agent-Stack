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

EPH PKI settings:

- `EPH_PKI_MOUNT`: PKI secrets-engine mount, default `pki`.
- `EPH_PKI_ROLE`: signing role, default `agent-nodes`.
- `EPH_PKI_TTL`: requested certificate lifetime, default `24h`.

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
