# ADR 0005: Fresh Three-Agent Runtime

- Status: Accepted for the clean-runtime implementation branch
- Date: 2026-11-17

## Context

The current Agent implementation has separate EPH and Target entrypoint modules, but both delegate to `apps/node_agent/runtime.py`. That runtime imports legacy Node Agent modules for CAPI Pivot, chrony, delivery lifecycle, Ironic, Metal3 and provider compatibility. The entrypoint split therefore does not constitute an independent implementation.

The user has explicitly rejected continued use of the old Agent implementation. Existing Node Agent code is not a compatibility layer, shared runtime, source of business logic, or base for the replacement.

## Decision

Implement exactly three Agent roles from new code:

1. **Cloud Control Agent** — deployed in Alibaba Cloud or Tencent Cloud. Owns global scheduling, durable orchestration state, tenant/project scope, task dispatch, status aggregation and failure coordination. It does not approve, review or gate Target takeover.
2. **EPH Agent** — runs as a Kubernetes Pod inside the temporary, RAM-booted, single-node EPH Kubernetes cluster. Owns source-side preflight, temporary bootstrap checks and the source-side Cluster/BMH identity and digest evidence used for takeover verification. It does not perform the CAPI Pivot or the Target takeover.
3. **Target Agent** — one logical cluster-scoped service, deployed as three replicas across the three target Kubernetes Control Plane master nodes with anti-affinity, leader election and disruption protection. Owns target adoption, the CAPI Pivot (`clusterctl move`), CAPI/Metal3/Ironic coordination, target cluster lifecycle, Worker provisioning and postflight.

There is no Node Agent role, no per-Worker resident Agent, and no network-inventory Agent DaemonSet. Target Worker facts must be obtained from Kubernetes, CAPI, Metal3 and explicitly approved infrastructure APIs. Facts unavailable from those sources must be reported as unavailable, never fabricated.

## Clean implementation rules

- The replacement EPH and Target runtimes must not import `apps.node_agent`, execute its entrypoint, or depend on its modules.
- The new Cloud Control Agent must not reuse the old Node Agent command loop or worker task handlers as Agent runtime code.
- Shared code is limited to newly authored, versioned protocol/domain contracts with no runtime or infrastructure side effects.
- New Agent packages own their lifecycle, configuration validation, health/readiness, identity, mTLS transport, polling/streaming, command dispatch, cancellation, checkpoint and result reporting.
- Keep platform API persistence and UI concerns separate from Agent runtime processes. Any API contract consumed by Agents must be explicit, versioned and tested.
- Old Agent modules, image names, tests, manifests, RBAC, DaemonSets and documentation are removed only after all new implementations and deployment references have been migrated. No compatibility shim is retained.
- CI must test the new packages and reject imports/references to the legacy package and old image/runtime names. CI changes must not suppress functional failures.

## Non-negotiable infrastructure invariants

- EPH is exactly one temporary single-node Kubernetes cluster; it has no Worker pool.
- Target Agent is scheduled only on Target Control Plane master nodes. Its three replicas are spread across distinct masters; only the elected leader polls or executes commands.
- Target Agent resumes durable deployment checkpoints after CAPI Pivot. It owns Target adoption and the autonomous handoff; Cloud Control Agent observes and coordinates but does not substitute for Target ownership evidence.
- Target Control Plane chronyd synchronizes from approved external NTP sources and exposes the internal NTP service at the Ironic internal IP/VIP.
- Every Target Worker synchronizes only from that Ironic internal IP/VIP. Worker configuration must not contain external NTP pools or fallback sources. Verify endpoint reachability and actual synchronization before kubeadm join.
- EPH uses an independent bootstrap NTP source because Target Control Plane does not exist during EPH bootstrap.
- CAPI Pivot, autonomous Target takeover, BMH registry migration, EPH cleanup and Worker provisioning are separate checkpointed phases. Target Agent detects completed Target Control Plane readiness, validates cluster identity, then autonomously performs the handoff without Cloud approval or manual review. It migrates BMH registry ownership, cleans up the temporary EPH workload and proceeds with Worker provisioning. Each side effect requires positive preconditions, idempotency and postflight evidence. Any ambiguous ownership or missing evidence fails closed.
- Control Plane and Data Plane network roles remain separate. Pod traffic uses the Data Plane; Data Plane integration must support the specified Multus/SR-IOV/DPDK design while the Control Plane uses ordinary NIC connectivity.

## Delivery sequence

1. Define and test fresh versioned Agent identity, command, lease, checkpoint, result and error contracts.
2. Implement Cloud Control Agent service and its durable orchestration boundary.
3. Implement independent EPH runtime and source-side operations.
4. Implement independent Target runtime, three-master HA/leader election and target-side operations.
5. Implement Kubernetes manifests, image build definitions, credentials and deployment automation for the three roles.
6. Migrate API/UI integration to the new contracts.
7. Delete the legacy Node Agent package, image, DaemonSet, RBAC, manifests, tests and docs; remove all references.
8. Run unit, contract, integration, QEMU EPH bootstrap, multi-master Target HA, CAPI/Metal3/Ironic compatibility and hardware acceptance tests.

A passing unit test or QEMU test is not evidence of physical BMC/Ironic provisioning. Release claims must state the exact environment and side effects verified.
