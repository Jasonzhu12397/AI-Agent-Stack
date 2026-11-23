# Control-stack offline manifests

Vendored multi-document release manifests for the EPH bootstrap cluster. The
EPH Agent applies them in `apps/eph_agent/control_stack.py` install order; file
names must equal the component names in that plan.

| Component | File | Version | SHA256 | Namespace / controller |
| --- | --- | --- | --- | --- |
| cert-manager | `cert-manager.yaml` | v1.16.3 | `5dda0f9c9dbed90825a0073f1c0f986c8a49e9b6c97a7701c87733afadc734bf` | cert-manager / cert-manager |
| Metal3 IPAM | `ipam.yaml` | v1.14.3 | `c693215a94810ff935c303adbcd14f8535aec3653aa7cc32a6931f44b78f0c39` | metal3-ipam-system / ipam-controller-manager |
| Cluster API core | `capi-core.yaml` | v1.14.4 | `0030264eb0a0d90ebacc9da8f6bbb717bee32e45ffadc4a74784fb5ad7b9bb0b` | capi-system / capi-controller-manager |
| Kubeadm bootstrap | `capi-bootstrap.yaml` | v1.14.4 | `cf805775f24d197581b06e938bcd0eb9a9ed49d675f67e0f689ce7b71e5e397c` | capi-kubeadm-bootstrap-system / capi-kubeadm-bootstrap-controller-manager |
| Kubeadm control plane | `capi-control-plane.yaml` | v1.14.4 | `7fecb41843dcff7e05ab539dbe497c050a9aa7c888d4fb68be12d3c1805be438` | capi-kubeadm-control-plane-system / capi-kubeadm-control-plane-controller-manager |
| BareMetal Operator | `baremetal-operator.yaml` | v0.14.3 | `02e04382640c5cfa3bdb74d924aa3ba5baf3c5c60f1b9f4fae26df1c72e6afc7` | baremetal-operator-system / baremetal-operator-controller-manager |
| Ironic Standalone Operator | `ironic.yaml` | v0.12.0 | `c3c60c4d1cd37ba8d8f0874ffb520d4a64b17f57923b16d578b8399c3d28c67c` | ironic-standalone-operator-system / ironic-standalone-operator-controller-manager |
| CAPM3 | `capm3.yaml` | v1.14.3 | `e7758abda9c60eb02609743a2ffd99d06e64316b36b7f4953c48505af683b667` | capm3-system / capm3-controller-manager |

Sources:

- cert-manager: `https://github.com/cert-manager/cert-manager/releases/download/v1.16.3/cert-manager.yaml`
- Metal3 IPAM: `https://github.com/metal3-io/ip-address-manager/releases/download/v1.14.3/ipam-components.yaml`
- Cluster API: `https://github.com/kubernetes-sigs/cluster-api/releases/download/v1.14.4/{core,bootstrap,control-plane}-components.yaml`
- BareMetal Operator: `https://github.com/metal3-io/baremetal-operator/releases/download/v0.14.3/baremetal-operator.yaml`
- Ironic Standalone Operator: `https://github.com/metal3-io/ironic-standalone-operator/releases/download/v0.12.0/install.yaml`
- CAPM3: `https://github.com/metal3-io/cluster-api-provider-metal3/releases/download/v1.14.3/infrastructure-components.yaml`

All manifests are Apache-2.0 and byte-identical to their published release
artifacts; the SHA256 column is the upstream checksum and
`tests/test_control_stack_vendor_guard.py` fails when a file stops matching it.
Images are pinned inside their manifests; Metal3 IPAM v1.14.3 pins
`quay.io/metal3-io/ip-address-manager` by digest.

IPAM tracks the CAPM3 release train: CAPM3 v1.14.3 requires the IPPool schema of
v1.14.3, which adds `allocationStrategy`. Vendoring IPAM v1.9.6 silently dropped
that field: an IPPool that sets it would have been pruned by the API server
while CAPM3 believed the strategy was applied.

## Local patches

None. The files are unmodified upstream artifacts, including the kustomize
placeholders several of them carry (`capi-core.yaml`, `capi-bootstrap.yaml`,
`capi-control-plane.yaml`, `capm3.yaml`, `ipam.yaml`).

## Variable expansion (not a patch)

Upstream publishes these manifests as kustomize output *before* variable
substitution, so controller args contain text such as
`--diagnostics-address=${CAPI_DIAGNOSTICS_ADDRESS:=:8443}` and
`--feature-gates=MachinePool=${EXP_MACHINE_POOL:=true}`. Kubernetes does not
expand `${VAR:=default}`: a plain `kubectl apply` hands the literal text to the
container and the controller fails to parse its own flags. Editing the files
would break their published checksums, so expansion happens at install time in
`apps/eph_agent/control_stack_manifests.py` through
`packages/agent_contracts/kustomize_vars.py`:

- `${NAME:=default}` becomes the default, or an override when one is supplied;
- `${NAME}` without a default is refused (`KUSTOMIZE_VAR_UNEXPANDED`) instead of
  being applied as literal text.

`tests/test_eph_agent_control_stack_manifests.py` asserts that every deployment
arg the loader returns is fully expanded, and
`tests/integration/kind_metal3_crds.py` server-side dry-runs the expanded
documents rather than the raw files.

The other files are unmodified upstream release artifacts and declare their own
namespaces as the first document.

## Not included

`ironic.yaml` installs the operator only. The `Ironic` workload CR is
environment specific (Ironic and ramdisk images, provisioning interface, IP
address, credentials), so the EPH Agent renders it from `spec.ironic` in the
command payload instead of guessing values here. When neither the payload nor
the supplied manifests contain the CR, the action fails fast with
`IRONIC_WORKLOAD_CR_REQUIRED` rather than waiting for
`CONTROL_STACK_READY_TIMEOUT:ironic`.

Payload contract (all values must come from the deployment, no defaults are
invented for required keys):

```yaml
ironic:
  api_credentials_name: ironic-auth        # required
  version: "37.0"                          # required
  namespace: baremetal-operator-system     # optional, must stay this value
  images:                                  # required
    ironic: registry.local/ironic:37.0
    deploy_ramdisk_downloader: registry.local/ipa-downloader:latest
    keepalived: registry.local/keepalived:2.2.4
    deploy_ramdisk_branch: latest          # optional
  networking:
    interface: eno2                        # required
    ip_address: 10.130.60.84               # required
    image_server_port: 8080                # optional pass-through
    disable_host_network: true             # optional pass-through
```

`packages/agent_contracts/ironic_spec.py` owns the validation rules (shared with
the platform API), and `apps/eph_agent/ironic_cr.py` wraps the normalised spec
into the CR
(`ironic.metal3.io/v1alpha1`, name `ironic`). After
`eph_single_node_ready` the EPH applies the operator, waits for
`crd/ironics.ironic.metal3.io` to report `established`, and only then applies
the CR; a CR sharing one apply with its CRD would fail API discovery. The
readiness probe then observes the workload's real `Ready` condition. Supplying
both `spec.ironic` and an `Ironic` document in the manifest payload is rejected
as `IRONIC_CR_MULTIPLE_SOURCES`.

## Verification

`tests/test_eph_offline_control_stack_manifests.py` checks that every planned
component has a file, every file parses through the real loader, every readiness
probe names a Deployment that exists in the corresponding manifest, and the
Ironic CR probe matches the CRD group/version shipped in `ironic.yaml`.
`tests/test_eph_ironic_cr_render.py` pins the rendered CR shape, and
`tests/integration/kind_metal3_crds.py` installs the real `Ironic` CRD and
server-side dry-runs a platform-rendered CR, so a schema drift fails CI.
