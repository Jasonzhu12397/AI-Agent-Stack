# EPH seed artifact API

The Cloud Control Plane creates and manages per-node NoCloud seed artifacts.
The API encrypts the seed payload at rest and never places the node private key
or client certificate in the seed.

## Configuration

Set \`EPH_SEED_ENCRYPTION_KEY\` to a dedicated Fernet key. Generate it in a
secret-management workflow with:

\`\`\`bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
\`\`\`

Store the key in the deployment secret manager and mount it into every API
replica. Do not commit it to an environment file, CI log, or source control.
If it is absent or invalid, artifact creation and download fail closed with
HTTP 503. Back up the key securely: losing it makes undownloaded encrypted
artifacts unrecoverable. Rotate it only after all outstanding artifacts have
expired, been downloaded, or been revoked.

The EPH PKI signer must also be configured. See
\`docs/eph-node-certificate-enrollment.md\` for Vault JWT and PKI role settings.

## Create

An administrator first enrolls an EPH AgentNode using the normal Agent Node
API and receives its one-time enrollment token. Then call:

\`\`\`http
POST /api/v1/tenants/{tenant_id}/projects/{project_id}/agent-nodes/{node_id}/seed-artifacts/
Authorization: session cookie
Content-Type: application/json
\`\`\`

The request contains the one-time enrollment token, HTTPS control URL, pinned
Agent image digest, hostname, cloud-init v2 network configuration, and the
Cloud Control Plane CA certificate. The enrollment token is verified against
the node's stored hash and is not persisted in plaintext by the artifact API.

The optional \`expires_in_seconds\` defaults to 1800 and is restricted to
60–3600 seconds. Only one active undownloaded artifact, or one previously
downloaded artifact for the same enrollment token, is allowed per node.

The response includes an artifact UUID, a one-time download token, an expiry,
and a download URL. Treat the download token as a secret.

## Download

\`\`\`http
GET /api/v1/agent-runtime/seed-artifacts/{artifact_id}/download/
Authorization: Bearer {download_token}
\`\`\`

The endpoint returns a ZIP containing exactly \`user-data\`, \`meta-data\`, and
\`network-config\`. The token is one-time. On successful download, the
encrypted payload is erased and the artifact is marked consumed. Responses
set \`Cache-Control: no-store\`.

## Revoke

\`\`\`http
POST /api/v1/tenants/{tenant_id}/projects/{project_id}/agent-nodes/{node_id}/seed-artifacts/{artifact_id}/revoke/
\`\`\`

An administrator can revoke an artifact before download. Revocation erases
its encrypted payload. Node revocation and node credential rotation also
invalidate every undownloaded seed artifact for that node.

## Lifecycle and limitations

- Artifact payloads are encrypted using Fernet before database persistence.
- The database stores only a SHA-256 hash of the download token.
- The seed itself contains the enrollment token, so downloaded seed media
  remains sensitive and must be protected and destroyed after boot.
- Expired artifacts cannot be downloaded. Expired encrypted rows are retained
  as metadata and should be cleaned by a future scheduled retention job.
- The API does not build an ISO or attach virtual media. The returned ZIP is
  the NoCloud seed content; an image builder/virtual-media controller remains
  a separate workflow component.
- No real bare-metal provisioning or BMC side effect is performed by these
  endpoints.
