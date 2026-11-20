# SR-IOV Network Operator offline manifests

`operator.yaml` is rendered from the published Helm chart
`oci://ghcr.io/k8snetworkplumbingwg/sriov-network-operator-chart:1.6.0`
with `sriovOperatorConfig.deploy=true` and `--include-crds`.

- Chart digest: sha256:f9519ee4f4090947218fe54950f80506f26b44b3ef86fc1ac7df345e345a1244
- License: Apache-2.0
- SHA256: cfd298702abbb57c8e855f6f6bbd3b6115445e83ec9491dfc4f65872984eab1e
- Namespace: sriov-network-operator
- Controller Deployment: sriov-network-operator
