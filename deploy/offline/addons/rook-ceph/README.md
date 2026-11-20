# Rook-Ceph offline addon bundle

This directory vendors the upstream Rook v1.17.4 installation manifests used by
the Target Agent `rook-ceph` storage addon:

- `crds.yaml` from `deploy/examples/crds.yaml`
- `common.yaml` from `deploy/examples/common.yaml`
- `operator.yaml` from `deploy/examples/operator.yaml`

Source: https://github.com/rook/rook/tree/v1.17.4/deploy/examples
License: Apache License 2.0

SHA-256:

```
cd8aeb52869d47803f13bba75986ec22b397e07b1fc6e7025c6d8b03f89a788d  common.yaml
4911a3969252d4537b697c7cddbaf04e5779debef7e7a1f3239cbeb70cc13f7b  crds.yaml
b8ae3226856679da53c8dbeff8a6ed2aeaffb783f986e2e4572cdeb96e05f4fb  operator.yaml
```

The manifests are applied only by the Target Agent after the checkpoint gate
passes. The CephCluster, storage networks and StorageClasses are generated from
the validated addon spec instead of editing these upstream files.
