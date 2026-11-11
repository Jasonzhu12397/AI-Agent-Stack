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


## Build a per-node CIDATA ISO

The download API intentionally returns a ZIP, not a bootable disk image. Convert
that per-node archive into a NoCloud seed ISO on a trusted provisioning host:

\`\`\`bash
bash eph-iso/create-seed-iso.sh eph-seed-{node-id}.zip eph-seed-{node-id}.iso
\`\`\`

The builder accepts exactly the three NoCloud files, rejects path traversal,
duplicate/unexpected entries, symlinks, encrypted entries, and oversized files,
then creates an ISO9660/Joliet image with volume label \`CIDATA\`. The output ISO
and its checksum file are mode \`0600\`. It refuses to overwrite an existing
output file. Keep the ISO on protected storage and destroy it after the node
has booted and exchanged its one-time enrollment credential.

Attach the resulting ISO as a separate virtual CD-ROM/USB device while booting
the generic EPH Live ISO. Do not bake per-node tokens, certificates, or network
configuration into the generic Live ISO.


## Expired seed payload cleanup

Expired, undownloaded seed artifacts are no longer downloadable immediately when
their expiry time passes. To erase their encrypted payloads while retaining the
audit/lifecycle metadata, run the Django management command from the repository
root:

\`\`\`bash
python manage.py cleanup_expired_eph_seed_artifacts --dry-run
python manage.py cleanup_expired_eph_seed_artifacts --batch-size 500
\`\`\`

The command clears only \`encrypted_payload\` for artifacts whose expiry has
passed and which have not been downloaded or explicitly revoked. It retains the
artifact row, node/user references, expiry timestamp, download-token hash, and
creation timestamp for audit and troubleshooting. Downloaded and unexpired
artifacts are not modified. A batch size from 1 to 10000 is accepted; the
default is 500. The dry-run reports the number of payloads that would be erased.

Run this command from a scheduled platform maintenance job (for example, once
daily). The job must use the same database and application settings as the
Platform API. Do not place the Fernet key or enrollment/download tokens in
the scheduler command line or logs.
