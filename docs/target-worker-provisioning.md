# Target Agent Worker Provisioning

## Ownership

The Target Agent is the only Agent allowed to create the target cluster's CAPI worker pool. Cloud Control submits a typed `target.worker.provision` command and records its result; it does not approve or perform the infrastructure mutation. The Target Agent must hold the target-cluster leader Lease and a valid command lease while executing.

## Resource bundle

The command payload contains:

- `namespace`: the target CAPI namespace.
- `desired_worker_count`: the exact desired worker replica count (1-500).
- `ironic_internal_ntp_ip`: the configured Ironic internal IP/VIP. It must exactly match the Target Agent's `IRONIC_INTERNAL_NTP_IP`.
- `timeout_seconds`: bounded rollout timeout (30-7200 seconds; default 1800).
- `resources`: exactly three provider-versioned Kubernetes objects:
  - `KubeadmConfigTemplate` (`bootstrap.cluster.x-k8s.io/v1beta1`)
  - `Metal3MachineTemplate` (`infrastructure.cluster.x-k8s.io/v1beta1`)
  - `MachineDeployment` (`cluster.x-k8s.io/v1beta1`)

The MachineDeployment must reference the exact bootstrap and infrastructure template names, API versions and namespace. Its selector must be a non-empty subset of the MachineDeployment template labels, and its replica count must equal `desired_worker_count`.

The Metal3MachineTemplate is provider-specific and must be generated from the deployment's approved image, BMC, network, user-data and hardware-selection configuration. The executor deliberately does not invent those values.

## Mutation sequence

1. Validate all object identities, API versions, namespace, references, labels, replica count, NTP endpoint and timeout before any Kubernetes mutation.
2. Generate a chrony pre-kubeadm command using only the pinned Ironic internal IP/VIP. Reject pre-existing `server` or `pool` directives in the supplied bootstrap commands.
3. Create the KubeadmConfigTemplate and Metal3MachineTemplate only when absent. Existing templates are immutable from this executor: an existing object with a different spec is rejected and must be replaced by a new versioned template name.
4. Apply the MachineDeployment using Kubernetes server-side apply and the `target-agent` field manager.
5. Poll the MachineDeployment, CAPI Machines and Kubernetes Nodes until the desired, updated, ready and available replica counts match; every Machine has `bootstrapReady=True` and a Node reference; and every referenced Node is Ready.
6. The generated bootstrap command fails unless chronyd reports the Ironic internal endpoint selected and tracking reports a normal leap status. A failed NTP bootstrap therefore prevents CAPI Machine bootstrap readiness and blocks the worker-ready checkpoint.

The executor checks the command lease during rollout. It stops polling and refuses to report success after lease loss. A timeout is reported with the last observed MachineDeployment/Machine/Node state; it does not mark the deployment complete.

## Handoff verification

The `workers_ready` handoff phase independently re-reads the MachineDeployment, its KubeadmConfigTemplate, selected CAPI Machines and referenced Kubernetes Nodes. It does not accept caller-supplied ready counts. It verifies that the bootstrap template still contains the pinned internal NTP command and requires the CAPI bootstrap and Node readiness conditions before constructing per-worker NTP evidence.

## RBAC

The Target Agent needs read access to Nodes, Machines and MachineDeployments; create/get access to versioned KubeadmConfigTemplates and Metal3MachineTemplates; and create/patch/get/list/watch access to MachineDeployments. The leader Lease is pre-created by the deployment manifest, so the Agent only needs get/update on that named Lease.

Worker-resource write verbs are granted through a namespace-scoped Role and RoleBinding, not the cluster-wide observer ClusterRole. The deploy script renders `TARGET_CAPI_NAMESPACE` (default `metal3`) into both RBAC objects and the Target Agent environment; the executor rejects any command targeting a different namespace. Set `TARGET_CAPI_NAMESPACE` to the actual target CAPI namespace when deploying. Do not grant delete, Secret, BMH or Ironic write permissions as part of Worker provisioning.

## Verification boundary

Unit tests cover payload validation, immutable-template conflict handling, lease loss, worker readiness observation, NTP bootstrap evidence and RBAC. A passing unit test does not prove provider compatibility or physical BMC/Ironic behavior. Validate the exact CAPI, CABPK, Metal3 provider and Ironic versions in an integration cluster before enabling production worker provisioning.
