# Deprecated: legacy Node Agent package

**DEPRECATED — DO NOT USE FOR NEW DEVELOPMENT**

The old Node Agent runtime and its shared EPH/Target implementation are superseded by the clean three-Agent architecture in [ADR 0005](../../docs/adr/0005-three-agent-fresh-runtime.md).

The only supported Agent roles are:

1. Cloud Control Agent — `apps/cloud_control_agent`, deployed in the cloud management environment.
2. EPH Agent — `apps/eph_agent`, deployed in the temporary EPH Single-Node Kubernetes environment.
3. Target Agent — `apps/target_agent`, deployed as three replicas across Target Control Plane masters with Kubernetes Lease leader election.

The new images and active deployment scripts do not copy, import, or execute this package. Do not add new imports, tests, features, adapters, manifests, or image build steps against `apps/node_agent`.

This directory remains temporarily while the platform API registry, command lifecycle, deployment checkpoint and legacy test migrations are completed. It is scheduled for deletion as part of the legacy API/model removal; its presence is not an approved runtime or compatibility contract.
