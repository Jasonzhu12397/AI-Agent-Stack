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

## Commands

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
