# Metal3 physical acceptance

`tools/metal3_acceptance.py` is a **read-only evidence collector**. It does not
power-cycle BMCs, create BMHs, apply manifests or change Ironic state. A passing
run proves only that the supplied checks passed on the supplied environment.

## Stages

1. `preflight` reads the platform `AgentMachine` projection and checks planning
   input: BMC address, boot NIC MAC, credential reference, CPU, RAM, duplicate
   addresses and skipped machines.
2. `redfish` resolves BMC credentials and reads
   `/redfish/v1/Systems/1` plus `EthernetInterfaces`, then compares the platform
   boot MAC with the BMC-reported MAC.
3. `cluster` reads BMH, CAPI Cluster, Machine, KubeadmControlPlane and
   MachineDeployment objects. With a target kubeconfig it also checks Nodes Ready,
   the `calico-node` `FELIX_BPFENABLED=true` setting and the absence of
   `kube-proxy`.
4. `verify` combines structural and cluster checks, optionally adding Redfish.

## One-command release run

`tools/metal3_release_run.py` is the automatic path. It executes preflight,
Redfish, cluster and aggregate verification in order, writes each stage JSON and
log, then seals `release-manifest.json`. No stage is run or merged by hand.

```bash
python tools/metal3_release_run.py \
  --project <project-id> \
  --kubeconfig /secure/eph.kubeconfig \
  --target-kubeconfig /secure/target.kubeconfig \
  --control-plane-replicas 3 --worker-replicas 4 \
  --evidence-dir evidence \
  --out evidence/release-manifest.json
```

If a stage exits non-zero, its evidence is still collected, but the manifest is
forced to `fail`; the command exits `1`. `--skip-redfish` omits the BMC stage and
removes it from the required kinds.

## Individual stages

These commands remain available for debugging only:

```bash
python tools/metal3_acceptance.py preflight --project <project-id> \
  --out evidence/01-preflight.json

python tools/metal3_acceptance.py redfish --project <project-id> \
  --out evidence/02-redfish.json

python tools/metal3_acceptance.py cluster \
  --kubeconfig /secure/eph.kubeconfig \
  --target-kubeconfig /secure/target.kubeconfig \
  --control-plane-replicas 3 --worker-replicas 4 \
  --out evidence/03-cluster.json

python tools/metal3_acceptance.py verify --project <project-id> \
  --kubeconfig /secure/eph.kubeconfig \
  --target-kubeconfig /secure/target.kubeconfig \
  --control-plane-replicas 3 --worker-replicas 4 \
  --redfish --out evidence/04-acceptance.json
```

Every command prints JSON and exits `1` when any check is `fail`. Evidence files
contain machine names, MAC addresses and state, never BMC usernames or passwords.

## Release gate

`tools/metal3_release_run.py` calls the same release gate automatically.
`tools/metal3_release_evidence.py` remains available to re-seal an existing
evidence directory; it turns the individual files into a fail-closed release
manifest. It requires `metal3-preflight`,
`metal3-redfish-evidence` and `metal3-cluster-evidence` by default, rejects any
evidence whose `status` is `fail` or whose checks contain a failure, and refuses
to pass when a required kind is missing. The manifest records each file's
SHA-256, size, evidence kind and timestamp plus the git commit, so the release
can be audited later without trusting mutable files on disk.

```bash
python tools/metal3_release_evidence.py \
  --evidence-dir evidence \
  --out evidence/release-manifest.json
```

Add `--require metal3-acceptance` to also require the aggregate verification
report. The command exits `1` whenever the manifest is not `pass`; attach the
manifest, not just the raw evidence, to the release record.

## What still requires real hardware

The repository unit tests mock Redfish and kubectl responses. They do not prove:

- BMC reachability, Redfish credentials or certificate trust in the lab.
- Ironic inspection, cleaning, IPA boot, BMH provisioning or power control.
- CAPI Pivot, BMH registry migration or EPH cleanup.
- Target control-plane readiness, worker join or Calico eBPF data-plane health.
- SR-IOV, DPDK or multi-NUMA scheduling.

Store the evidence JSON with the change record for the deployment, then attach
the acceptance result to the release. Do not mark a deployment complete from mock
tests.
