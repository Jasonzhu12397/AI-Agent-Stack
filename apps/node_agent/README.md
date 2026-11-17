# EPH and Target Agent Runtime

The EPH Agent and Target Agent are the only site-side Agent roles. They share an isolated runtime implementation and image, with strict role-specific command allowlists. EPH is enrolled as the temporary source-side Agent. Target Agent is a cluster-scoped service deployed as Pods in the target Kubernetes control plane; it is not a per-host Node Agent. This runtime is separate from the cloud Control Agent.

## Runtime boundary

- Cloud AI Agent: runs in the cloud deployment from `Dockerfile.cloud-agent`, starts `worker_app.main`, and owns model execution, tool routing, RAG, workflow orchestration, approvals and durable task state.
- Shared EPH/Target runtime implementation: currently packaged under the legacy internal path `apps.node_agent` and built from `Dockerfile.node-agent`. This package name is an implementation detail, not a third Agent role.
- Studio API: runs from `Dockerfile`, exposes user-facing APIs and the versioned Agent command/heartbeat API. It is a separate process and image from both Agent runtimes.

The shared Agent runtime image does not contain Django, the cloud worker package, the Studio API package, model-provider SDKs, PostgreSQL/Redis/Qdrant clients or cloud LLM credentials. It has no general-purpose shell command endpoint. The cloud Agent does not mount kubeconfigs, host sysfs, BMC credentials or Kubernetes ServiceAccount tokens.

Kubernetes API requests from the shared Agent runtime validate the in-cluster service endpoint, port and ServiceAccount token before use. Both read-only requests and server-side dry-run MachineDeployment validation reject HTTP redirects so the ServiceAccount bearer token is never forwarded to another origin. Configure the in-cluster API endpoint directly; redirects are treated as an error.

## Agent roles

### EPH Agent (AGENT_NODE_TYPE=eph_node)

Runs as a Pod in the temporary EPH single-node Kubernetes cluster on the reserved physical host. Its responsibilities are:
- Report host health and validate the temporary management environment.
- Inspect source Kubernetes, Cluster API, Metal3 and Ironic inventory.
- Produce a validated deployment plan and source-side Pivot preflight.
- Execute an explicitly approved `cluster.capi.pivot.execute` command. With an operation ID and the exact approved payload, the EPH Agent performs the real `clusterctl move` path after rechecking the source inventory, target management-plane readiness and Metal3 handoff.
- Report the move result without declaring Target ownership complete. The Target Agent must independently verify the adopted CAPI inventory and run Metal3/Ironic postflight before the deployment can advance.

### Target Agent (AGENT_NODE_TYPE=target_cluster)

Runs as a Pod in the target Kubernetes cluster after its API and control plane are healthy. Its responsibilities are:
- Report target cluster health and Kubernetes identity.
- Validate target CAPI/Metal3 APIs and controller readiness before handoff.
- Verify expected Cluster identity and UID against an independently executed test fixture or controlled integration cluster.
- Report Machine inventory and Ready counts to the control plane.
- When a MachineDeployment name is supplied to `cluster.postflight`, verify its UID, generation convergence, desired/updated/ready/available replica counts and unavailable replicas before reporting worker inventory complete.
- Persist and restore deployment checkpoints across agent restarts.

The Target Agent cannot run source-side Pivot commands. The EPH Agent cannot run target adoption verification commands. There is no separate Node Agent role.

## Build and deploy

Build the shared EPH/Target Agent runtime image using `deploy/agents/build-node-agent.sh` with explicit version and checksum inputs. Deploy EPH and Target Pods with `deploy/agents/deploy.sh`; provide separate EPH and target Kubernetes contexts, per-agent credentials and an immutable image digest. The deployment script performs server-side dry-run validation before applying either workload.

## Safety

The `cluster.capi.pivot.execute` command has a real mutating path: it invokes `clusterctl move` only when the cloud control plane dispatches the exact operation-bound command after independent approval and Target Agent acknowledgement. The EPH Agent rechecks the approved source inventory digest and target/Metal3 preflight immediately before mutation. A successful command result is not proof of Target adoption and does not enable EPH reclaim. The Target Agent must independently verify the target Cluster identity, adopted inventory, controller readiness and applicable Metal3/Ironic postflight. Ambiguous or interrupted execution requires reconciliation; it must not be replayed automatically. EPH reclaim remains fail-closed until the separate Target adoption and hardware verification gates complete. The shared runtime image includes kubectl and clusterctl only for the bounded, typed operations implemented by this runtime; arbitrary shell, arbitrary manifest apply and arbitrary command payloads are not supported.

## Dual-network inspection

The `cluster.network.inspect` command reads Kubernetes node allocatable extended resources, Multus NetworkAttachmentDefinitions and selected CNI component Pods. If the Deployment mounts host `/sys` read-only at `/host/sys`, it also reports physical NIC/PCI/VF/NUMA/hugepage inventory. The mount is read-only; the Agent remains non-root, non-privileged and without host networking or host PID access.

Network interface names, VLANs, CIDRs, gateways, MTUs, SR-IOV resource names and DPDK settings are deployment inputs. The Agent validates declared profiles but does not invent values, install CNI components, change routes or bind devices. A NetworkAttachmentDefinition is configuration evidence, not proof of dataplane connectivity.


## Target Agent high-availability topology

The intended production topology is three Target Agent Pods, one scheduled on each of the three target Kubernetes control-plane/master nodes. They represent one logical Target Agent service for the target cluster, not three independent per-node agents. Before scaling the current single-replica manifest, implement Kubernetes Lease-based leader election (or an equivalent server-side single-active poller) so only one replica can poll and execute commands at a time; standby replicas must not submit duplicate heartbeats, claim commands or execute side effects. Use pod anti-affinity or required topology spread across the three control-plane nodes, tolerate the control-plane/master NoSchedule taints, and verify all three replicas are Ready. The current manifest is still single-replica and does not yet implement this HA/leader-election contract; do not treat it as satisfying the three-master requirement.