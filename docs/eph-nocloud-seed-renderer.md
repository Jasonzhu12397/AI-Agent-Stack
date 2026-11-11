# EPH NoCloud seed renderer

The platform API seed renderer is a pure, side-effect-free function in
`core.eph_seed.render_nocloud_seed`. It accepts a node-scoped configuration
and returns the three NoCloud datasource files: `user-data`, `meta-data`,
and `network-config`.

The renderer requires an HTTPS control-plane URL, a UUID node identity, a
one-time enrollment token, an immutable Agent image digest, a version 2
network configuration, and CA/client TLS material. It never generates or
persists credentials. The caller must obtain credentials from the existing
node enrollment and certificate lifecycle, and must deliver the returned
seed as a per-node secret artifact.

Cloud-init user-data is JSON after the `#cloud-config` header. JSON is valid
YAML, which avoids adding a YAML serializer dependency to the API. Credential
files are written as root-owned mode 0600 files under
`/etc/ai-agent-studio/eph-bootstrap`.

This renderer deliberately does not build or upload an ISO, rotate node
credentials, mint certificates, or persist seed artifacts. Those operations
must be implemented by a privileged orchestration service with explicit
secret-storage and artifact-expiration policies. Never place rendered seed
content in application logs or ordinary audit metadata.
