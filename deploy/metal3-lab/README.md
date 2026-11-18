# Metal3 Virtual Lab

This runner uses the upstream `metal3-io/metal3-dev-env` workflow at immutable commit
`7076f07624cdfba990f83bea24f749b37e3baad7`. It does not invent a local Ironic,
BMO, CAPM3 or CAPI deployment implementation.

The upstream workflow creates a Kubernetes management cluster and virtual bare-metal
hosts. The project runner exposes the upstream host preparation, verification,
provision/pivot/re-pivot/deprovision lifecycle and cleanup as explicit commands.

## Safety and host requirements

The upstream scripts modify host networking, packages, libvirt and cluster state.
Run them only inside a disposable Ubuntu 22.04/24.04 VM. Do not run `prepare`,
`lifecycle` or `clean` on a production host.

Minimum documented development host size for Ubuntu targets is 4 CPUs and 8 GiB RAM.
KVM/nested virtualization is the normal hypervisor. Upstream QEMU mode is experimental
and must be selected explicitly.

## Run

```bash
export METAL3_LAB_ALLOW_HOST_MUTATION=1
export METAL3_LAB_WORK_DIR=/var/lib/metal3-lab
bash deploy/metal3-lab/run.sh prepare
bash deploy/metal3-lab/run.sh verify
bash deploy/metal3-lab/run.sh lifecycle
```

After collecting logs and confirming that no test artifacts are needed:

```bash
bash deploy/metal3-lab/run.sh clean
```

The runner records timestamped logs under `$METAL3_LAB_WORK_DIR/artifacts`.
It verifies the checked-out upstream commit before every action. It does not silently
follow upstream `main`.

## What this validates

The upstream dev-env lifecycle validates the community's virtualized CAPI/Metal3
provisioning and pivot workflow. It is a provider/environment validation tier. This is not an AI Agent Stack three-agent end-to-end test. It does not claim to have exercised
Cloud Control Agent command scheduling, EPH Agent pivot execution, Target Agent
handoff, the project's worker provisioning adapter, or the project's EPH cleanup.

The repository's own agent integration must be layered onto this live environment:
observe real Cluster/Machine/Metal3Machine/BMH objects, invoke the agents through the
Cloud Control API and verify state transitions and durable attestations. Unit and
mocked API tests must never be reported as `LIBVIRT_IRONIC` evidence.

## Upstream references

- [Metal3 dev-env](https://github.com/metal3-io/metal3-dev-env)
- [Developer environment guide](https://book.metal3.io/developer_environment/tryit)
- [Metal3 quick-start](https://book.metal3.io/quick-start)
- [BMO E2E guide](https://github.com/metal3-io/baremetal-operator/blob/main/test/e2e/README.md)
