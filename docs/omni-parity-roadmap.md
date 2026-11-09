# Omni-Inspired Infrastructure Platform Roadmap

## Product target

AI Agent Studio should provide an end-to-end infrastructure management experience comparable in breadth to Sidero Omni, while retaining the platform's AI-native workflow engine and the project's required CAPI + Metal3 + Ironic bare-metal architecture.

This is a capability and product-scope reference, not a plan to copy Omni source code or adopt Talos-specific implementation details. Omni is built around Talos Linux; this project must support the selected Linux/RAM-boot EPH environment, Kubernetes distributions and the user's bare-metal network design.

## Product principles

- One control plane for machine inventory, cluster lifecycle, operations, policy, audit and AI-assisted automation.
- Machines are first-class resources with identity, hardware/network inventory, lifecycle state, ownership, maintenance state and cluster assignment.
- Cluster desired state and observed state are separate. A plan, approval or dry-run must never be displayed as a successful deployment.
- All infrastructure mutations are typed, scoped, idempotent, approved where required, leased, audited and recoverable.
- The cloud Supervisor is durable. EPH memory, local etcd, Agent process memory and temporary credentials are disposable.
- Keep Control Plane and Data Plane networks distinct. Workload data traffic follows the declared Data Plane profile; management and Agent control traffic remain reachable over the Control Plane network.
- Provider-specific writes remain disabled until compatibility and destructive-operation tests pass against the exact versions and hardware.

## Capability parity map

| Capability area | Expected platform behavior | Project implementation direction |
| --- | --- | --- |
| Fleet / machine inventory | Discover, enroll, label, filter and inspect machines; show health and lifecycle | Machine inventory API and UI backed by EPH/Target Agent hardware, BMC, NIC, PCI, NUMA and Kubernetes observations |
| Machine lifecycle | Allocate, reserve, assign, drain, maintain, reprovision and safely reclaim | Explicit machine state machine; CAPI + Metal3 BareMetalHost + Ironic/IPA adapters; guarded EPH reclamation |
| Cluster lifecycle | Create, import, scale, upgrade, recover and delete clusters | ClusterDeployment desired state, CAPI Cluster/KCP/MachineDeployment reconciliation and observed-state tracking |
| Cluster templates | Reusable, versioned cluster configuration with validation and diff | Versioned deployment profiles for Kubernetes, CAPI/Metal3 providers, OS image, storage and network |
| Machine configuration | Inspect configuration, apply safe changes and report drift | Versioned machine configuration profiles, policy validation, staged rollout and rollback |
| Health and operations | Cluster/machine health, events, logs, diagnostics and metrics | Unified observability API, event timeline, node diagnostics and audit correlation |
| Upgrades | Plan, pre-check, roll out and verify upgrades | Compatibility gates, control-plane-first rollout, worker batches, disruption budgets and postflight |
| Access and security | Enterprise identity, role-based access and secure remote connectivity | OIDC/SAML, tenant/project RBAC, mTLS Agent identity, short-lived credentials, approval separation and audit |
| High availability | Durable management service and resilient Kubernetes API access | Cloud-hosted Supervisor HA, PostgreSQL HA, Redis as non-authoritative cache, backup/restore and API endpoint strategy |
| Edge / disconnected operations | Secure management across firewalls and constrained networks | Agent-initiated outbound TLS, reconnect-safe command leases, bounded offline queue and explicit stale-state display |
| Integrations | Infrastructure provider and ecosystem integration | CAPI, CAPM3, Metal3, Ironic, BMC vendors, CSI, CNI, registry, monitoring and ticketing connectors |
| AI operations | Not the core Omni differentiator; this project adds it | LLM-assisted intent-to-plan, policy checks, approval, typed execution, progress summaries and knowledge-backed runbooks |

## Required product navigation

The operator experience should grow into these connected areas:

1. **Dashboard** — fleet capacity, cluster health, active operations, failed tasks and policy alerts.
2. **Infrastructure / Machines** — all discovered machines, hardware details, BMC and network identity, labels, pool membership, lifecycle and maintenance actions.
3. **Clusters** — cluster list, topology, control-plane and worker pools, Kubernetes/provider versions, health, events and lifecycle actions.
4. **Cluster Templates** — reusable desired-state profiles, validation, version history and rendered diff.
5. **Operations** — deployment, scale, upgrade, repair, drain, reprovision, pivot and reclaim workflows with approvals and live step status.
6. **Network** — Control Plane/Data Plane profiles, NIC/PCI/VF inventory, Multus attachments, SR-IOV resources, OVS/Calico status and MTU/route validation.
7. **Storage** — CSI capabilities, storage classes and volume health.
8. **Observability** — machine/cluster events, logs, metrics, diagnostics and audit timeline.
9. **Identity & Governance** — OIDC/SAML, tenant/project roles, policies, approvals, credentials and audit.
10. **AI Agent Studio** — model routing, Agents, workflows, knowledge, integrations and natural-language operations.

Existing Vue navigation should be evolved rather than replaced. Every operational screen must be backed by real APIs and meaningful loading, empty, error, permission-denied and stale-data states; no static mock dashboard should imply live infrastructure.

## Delivery phases and acceptance gates

### Phase A — Fleet foundation

- Introduce durable machine inventory and lifecycle resources, with stable hardware identity and tenant/project scope.
- Correlate Agent identity, BMC identity, system UUID/serial, NIC MAC/PCI identity and BareMetalHost identity without treating any single mutable field as sufficient proof.
- Add inventory APIs, filters, detail view, labels, pool membership and observation timestamps.
- Acceptance: tests for duplicate identity, tenant isolation, stale heartbeat, identity conflict, revoked Agent and incomplete hardware inventory.

### Phase B — Cluster operations

- Persist desired cluster specification separately from observed cluster state.
- Add create/import, topology, control-plane and worker-pool views, reconciliation status and operation history.
- Add safe scale and maintenance workflows with preflight, approval, bounded rollout and postflight.
- Acceptance: API and UI integration tests prove that plans and dry-runs cannot become successful deployments; incomplete or unready replicas block progression.

### Phase C — Real bare-metal provisioning

- Implement version-pinned CAPI/CAPM3/Metal3/Ironic adapters, starting with read-only discovery and server-side dry-run.
- Add narrowly scoped create/update operations for Cluster, KubeadmControlPlane, MachineDeployment, BareMetalHost and required templates.
- Add BMC/IPA power, boot, inspection and provisioning actions behind separate approval and capability gates.
- Acceptance: isolated hardware lab tests verify provisioning, failure recovery, idempotency, audit, credential redaction and no cross-tenant effects. No production enablement based on mocks alone.

### Phase D — Day-2 lifecycle

- Control-plane and worker upgrades, rolling maintenance, drain/uncordon, replacement, cluster repair and deletion.
- Machine pool capacity, scheduling/placement constraints, GPU/NUMA/SR-IOV-aware inventory and workload readiness.
- Acceptance: fault-injection tests, rollback/retry tests, quorum and PodDisruptionBudget checks, and post-operation health verification.

### Phase E — EPH handoff and safe reclamation

- Provision the first target Control Plane, deploy Target Agent there, resume durable task state, pivot CAPI state, verify reconciliation, then provision remaining Control Plane and Data Plane workers.
- Reclaim the original EPH host only after identity re-verification, CAPI pivot verification, target worker readiness and all configured security/reset steps.
- Acceptance: physical-lab end-to-end tests prove the complete lifecycle. Until then, reclamation stays fail-closed.

### Phase F — Enterprise operations

- OIDC/SAML, fine-grained RBAC, HA deployment, backup/restore, multi-site fleet views, audit export, policy packs and secure disconnected-site operation.
- Acceptance: security tests, restore drills, HA failover tests and documented operational SLOs.

## Architecture boundaries

- **Cloud Supervisor:** API, authentication, policy, LLM orchestration, durable workflows, approval, audit, inventory and command dispatch.
- **EPH Agent:** temporary bootstrap-cluster inspection and explicitly authorized bootstrap operations.
- **Target Agent:** target-cluster reconciliation, health, day-2 operations and worker readiness.
- **CAPI / CAPM3 / Metal3 / Ironic / IPA:** infrastructure provisioning control chain; adapters are version-aware and independently testable.
- **PostgreSQL:** authoritative desired state, observed snapshots, commands, checkpoints, approvals and audit.
- **Redis:** cache and ephemeral coordination only; never the sole source of deployment state.
- **Network:** management/control traffic on the ordinary Control Plane network; workload traffic on the declared Data Plane network using Multus and SR-IOV/DPDK where required.

## Current known gaps

At the time this roadmap was added, the repository has dual-Agent enrollment and command leasing, persisted cluster planning/checkpoints, read-only CAPI/Metal3 observation, network inventory, approval primitives and a Vue operator workspace. The actual CAPI/Metal3/Ironic provisioning write adapter is not enabled. Machine inventory is not yet a complete first-class fleet lifecycle service. Full cluster upgrade/scale/repair operations, production OIDC/SAML and management-plane HA are not complete. The EPH reclaim executor must remain fail-closed.

The GitHub CI suite validates code, tests, frontend build and Compose smoke behavior; it does not substitute for physical bare-metal provisioning, BMC/IPA tests, CAPI pivot tests or network dataplane performance tests.
