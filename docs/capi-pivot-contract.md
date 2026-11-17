# EPH CAPI Pivot contract

This contract is implemented by the new `apps/eph_agent/capi_pivot.py` adapter. It does not import or reuse the deprecated Node Agent package.

## Actions

- `eph.capi.pivot.dry_run`: performs read-only source/target checks and invokes `clusterctl move --dry-run`.
- `eph.capi.pivot`: performs the same preflight, requires an approval digest bound to the exact plan, annotates the source Cluster identity, runs `clusterctl move`, and verifies postconditions.

The actual move is permitted only when the command's `approval_digest` equals `payload.plan_digest`. The platform API must compute the digest over the complete approved plan, including the source Cluster UID, stable logical cluster identity, exact BMH identity set, provider deployment references/versions, Target controller deployment references, namespace and timeout. The Cloud Control API only records the trusted platform API's digest; it does not independently authenticate the human approver.

## Required payload

- `namespace`, `cluster_name`: Kubernetes DNS-label names. The source namespace must contain exactly one CAPI Cluster to avoid moving unrelated clusters.
- `stable_cluster_identity`: stable logical identifier, distinct from Kubernetes object UIDs.
- `expected_source_cluster_uid`: source CAPI Cluster object's Kubernetes UID observed during preflight.
- `expected_source_bmh_identities`: non-empty list of `namespace`, `name` and `hardware_identity`. Hardware identity is derived from system UUID, system serial number, or the BMC address plus boot MAC. The live source inventory must exactly match this approved set before the move.
- `provider_deployments`: for every required provider (CAPI core, kubeadm bootstrap/control-plane, Metal3, Bare Metal Operator and Ironic as deployed in the selected environment), provide source and Target Deployment namespace/name references. The adapter verifies source and Target controller readiness and compares version tags or `app.kubernetes.io/version` / `ai-agent-stack.io/provider-version` metadata. Target versions must be equal to or newer than source versions.
- `target_controller_deployments`: every Target provider Deployment that must be ready before Pivot, with namespace, name and minimum ready replica count. Each must tolerate the Control Plane master taint and its Ready Pods must actually be scheduled on Target Control Plane masters. This is mandatory because the Target management cluster may have no Workers yet.
- `plan_digest`: must exactly match the command's approval digest for the mutating action.
- `move_timeout_seconds`: optional integer from 60 through 3600; defaults to 1800.

The EPH Agent uses separate source and Target kubeconfig files mounted as read-only Secrets. The Target kubeconfig Secret is optional during initial EPH bootstrap and must be provisioned only after the Target API is ready.

## Safety and postflight

Before Pivot, the adapter verifies that source and Target Kubernetes APIs are distinct, at least three Target Control Plane masters are Ready, the required Target CAPI/Metal3/Ironic controllers are Ready on those masters, provider versions are compatible, and the source Cluster/BMH inventory still matches the approved plan.

Before the move, it writes these annotations to the source CAPI Cluster:

- `ai-agent-stack.io/source-cluster-uid`
- `ai-agent-stack.io/stable-cluster-identity`

The Target Kubernetes API assigns a new metadata UID to the recreated object. The source and Target UIDs are recorded separately; equality is not expected. The source UID annotation and stable logical identity must survive the move.

After `clusterctl move`, the adapter verifies source Cluster absence, Target Cluster presence, identity annotations, Target controller readiness and exact equality of source/Target BMH hardware-identity sets. It waits for Target BMH hardware identity reconciliation for up to five minutes. Any mismatch blocks EPH cleanup.

A failed or interrupted move may leave source and Target objects in an ambiguous state. The adapter fails closed and does not delete EPH. A human/operator reconciliation is required before retrying ambiguous partial moves.

## Runtime boundaries

- The command lease is renewed while `clusterctl move` runs. Lease loss terminates the subprocess and returns an ambiguous/failed result; it never triggers EPH cleanup.
- The subprocess receives only a minimal environment and the intended kubeconfig path; Cloud Control mTLS private keys and Agent tokens are not inherited.
- Commands are passed as argument arrays; no shell is used.
- The adapter is unit-tested with mocked Kubernetes/clusterctl responses. A live provider-version compatibility run and physical BMC/Ironic acceptance are still required before production use.
