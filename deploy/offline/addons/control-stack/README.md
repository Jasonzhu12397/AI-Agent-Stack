# Control-stack offline manifests

`ipam.yaml` is the Metal3 IP address manager v1.9.6 multi-document release
manifest, vendored for the EPH bootstrap cluster.

- Source: https://github.com/metal3-io/ip-address-manager/releases/download/v1.9.6/ipam-components.yaml
- License: Apache-2.0
- SHA256: 854609a7a90e8ab83a13a1d332c977985e42fe9304f4da0ad1896e89240ab55d
- Local patch: the upstream release ships `${VAR:=default}` placeholders in the
  controller args, which Kubernetes does not expand. They are replaced with
  their defaults (`:8443`, `false`, `VersionTLS13`); without this the manager
  exits at startup and the IPPool admission webhook never serves.
- Namespace: capm3-system (controller `ipam-manager`)
- Image: quay.io/metal3-io/ip-address-manager:v1.9.6
