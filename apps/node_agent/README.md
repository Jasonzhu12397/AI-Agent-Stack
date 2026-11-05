# Dual Node Agent Runtime

The EPH Agent and Target Agent use the same Python runtime with strict, role-specific command allowlists. They are independently enrolled nodes with separate credentials, heartbeats, command leases, and audit history.

## Agent roles

### EPH Agent (AGENT_NODE_TYPE=eph_node)

Runs on the temporary EPH management node. Its responsibilities are:
- Report host health and validate the temporary management environment.
- Inspect the source Kubernetes, Cluster API, Metal3 and Ironic inventory.
- Produce a validated deployment plan and source-side Pivot preflight.
- Execute an explicitly approved cluster.capi.pivot.execute command.
- Never declare ownership transferred merely because clusterctl move returned success.

### Target Agent (AGENT_NODE_TYPE=target_cluster)

Runs inside the target Kubernetes cluster. Its responsibilities are:
- Report target cluster health and Kubernetes identity.
- Validate the target CAPI/Metal3 APIs and controller readiness before handoff.
- After the source Pivot command completes, verify that the expected Cluster object exists and retains the source Cluster UID.
- Report Machine inventory and Ready counts to the control plane.
- Persist and restore deployment checkpoints across agent restarts.

The Target Agent cannot run source-side Pivot commands. The EPH Agent cannot run target adoption verification commands.

## Current handoff sequence

1. The EPH Agent completes source inventory and source management-plane preflight.
2. The Target Agent completes target API/controller preflight.
3. The control plane compares the five required provider API versions and fails closed on mismatch.
4. The control plane creates a source-side Pivot command in waiting_approval.
5. A different tenant administrator approves the immutable command payload hash.
6. The EPH Agent receives a 90-second command lease, renews it while executing, runs dry-run and then clusterctl move.
7. The control plane validates the source command result and queues cluster.capi.pivot.target.verify.
8. The Target Agent verifies the Cluster UID and Machine inventory. Only then is target_agent_owns_cluster recorded as true.

A Pivot timeout or expired running lease has an unknown outcome and is never automatically retried. Reconcile the source and target API inventories before creating another Pivot attempt.


## Dual-network inspection

Both Agent roles expose the read-only `cluster.network.inspect` command. It discovers Kubernetes node allocatable extended resources (including advertised SR-IOV resources and hugepages), Multus NetworkAttachmentDefinitions and plugin type names, and likely Calico/Multus/OVS/SR-IOV component Pods in `kube-system`. It also reports the Agent Pod's own network namespace interfaces and default routes.

When the Agent Deployment mounts host `/sys` read-only at `/host/sys`, the same command also reports physical NIC names/MAC/MTU/link state, PCI network devices and drivers, PF/VF counts, NUMA node assignments and hugepage totals/free counts. Without that mount the host hardware section is explicitly `not_configured`; the container sysfs is never mislabeled as host inventory. This requires a site-approved read-only `hostPath` policy exception; the Agent remains non-root, non-privileged, without host networking or host PID access.

The command deliberately does not claim that a host PF/VF is healthy, that a VF is attached to a workload Pod, or that DPDK is ready. The Pod's `/sys/class/net` and `/proc/net/route` are the Agent container network namespace, not the physical host namespace. A NetworkAttachmentDefinition is configuration evidence, not proof of dataplane connectivity. Host PCI, IOMMU/VFIO binding, hugepages per NUMA node, CPU pinning and actual application Pod route validation require a dedicated host/network adapter and environment-level integration tests.

A cluster plan may include a versioned `network_profile`:

```json
{
  "version": "v1",
  "pod_traffic_default": "data_plane",
  "control_plane": {
    "interface": "eno1",
    "cidr": "10.10.10.10/24",
    "gateway": "10.10.10.1",
    "mtu": 1500
  },
  "data_plane": {
    "interfaces": ["ens2f0"],
    "mtu": 9000,
    "cni": {
      "primary": "calico",
      "meta": "multus",
      "attachments": [
        {"name": "ovs-data", "type": "ovs"},
        {
          "name": "sriov-data",
          "type": "sriov",
          "resource_name": "intel.com/sriov_netdevice"
        }
      ]
    }
  }
}
```

The plan validator checks the declared network roles, CIDRs/gateway, explicitly supplied MTUs, Calico+Multus composition, unique attachment names, supported OVS/SR-IOV attachment types and SR-IOV extended resource names. It does not invent interface names or MTU values. DPDK declarations additionally require hugepages, an explicit NUMA policy and the Kubernetes CPU Manager `static` policy. It hashes the complete profile into the cluster plan. This is input validation only; it does not install CNI components, create NetworkAttachmentDefinitions, alter routes, or bind devices.

The RBAC template grants read-only access to node metadata, NetworkAttachmentDefinitions and Pods in `kube-system` only for component discovery. Extend workload namespace access only through separately reviewed namespace-scoped Roles and RoleBindings.
For workload attachment checks, dispatch `cluster.network.inspect` with `workload_namespaces` and `expected_attachments` (for example `{"workload_namespaces":["app-ns"],"expected_attachments":["app-ns/sriov-data"]}`). The Agent reads only Pods in the explicitly named namespaces and returns each Pod's Multus network-status interface/IP summary plus missing expected attachments. A namespace without an explicit read-only Pod Role/RoleBinding is reported as unavailable (for example HTTP 403); it is not silently treated as passing. The supplied RBAC grants Pod listing in `default` and `kube-system` only, so add narrowly scoped Role/RoleBinding pairs for each approved application namespace.

## Optional read-only Ironic API verification

The `cluster.metal3.observe` command can perform a direct Ironic node read when both `IRONIC_API_URL` and `IRONIC_API_TOKEN` are configured. The URL must be an HTTPS origin without a path, query, or fragment. The token is sent only in the `X-Auth-Token` request header and is never returned in command output. Optional settings are `IRONIC_API_CA_FILE`, `IRONIC_API_MICROVERSION`, and `IRONIC_API_TIMEOUT_SECONDS`. The adapter performs only `GET /v1/nodes/{uuid}`; it has no Ironic write or power-control operations.

The node UUID is read from the Metal3 BareMetalHost `status.provisioning.ID` field. The observer verifies the returned UUID, requires Ironic `provision_state=active`, `maintenance=false`, no pending target provision state, and no recorded last error. Missing UUIDs, API failures, mismatched identity, maintenance, and transitional states fail closed. BMHs marked `externally provisioned` are not represented as Ironic-verified nodes.

Store the endpoint and token in the `ai-agent-system/ironic-api-credentials` Secret. The deployment templates expose its `api-url` and `token` keys as optional environment variables, so the Agent remains functional when the Secret is absent. For example, create the Secret in each cluster where an Agent needs direct Ironic access:

```sh
kubectl -n ai-agent-system create secret generic ironic-api-credentials \
  --from-literal=api-url='https://ironic-api.example.net' \
  --from-literal=token='<short-lived-or-scoped-token>'
```

For a private CA, mount its certificate read-only and set `IRONIC_API_CA_FILE` to that mounted path. Do not disable TLS verification. Do not place credentials in a command payload, ConfigMap, source control, or logs. Direct Ironic verification remains `not_configured` when the URL and token are absent.

## Read-only CAPI to Metal3 lifecycle observation

The Target Agent exposes `cluster.metal3.observe`. It follows each Machine belonging to the requested Cluster through `spec.infrastructureRef` to the namespaced Metal3Machine, then resolves the BareMetalHost through the BMH `spec.consumerRef` including the Metal3Machine UID. Missing, cross-namespace, ambiguous, or stale ownership links fail closed. A BMH is considered provisioned only when it is online, its provisioning state is `provisioned` or `externally provisioned`, its operational status is absent or `OK`, and it has no reported error.

This command is read-only. It does not claim direct Ironic API health: unless a separately authenticated Ironic adapter is configured, the response explicitly reports `ironic.status=not_configured`. The Target Agent RBAC grants read-only access to Metal3Machine objects in the configured target namespace and BareMetalHosts only in the explicitly bound Metal3 namespaces.

Example command payload:

```json
{
  "cluster_name": "edge-lab",
  "namespace": "default",
  "baremetal_host_namespace": "metal3-system"
}
```

## Configuration

Set these environment variables from a protected secret source:
- AGENT_CONTROL_URL: control-plane origin, for example https://studio.example.com.
- AGENT_NODE_ID: UUID returned when this node was enrolled.
- AGENT_TOKEN: one-time enrollment token returned by the control plane; do not commit it.
- AGENT_NODE_TYPE: eph_node or target_cluster.
- APP_ENV=production enforces HTTPS.
- The EPH Agent must have source and target kubeconfig secret files mounted at the paths configured by CAPI_PIVOT_SOURCE_KUBECONFIG and CAPI_PIVOT_TARGET_KUBECONFIG.
- The Target Agent must have read-only access to the target Kubernetes API for adoption verification.

## Run

Run one heartbeat/poll/command cycle:

```bash
python -m apps.node_agent.agent --once
```

Run continuously:

```bash
python -m apps.node_agent.agent --poll-seconds 5
```

## Explicitly not enabled yet

This skeleton does not yet retire the EPH in-memory Kubernetes, reboot the physical EPH host, or join that host as a worker. It also does not perform real-cluster end-to-end verification. EPH reclamation must remain disabled until the target Agent has verified adoption and a separate, audited host-reclaim adapter has been implemented and tested.

The agent does not invoke a shell, execute arbitrary Python, or accept credentials/manifests from command payloads. Production deployment should replace development bearer credentials with mTLS or short-lived workload identity and run each agent under a dedicated OS user or Kubernetes ServiceAccount.