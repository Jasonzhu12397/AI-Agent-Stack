# EPH NoCloud seed renderer

The platform API seed renderer is a pure, side-effect-free function in
\`core.eph_seed.render_nocloud_seed\`. It accepts a node-scoped configuration
and returns the three NoCloud datasource files: \`user-data\`, \`meta-data\`,
and \`network-config\`.

The renderer requires an HTTPS control-plane URL, a UUID node identity, a
one-time enrollment token, an immutable Agent image digest, a version 2
network configuration, and the trusted CA certificate used to verify the
Cloud Control Plane HTTPS endpoint.

The seed deliberately does not contain a client certificate or private key.
After boot, the EPH node generates its own private key and CSR locally. The
runtime enrollment API submits the CSR to the configured Vault PKI role,
consumes the one-time token after successful signing, and returns a
node-bound runtime token plus the signed certificate chain. The private key
never leaves the EPH node.

Cloud-init user-data is JSON after the \`#cloud-config\` header. JSON is valid
YAML, which avoids adding a YAML serializer dependency to the API. The
bootstrap configuration and control-plane CA are written as root-owned mode
0600 files under \`/etc/ai-agent-studio/eph-bootstrap\`.

This renderer deliberately does not build or upload an ISO, sign CSRs, rotate
certificates, or persist seed artifacts. Those operations belong to separate
orchestration and credential lifecycle components. Never place rendered seed
content in application logs or ordinary audit metadata.
