# Dual Node Agent Runtime

The EPH Agent and Target Agent use the same isolated Kubernetes-side runtime image, with strict role-specific command allowlists. They are independently enrolled nodes with separate credentials, heartbeats, command leases and audit history. This runtime is not the cloud AI Agent.

## Runtime boundary

- Cloud AI Agent: runs in the cloud deployment from `Dockerfile.cloud-agent`, starts `worker_app.main`, and owns model execution, tool routing, RAG, workflow orchestration, approvals and durable task state.
- Kubernetes Node Agent: runs from `Dockerfile.node-agent`, starts `apps.node_agent.agent`, and contains only cluster-side inspection and execution adapters plus PyYAML, kubectl and clusterctl.
- Studio API: runs from `Dockerfile`, exposes user-facing APIs and the versioned Agent command/heartbeat API. It is a separate process and image from both Agent runtimes.

The Node Agent image does not contain Django, the cloud worker package, the Studio API package, model-provider SDKs, PostgreSQL/Redis/Qdrant clients or cloud LLM credentials. It has no general-purpose shell command endpoint. The cloud Agent does not mount kubeconfigs, host sysfs, BMC credentials or Kubernetes ServiceAccount tokens.

## Agent roles

### EPH Agent (AGENT_NODE_TYPE=eph_node)

Runs as a Pod in the temporary EPH single-node Kubernetes cluster on the reserved physical host. Its responsibilities are:
- Report host health and validate the temporary management environment.
- Inspect source Kubernetes, Cluster API, Metal3 and Ironic inventory.
- Produce a validated deployment plan and source-side Pivot preflight.
- Execute an explicitly approved `cluster.capi.pivot.execute` simulation after read-only source/target preflight.
- Never transfer ownership from the EPH Agent; a successful simulation is not a real Pivot.

### Target Agent (AGENT_NODE_TYPE=target_cluster)

Runs as a Pod in the target Kubernetes cluster after its API and control plane are healthy. Its responsibilities are:
- Report target cluster health and Kubernetes identity.
- Validate target CAPI/Metal3 APIs and controller readiness before handoff.
- Verify expected Cluster identity and UID against an independently executed test fixture or controlled integration cluster.
- Report Machine inventory and Ready counts to the control plane.
- Persist and restore deployment checkpoints across agent restarts.

The Target Agent cannot run source-side Pivot commands. The EPH Agent cannot run target adoption verification commands.

## Build and deploy

Build the Node Agent image using `deploy/agents/build-node-agent.sh` with explicit version and checksum inputs. Deploy EPH and Target Pods with `deploy/agents/deploy.sh`; provide separate EPH and target Kubernetes contexts, per-agent credentials and an immutable image digest. The deployment script performs server-side dry-run validation before applying either workload.

## Safety

The current `cluster.capi.pivot.execute` command is simulation-only. It never invokes mutating `clusterctl move`, never writes Kubernetes resources, and never changes Agent ownership. EPH reclaim remains fail-closed. The node image includes kubectl and clusterctl only for the bounded, typed operations implemented by this runtime; arbitrary shell, arbitrary manifest apply and arbitrary command payloads are not supported.

## Dual-network inspection

The `cluster.network.inspect` command reads Kubernetes node allocatable extended resources, Multus NetworkAttachmentDefinitions and selected CNI component Pods. If the Deployment mounts host `/sys` read-only at `/host/sys`, it also reports physical NIC/PCI/VF/NUMA/hugepage inventory. The mount is read-only; the Agent remains non-root, non-privileged and without host networking or host PID access.

Network interface names, VLANs, CIDRs, gateways, MTUs, SR-IOV resource names and DPDK settings are deployment inputs. The Agent validates declared profiles but does not invent values, install CNI components, change routes or bind devices. A NetworkAttachmentDefinition is configuration evidence, not proof of dataplane connectivity.
