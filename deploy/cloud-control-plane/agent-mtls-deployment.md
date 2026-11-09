# Agent mutual TLS deployment contract

## Status

**Not enabled end to end.** The Agent process can be configured with a CA bundle and client certificate/key. The Django runtime API now compares a trusted ingress-provided SHA-256 fingerprint header with the unique AgentNode binding for certificate-bound nodes. However, the current cloud deployment does not yet configure and isolate the trusted mTLS ingress path, so this application check alone does not make production mTLS active.

Do not describe this deployment as mTLS-protected until every requirement in this document is implemented and tested against the deployed ingress.

## Current repository topology

- The cloud Compose profile publishes the web/Nginx service and keeps the Daphne API on the Compose network.
- Nginx currently listens on HTTP port 8080 and proxies `/api/` to Daphne.
- The cloud deployment guide expects an external cloud load balancer or host reverse proxy to terminate HTTPS.
- The Agent runtime routes are under `/api/v1/agent-runtime/`.
- Runtime API authentication validates the per-node Bearer token hash. For a node with an active certificate binding, it additionally requires both `X-Verified-Client-Cert-SHA256` and a peer address exactly matching `AGENT_MTLS_TRUSTED_PROXY_IP`. The address check is fail-closed when unset or invalid. The configured address must be the dedicated mTLS ingress peer as observed by Daphne, never the public web proxy or an untrusted load-balancer hop. The header is trustworthy only when that ingress strips any client-supplied copy and injects its own value.

## Required trust boundary

The production ingress must enforce all of the following before the application accepts certificate identity:

1. Terminate TLS on a dedicated Agent ingress/listener with mandatory client-certificate verification against the configured Agent CA. A missing, expired, untrusted, or invalid client certificate must fail the TLS handshake.
2. Derive the certificate SHA-256 fingerprint from the TLS session itself. Never accept a fingerprint, subject, issuer, serial, or verification result supplied by the Agent in an ordinary HTTP header.
3. Remove all externally supplied certificate identity headers before proxying. Only the trusted ingress may set the internal verified-certificate metadata. The application additionally checks the TCP peer address against the exact `AGENT_MTLS_TRUSTED_PROXY_IP` configured on the API service; an empty value rejects all certificate-bound runtime requests.
4. Ensure the application listener receiving that metadata is reachable only from the trusted ingress network. Do not publish the Daphne port publicly or allow untrusted containers/hosts to reach it.
5. Bind the verified fingerprint to one AgentNode record, enforce uniqueness and tenant/project scoping, and let the application compare the trusted fingerprint header on every runtime request. Revocation locks the node out of runtime authentication until an administrator binds a replacement certificate.
6. Apply the same identity check to heartbeat, inventory, command polling, lease renewal, checkpoint, and command-result endpoints. Bearer-token authentication remains an additional credential, not a substitute for certificate verification.
7. Keep browser/user API traffic separate from the Agent mTLS listener so browser sessions are not required to present Agent certificates.

A cloud load balancer may perform the TLS verification, but its verified identity must be conveyed over a private, authenticated backend channel with spoofed client headers stripped. If the chosen load balancer cannot provide that guarantee, use a dedicated TLS proxy under the deployment team's control.

## Certificate lifecycle

- Enrollment: issue a short-lived bootstrap token through the authenticated Studio enrollment flow; generate or provision a unique client keypair for the Agent.
- Binding: register only the certificate fingerprint and non-secret metadata. Never store private keys or PEM private-key material in the database.
- Rotation: support overlap only for an explicitly bounded transition period; audit the old and new fingerprints and revoke the old binding after successful Agent confirmation.
- Revocation: reject the certificate immediately after revocation and invalidate active Agent sessions/leases according to the runtime policy.
- Recovery: require an administrator-authorized re-enrollment path. Do not silently fall back to Bearer-only authentication when mTLS is required.

## Required application and integration tests

- no client certificate / TLS handshake rejection;
- untrusted CA, expired certificate, malformed certificate and wrong fingerprint;
- forged certificate headers sent directly to the application;
- direct access to the application listener from an untrusted network;
- certificate bound to a different AgentNode;
- revoked certificate and old certificate after rotation;
- duplicate fingerprint registration;
- cross-tenant and cross-project certificate replay;
- every Agent runtime endpoint rejects a missing or mismatched verified identity;
- valid certificate plus valid node token succeeds;
- audit events contain fingerprints and lifecycle status only, never private keys or bearer tokens.

## Release gate

Do not enable production mTLS enforcement until the ingress configuration, application fingerprint binding, certificate lifecycle endpoints, and negative integration tests are all deployed together. Until then, report the state accurately as **Agent TLS client configuration, certificate fingerprint binding, and trusted-proxy peer pinning available; dedicated mandatory-client-certificate ingress and end-to-end mTLS deployment pending**.
