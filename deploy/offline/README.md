# Offline image factory

This directory builds the three repository-owned Agent images and packages every
Metal3/IPA dependency without using an external registry during EPH deployment.
Docker/Skopeo are required **only on the image build machine**, never on the EPH
node. EPH and Target workloads remain Kubernetes Deployments.

Both the EPH Live OS and every Target control-plane/worker OS image must already
contain compatible `containerd`, `kubeadm`, `kubelet` and `kubectl` binaries.
Cloud-init and CAPI only verify and enable them; they do not install Kubernetes
packages. Kubeadm skips the kube-proxy addon on EPH and Target control planes.
The Target Agent applies Calico eBPF, waits for Calico readiness, and only then
removes the kube-proxy DaemonSet and ConfigMap.

## 1. Select versions

Edit `images.json` before fetching. It contains one entry for every image, the
Python base image, kubectl, clusterctl, Python requirements, IPA branch/flavor,
and the httpd-infra Pod definition. The vbmc and sushy-tools emulator Pods are
only declared for a virtual fleet; a physical one has real BMCs and the
`metal3_deploy.py verify` check requires them only with `--emulated-bmc`.

`main`, `master` and `latest` are convenient upstream development references, not
immutable versions. Replace them with the exact releases validated on your
hardware when required. Every fetch resolves the actual copied image manifest to
a digest and records SHA256 for every archive, binary, wheel and IPA tarball.
Changing any version or URL invalidates the old lock and requires another fetch.

## 2. Fetch once on a connected build machine

Requirements: Python 3.12, Docker with buildx, Skopeo, and network access.

```bash
python3 tools/offline_images.py fetch
```

Downloads are stored under `deploy/offline/assets/`; `assets/lock.json` is the
reproducibility lock. The lock is written only after the complete fetch succeeds.
No deployment machine connects to Quay, Docker Hub, PyPI, GitHub, dl.k8s.io or
OpenDev.

## 3. Build with no network

Disconnect the build machine or block its network, then run:

```bash
python3 tools/offline_images.py build --offline
```

To merge the generated local image references into an existing EPH bootstrap
configuration:

```bash
python3 tools/offline_images.py build --offline \
  --bootstrap /path/to/bootstrap-input.json \
  --endpoint 10.0.0.10:5000
```

The build always uses `--pull=false` and `--network=none`, regardless of whether
the optional `--offline` flag is written. It verifies the manifest hash and every
asset SHA256 before invoking Docker. Agent Dockerfiles install Python packages
exclusively with `pip --no-index`; they contain no apt, curl, wget or other
network installation path.

`deploy/offline/output/` contains:

- Docker archives for Cloud Control, EPH Agent and Target Agent;
- archives for registry, vbmc, sushy-tools, Ironic, BMO, CAPI, CAPM3, IPAM,
  IRSO and the other configured Metal3 images;
- the IPA kernel/ramdisk archive;
- kube-apiserver, kube-controller-manager, kube-scheduler, etcd, CoreDNS and
  pause archives with the exact version tags Kubeadm expects;
- Calico node, CNI, kube-controllers and Tigera operator images for the eBPF
  data plane; kube-proxy is deliberately excluded;
- `bootstrap-images.json`, with pinned local-registry references for every image;
- `bootstrap.json` when `--bootstrap` was supplied, preserving the original
  bootstrap fields and adding `agent_image`, `agent.image`, `registry` and `ipa`;
- ready-to-consume registry seed data for `eph-iso/local_registry.py`.

Use the generated `bootstrap.json` as the EPH Live ISO bootstrap input and include
`output/images/` plus `output/ipa/` in the ISO. Before `kubeadm init`, EPH imports
all archives into containerd without contacting a registry. It then starts the
registry Pod, publishes and digest-checks every image, and starts the remaining
Deployments. Target Kubeadm configuration uses the local `imageRepository`,
CoreDNS repository and pause `sandbox_image`; remote nodes pull only from EPH.
No Compose service or external registry is used.

## Scope

Cloud Control Agent, EPH Agent and Target Agent are built from this repository's
source. Metal3 and other upstream images are fetched, pinned, verified and
packaged as upstream artifacts; they are not misrepresented as source rebuilds.
The mocked tests validate this workflow, not hardware compatibility. Record the
exact versions that pass your real BMC/IPA/Metal3 deployment in `images.json` and
keep the resulting `lock.json` with the corresponding offline release.
