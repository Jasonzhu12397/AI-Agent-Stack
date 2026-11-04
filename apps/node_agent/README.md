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