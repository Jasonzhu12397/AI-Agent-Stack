# Agent mutual TLS deployment contract

## Status

**Ingress implementation added; production activation remains operator-controlled and requires deployment validation.** The Agent process can be configured with a CA bundle and client certificate/key. The cloud Compose profile now includes an isolated HAProxy listener that requires client certificates, derives the SHA-256 fingerprint from the TLS peer certificate, strips client-supplied identity headers, and forwards only Agent runtime routes. Django checks both the fingerprint and the source IP against the dedicated internal network CIDR. `AGENT_MTLS_REQUIRED=true` rejects every unbound Agent, preventing Bearer-only fallback. The profile is opt-in until real certificates, network reachability, and live Agent handshakes are validated.

Do not describe this deployment as mTLS-protected until every requirement in this document is implemented and tested against the deployed ingress.

## Current repository topology

- The cloud Compose profile publishes the web/Nginx service and keeps the Daphne API on the Compose network. The API also joins a dedicated internal `agent_mtls` network shared only with the optional HAProxy Agent ingress.
- Nginx currently listens on HTTP port 8080 and proxies `/api/` to Daphne. The web listener explicitly clears `X-Verified-Client-Cert-SHA256` before proxying, so a browser or ordinary HTTP client cannot smuggle a certificate fingerprint through that route.
- The cloud deployment guide expects an external cloud load balancer or host reverse proxy to terminate HTTPS.
- The Agent runtime routes are under `/api/v1/agent-runtime/`.
- Runtime API authentication validates the per-node Bearer token hash. For a node with an active certificate binding, it additionally requires both `X-Verified-Client-Cert-SHA256` and a peer address exactly matching `AGENT_MTLS_TRUSTED_PROXY_IP`. The exact peer address takes precedence over the optional CIDR fallback, and the check is fail-closed when neither is configured or the value is invalid. When `AGENT_MTLS_REQUIRED=true`, unbound nodes are also rejected, so Bearer-only fallback is disabled.

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


## Enable the cloud mTLS ingress profile

1. Provision an ingress server certificate and private key as a single PEM file at the path configured by `AGENT_MTLS_SERVER_PEM`. Its SAN must include the DNS name used by EPH and Target Agents. Provision the issuing client CA certificate at `AGENT_MTLS_CLIENT_CA_PEM`. Keep the private key readable only by the deployment administrator and the ingress container.
2. Configure `AGENT_MTLS_NETWORK_SUBNET` and `AGENT_MTLS_INGRESS_IP`. Compose assigns that exact static IP to HAProxy and sets `AGENT_MTLS_TRUSTED_PROXY_IP` to the same value in Django. The API compares the TCP peer for exact equality; do not widen this to the whole Docker subnet. The optional CIDR setting is only a fallback for non-Compose deployments.
3. Set `COMPOSE_PROFILES=agent-mtls` and `AGENT_MTLS_REQUIRED=true`. Keep `AGENT_MTLS_BIND_ADDRESS=127.0.0.1` when a host-level TLS passthrough proxy connects locally. If Agents connect directly to the VM, bind the port to the required interface and restrict the cloud security group to the Agent source ranges.
4. Run the cloud deployment script. The HAProxy listener exposes only `/api/v1/agent-runtime/`, requires a valid client certificate, and forwards the SHA-256 digest of the TLS peer certificate. All other paths receive 404.
5. Bind each Agent's certificate fingerprint through the authenticated Studio certificate-binding endpoint before runtime communication. With mandatory enforcement enabled, an Agent without a binding cannot heartbeat or poll commands.
6. Verify valid and invalid certificate handshakes from actual Agent hosts, then test heartbeat, inventory, command poll, lease renewal, checkpoint and result submission. Do not consider Compose validation or a proxy configuration check equivalent to a live mTLS acceptance test.

The profile is intentionally opt-in. If `AGENT_MTLS_REQUIRED=true` is set without starting the `agent-mtls` profile, Agent runtime authentication fails closed.

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

Do not enable production mTLS enforcement until the ingress configuration, application fingerprint binding, certificate lifecycle endpoints, negative integration tests, and real Agent handshake tests are deployed together. The repository now contains the optional HAProxy ingress profile and mandatory application policy; actual production mTLS remains unverified until deployed with operator-provisioned certificates and tested from EPH/Target Agent hosts.
