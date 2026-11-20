# Control-stack offline manifests

Vendored multi-document release manifests for the EPH bootstrap cluster. The
EPH Agent applies them in `apps/eph_agent/control_stack.py` install order; file
names must equal the component names in that plan.

| Component | File | Version | SHA256 | Namespace / controller |
| --- | --- | --- | --- | --- |
| cert-manager | `cert-manager.yaml` | v1.16.3 | `5dda0f9c9dbed90825a0073f1c0f986c8a49e9b6c97a7701c87733afadc734bf` | cert-manager / cert-manager |
| Metal3 IPAM | `ipam.yaml` | v1.9.6 | `854609a7a90e8ab83a13a1d332c977985e42fe9304f4da0ad1896e89240ab55d` | capm3-system / ipam-controller-manager |
| Cluster API core | `capi-core.yaml` | v1.14.4 | `0030264eb0a0d90ebacc9da8f6bbb717bee32e45ffadc4a74784fb5ad7b9bb0b` | capi-system / capi-controller-manager |
| Kubeadm bootstrap | `capi-bootstrap.yaml` | v1.14.4 | `cf805775f24d197581b06e938bcd0eb9a9ed49d675f67e0f689ce7b71e5e397c` | capi-kubeadm-bootstrap-system / capi-kubeadm-bootstrap-controller-manager |
| Kubeadm control plane | `capi-control-plane.yaml` | v1.14.4 | `7fecb41843dcff7e05ab539dbe497c050a9aa7c888d4fb68be12d3c1805be438` | capi-kubeadm-control-plane-system / capi-kubeadm-control-plane-controller-manager |
| BareMetal Operator | `baremetal-operator.yaml` | v0.14.3 | `02e04382640c5cfa3bdb74d924aa3ba5baf3c5c60f1b9f4fae26df1c72e6afc7` | baremetal-operator-system / baremetal-operator-controller-manager |
| Ironic Standalone Operator | `ironic.yaml` | v0.12.0 | `c3c60c4d1cd37ba8d8f0874ffb520d4a64b17f57923b16d578b8399c3d28c67c` | ironic-standalone-operator-system / ironic-standalone-operator-controller-manager |
| CAPM3 | `capm3.yaml` | v1.14.3 | `e7758abda9c60eb02609743a2ffd99d06e64316b36b7f4953c48505af683b667` | capm3-system / capm3-controller-manager |

Sources:

- cert-manager: `https://github.com/cert-manager/cert-manager/releases/download/v1.16.3/cert-manager.yaml`
- Metal3 IPAM: `https://github.com/metal3-io/ip-address-manager/releases/download/v1.9.6/ipam-components.yaml`
- Cluster API: `https://github.com/kubernetes-sigs/cluster-api/releases/download/v1.14.4/{core,bootstrap,control-plane}-components.yaml`
- BareMetal Operator: `https://github.com/metal3-io/baremetal-operator/releases/download/v0.14.3/baremetal-operator.yaml`
- Ironic Standalone Operator: `https://github.com/metal3-io/ironic-standalone-operator/releases/download/v0.12.0/install.yaml`
- CAPM3: `https://github.com/metal3-io/cluster-api-provider-metal3/releases/download/v1.14.3/infrastructure-components.yaml`

All manifests are Apache-2.0. `quay.io/metal3-io/ip-address-manager:v1.9.6` is
the IPAM controller image; the other images are pinned inside their manifests.

## Local patches

`ipam.yaml`: the upstream release ships `${VAR:=default}` placeholders in the
controller args, which Kubernetes does not expand. They are replaced with their
defaults (`:8443`, `false`, `VersionTLS13`); without this the manager exits at
startup and the IPPool admission webhook never serves.

The other files are unmodified upstream release artifacts.

## Not included

`ironic.yaml` installs the operator only. The `Ironic` workload CR is
environment specific (Ironic and ramdisk images, provisioning interface, IP
address, credentials), so the EPH Agent requires it in the command payload
instead of guessing values here. Because the control stack readiness probe waits
for the `Ironic` CR `Ready` condition, a deployment without that CR stays
blocked rather than reporting a false success.

## Verification

`tests/test_eph_offline_control_stack_manifests.py` checks that every planned
component has a file, every file parses through the real loader, every readiness
probe names a Deployment that exists in the corresponding manifest, and the
Ironic CR probe matches the CRD group/version shipped in `ironic.yaml`.
