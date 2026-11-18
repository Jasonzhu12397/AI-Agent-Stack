# Three-Agent Bare-Metal Kubernetes Delivery Architecture

> **Authoritative architecture:** exactly three Agent roles exist. The legacy Node Agent and Network Inventory Agent are not part of the new runtime or deployment topology. See [ADR 0005](adr/0005-three-agent-fresh-runtime.md).

## 1. Agent roles and placement

1. **Cloud Control Agent** runs in the Alibaba Cloud or Tencent Cloud management environment. It owns durable orchestration state, task scheduling, approval evidence, command dispatch, leases, status aggregation, audit correlation and failure coordination. It does not directly operate target Kubernetes resources.
2. **EPH Agent** runs in the temporary EPH Live OS / temporary single-node Kubernetes management environment. EPH is exactly one temporary Kubernetes node and has no Worker pool. EPH Agent performs source-side hardware/network preflight and records the source-side Cluster/BMH identity and digest evidence; it does not perform the CAPI Pivot or the Target takeover. Its local etcd, Pod filesystem and EPH-only credentials are disposable.
3. **Target Agent** runs in the target Kubernetes cluster on Control Plane Master nodes. It is one logical cluster-scoped Agent deployed as three replicas, one per master, with Kubernetes Lease leader election. Only the leader claims or executes commands. It owns Target adoption, the CAPI Pivot (`clusterctl move`), CAPI/Metal3/Ironic coordination, BMH ownership verification, EPH handoff/cleanup and Target Worker provisioning.

There is no per-Worker Agent and no Network Inventory Agent DaemonSet. Worker facts are obtained through Kubernetes, CAPI, Metal3 and approved infrastructure APIs. Missing evidence is reported as unavailable, not inferred.

## 2. Ordered deployment lifecycle

`eph_single_node_ready -> target_control_plane_ready -> target_agent_ready -> capi_pivot_verified -> bmh_registry_migrated -> eph_cleanup_verified -> worker_provisioning -> workers_ready -> completed`

- EPH Agent first proves the temporary single-node environment and source inventory are healthy.
- Cloud Control Agent coordinates and records the deployment, but does not own target adoption or approve EPH cleanup.
- Target Control Plane masters are provisioned before Target Workers.
- Target Agent starts on the Target Control Plane, restores the durable checkpoint and verifies the target API, etcd and required controllers.
- The Target Agent pivots CAPI/Metal3 management state from the EPH source cluster to the Target cluster. Cluster identity, owner references, controller reconciliation and exact BMH identity sets must be verified.
- EPH cleanup is an autonomous Target Agent operation only after CAPI Pivot, target controller readiness, BMH ownership and credential handoff are positively verified. Any missing evidence leaves EPH intact.
- Target Agent then provisions Workers through CAPI/Metal3/Ironic and verifies host, Node, network and NTP postflight.

All external side effects require idempotency keys, durable checkpoints before and after execution, bounded leases, explicit approval where required, and postflight evidence. Unknown outcomes fail closed and require reconciliation rather than blind retries.

## 3. Hierarchical NTP requirements

- **EPH Single-Node:** configure chronyd with an independent bootstrap NTP source before `kubeadm init`. EPH cannot depend on the Target Control Plane because it does not exist yet.
- **Target Control Plane:** synchronize chronyd from approved external NTP sources. Expose a real, reachable internal NTP service on the Ironic internal IP/VIP over UDP/123.
- **Every Target Worker:** configure chronyd with exactly one source: the Ironic internal IP/VIP. Do not add public pools, external servers or fallback sources. Verify reachability and that chronyd is actually synchronized to that exact endpoint before `kubeadm join`.
- A clock that merely appears synchronized is insufficient evidence. Record the configured source and chronyd's selected source.

The current Target Agent implements a fail-closed policy validator and config renderer for the Worker source. Host-level chronyd installation, restart and synchronization verification remain an implementation task and are not claimed as complete.

## 4. Network architecture

- Kubernetes Control Plane management uses ordinary NIC connectivity.
- The Kubernetes Data Plane is separate from the Control Plane network.
- Pod traffic uses the Data Plane. Multus provides multi-network attachment; SR-IOV CNI and DPDK provide the high-performance data path where required.
- The Agent control channel uses outbound HTTPS with mutual TLS and a narrowly scoped identity. It must be explicitly routed/reachable through the management network without exposing Kubernetes API credentials to the cloud runtime.
- Ironic's internal IP/VIP belongs to the provisioning/data-plane reachability design and is the sole NTP source advertised to Target Workers.

Network readiness must verify actual routes, VLANs, MTU, SR-IOV resource availability, Multus attachments and Ironic internal endpoint reachability; configuration presence alone is not proof.

## 5. Trust and persistence boundaries

- Cloud Control Agent stores durable deployment and command state in PostgreSQL. Agent-to-cloud communication uses mTLS; the client certificate Common Name is bound to the registered Agent UUID.
- EPH and Target have separate identities, deployment manifests, images and credentials.
- Target Agent replicas share one logical Agent identity but have unique Pod instance identities. Kubernetes Lease ownership gates command execution.
- Lease tokens are short-lived, never logged, and stored only as hashes by Cloud Control.
- Human approval is verified by the authenticated platform API before command submission. The Cloud Control API records the approval digest and trusts only the dedicated platform API mTLS identity for that boundary.
- Secrets, kubeconfigs, BMC credentials and TLS private keys are delivered through Kubernetes Secrets or an external secret manager, never committed to Git.

## 6. Implementation status

The new branch currently provides independent Cloud Control, EPH and Target process entrypoints; mTLS heartbeat/claim/result transport; durable PostgreSQL command storage; Kubernetes Lease leader election; read-only Kubernetes/CAPI/Metal3 observation; and Worker NTP policy validation.

CAPI Pivot execution, exact BMH registry migration, autonomous EPH cleanup, Worker provisioning, host-level chronyd application, full platform API/UI integration and physical BMC/Ironic acceptance remain incomplete. They must be delivered as new adapters and tested against the selected CAPI, Metal3, Ironic and operating-system versions. A green unit test or QEMU bootstrap test is not proof of physical bare-metal provisioning.
