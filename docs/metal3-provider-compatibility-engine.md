# Metal3 Provider Compatibility Engine

## Purpose

The node agent exposes `cluster.provider.compatibility` as a read-only compatibility gate. It evaluates an explicitly supplied set of deployed component image versions against the Metal3 community version-support page and periodic integration-test matrix. It also reads the live Kubernetes server version and preferred CAPI API version from the connected cluster.

The command is available to the EPH agent only. It does not install providers, change CRDs, create Kubernetes resources, contact BMC endpoints, or authorize provisioning.

## Community evidence snapshot

Snapshot date: 2026-11-05.

| Component | Supported releases | Tested release lines | EOL release lines |
|---|---|---|---|
| CAPM3 | 1.14, 1.13 | 1.12 | — |
| BMO | 0.14, 0.13 | 0.12 | — |
| Ironic image | 39.0, 38.0, 37.0 | 35.0 | 33.0 |
| Ironic Standalone Operator | 0.12, 0.11 | 0.10 | 0.9 |

IrSO-to-Ironic release compatibility is checked independently:
- IrSO 0.12: Ironic 39.0, 38.0, 37.0.
- IrSO 0.11: Ironic 38.0, 37.0, 35.0.
- IrSO 0.10: Ironic 37.0, 35.0, 34.0.
- IrSO 0.9: Ironic 35.0, 34.0, 33.0.

The upstream periodic CI matrix explicitly lists these CAPM3/BMO/Ironic profiles:
- CAPM3 1.14 + BMO 0.14 + Ironic 37.0.
- CAPM3 1.13 + BMO 0.13 + Ironic 35.0.
- CAPM3 1.12 + BMO 0.12 + Ironic 33.0.

CAPM3 1.14 and 1.13 use CAPI API v1beta2; CAPM3 1.12 uses v1beta1 in the current version-support table. A live CAPI API mismatch is a hard blocker.

## Command contract

Command type: `cluster.provider.compatibility`

Example payload:

```json
{
  "provider_versions": {
    "capm3": "1.14.0",
    "bmo": "0.14.0",
    "ironic": "37.0.0",
    "irso": "0.12.0"
  }
}
```

The agent obtains Kubernetes `/version` and `/apis` itself. The caller cannot override the live Kubernetes or CAPI API versions. Component versions must come from the deployed image tags or immutable deployment inventory collected by the control plane; do not copy versions from a desired plan and describe them as observed runtime versions.

Output distinguishes:
- `supported`: every component release is listed as supported and the IrSO/Ironic and known CAPI API constraints match.
- `tested`: no hard incompatibility was found, but at least one component is in a tested (not current supported) release line.
- `unsupported`: a component release is unknown, EOL, or unsupported; the IrSO/Ironic pair is invalid; or the live CAPI API version conflicts with the CAPM3 mapping.

`upstream_ci_profile_tested` is separate from the overall status. A supported combination that is not explicitly listed in the periodic CI matrix is not represented as CI-tested.

## Safety and remaining work

The command always returns `mutation_authorized: false` and `provisioning_ready: false`. This is intentional: a release matrix does not prove the local cluster is healthy.

The next gates remain:
1. Collect actual controller and Ironic image tags/digests from the live deployments rather than relying on caller-supplied version labels.
2. Validate all installed served CRD versions and OpenAPI schemas.
3. Check controller deployment readiness and webhook availability.
4. Check Ironic API health, IPA kernel/initramfs assets and checksums, provisioning-network reachability and BMC protocol.
5. Run the upstream-style Libvirt + Ironic E2E suite and archive the result against the repository commit SHA.
6. Only after the real Ironic test tier passes, consider enabling a narrowly scoped provisioning adapter.

## Sources

- https://book.metal3.io/version_support
- https://github.com/metal3-io/metal3-dev-env
- https://github.com/metal3-io/cluster-api-provider-metal3/blob/main/test/e2e/pivoting.go
- https://github.com/metal3-io/baremetal-operator/blob/main/test/e2e/README.md
