# EPH Live ISO

The EPH runtime is a reusable Debian 12 Live ISO that boots entirely into RAM with `toram`, starts a single-node Kubernetes cluster with kubeadm, kubelet, and containerd, and runs the EPH Agent as a Kubernetes Deployment Pod. It does not install an operating system to local disk.

The BIOS ISOLINUX menu automatically starts the default Live entry after five seconds, so unattended and headless boots do not wait for keyboard input. CI builds the ISO, validates the BIOS configuration, and boots it under QEMU.

## Build

Build on Debian 12/bookworm with live-build, xorriso, squashfs-tools, wget, ca-certificates, and debootstrap.

~~~bash
sudo env KUBERNETES_VERSION=v1.35.3 bash eph-iso/build.sh
~~~

The builder supports amd64 and Kubernetes minors v1.33, v1.34, v1.35, and v1.36. It downloads kubeadm, kubelet, and kubectl from the official Kubernetes release endpoint and verifies each binary against its published SHA-256 file. The output directory receives the ISO, SHA-256 sidecar, and release metadata JSON.

## Per-node configuration with cloud-init NoCloud

Use one generic ISO for every machine. Do not rebuild the ISO to inject per-node identity, network settings, or credentials. Generate a separate NoCloud seed image for each EPH node and attach it as a second virtual disk/CD-ROM or expose it through approved virtual media.

The seed volume must use the filesystem label `CIDATA` and contain these files at its root:

- `user-data`: cloud-config that writes the node bootstrap configuration and both CA trust anchors.
- `meta-data`: unique instance ID and hostname.
- `network-config`: cloud-init v2 network configuration for the site's interfaces.

Example source files are in `eph-iso/examples/cloud-init/`. The example `user-data` writes these files with root ownership and restrictive permissions:

~~~text
/etc/ai-agent-studio/eph-bootstrap/
  bootstrap.json
  tls/
    ca.crt
    agent-ca.crt
~~~

`bootstrap.json` contains the HTTPS control-plane URL, node UUID, one-time enrollment token, and immutable EPH Agent image reference pinned by SHA-256 digest. The two CA files have different purposes:

- `tls/ca.crt` verifies the Cloud Control Plane HTTPS endpoint.
- `tls/agent-ca.crt` is the independently provisioned Agent client-certificate issuer trust anchor.

Never derive the Agent issuer root from the returned certificate chain. The EPH node generates its own private key and CSR after boot; the private key never leaves the node and is never written to the seed. The Cloud Control Plane consumes the one-time enrollment token only after the trusted issuer signs the CSR, then returns a node-bound client certificate and a separate runtime token.

The bootstrap process reads the cloud-init files from `/etc/ai-agent-studio/eph-bootstrap`, copies the trust anchors into Kubernetes Secrets, applies the dedicated EPH Agent RBAC and Deployment, and waits for the Deployment rollout and a Running, Ready Agent Pod. Optional CAPI pivot kubeconfigs may be supplied only as a complete source/target pair when the workflow authorizes their use.

An additional read-only volume labeled `EPHBOOT` is optional. It is used only for site-specific systemd-networkd profiles under `network/*.network`, `network/*.netdev`, and `network/*.link`; it is not the Agent enrollment source. The network configuration service safely skips this media when it is absent.

For production, the Cloud Control Plane must render a unique NoCloud user-data document per node. The example user-data contains placeholders and is not deployable as-is. Never commit real enrollment tokens, private keys, kubeconfigs, or customer network details. Protect the seed as secret material and destroy it after provisioning.

## Network configuration

Use cloud-init's NoCloud `network-config` to configure DHCP or static addressing, bonds, VLANs, bridges, and interface matching. Do not assume a fixed interface name or MAC address. Production profiles must be rendered from the site's network inventory and validated against the actual NIC topology.

The temporary EPH Kubernetes cluster uses a Calico release selected for the Kubernetes minor: v1.33 uses Calico v3.31.6, v1.34 uses v3.32.1, and v1.35/v1.36 use v3.33.0. The CI QEMU image uses Kubernetes v1.35.3 with Calico v3.33.0. The Pod CIDR is `10.244.0.0/16`; Calico detects the kubeadm-assigned Pod CIDR from the Node configuration. Calico is independent of the eventual target cluster's Calico/Multus/OVS/SR-IOV design. The EPH runtime smoke test checks both Calico DaemonSet and controller Deployment readiness and validates service-DNS and Pod-network connectivity between separate Pods.

## Runtime layout

- Debian Live root filesystem: `toram`; no local-disk installation.
- cloud-init: NoCloud datasource from a per-node CIDATA seed.
- kubeadm: initializes the single-node Kubernetes control plane; kubelet and containerd run as systemd services.
- `/var/lib/containerd`, `/var/lib/etcd`, and `/var/lib/kubelet`: tmpfs; cluster runtime state is disposable.
- CNI: pinned Calico manifest; the target cluster network is configured independently.
- EPH Agent: Kubernetes Deployment Pod in `ai-agent-system`; not Docker Compose and not a host process.
- Agent image: immutable SHA-256 digest.
- Agent identity: locally generated private key, issuer-validated client certificate, node-bound runtime token, and mTLS control-plane requests.

## QEMU acceptance checks

The CI QEMU test is designed to verify the complete temporary-node runtime, not merely ISO creation:

1. `toram` boot, disabled swap, and tmpfs-backed containerd/etcd/kubelet state.
2. Kubernetes API readiness and exactly one Ready Node.
3. Calico node DaemonSet and Calico controller Deployment readiness.
4. An ordinary nginx Deployment Pod reaches Running/Ready.
5. A separate BusyBox client Pod resolves the Kubernetes Service DNS name and successfully reaches the nginx Pod over the cluster network.
6. The EPH bootstrap service reads EPHBOOT, exchanges the one-time enrollment token for a node-bound mTLS identity, and creates the Agent resources.
7. The actual Node Agent image is pulled by digest, the Agent Deployment rolls out, and its Pod reaches Running/Ready on the EPH node.
8. The isolated CI control-plane fixture confirms certificate issuance, mTLS heartbeat, inventory reporting, and command polling. This fixture validates the guest enrollment and transport path; it does not replace integration testing against the production Cloud Control Plane and Vault issuer.

A QEMU smoke test is successful only when the workload network test and the real EPH Agent Pod both pass. A generic pause Pod or a successful Kubernetes API probe alone is insufficient.

## Safety and limitations

This runtime does not perform real CAPI pivot, Metal3/Ironic provisioning, BMC operations, or EPH host reclaim. Those require separate guarded workflow phases, trusted infrastructure integrations, and explicit approvals.

The temporary cluster and its kubelet state are intentionally lost at power-off. Persist workflow checkpoints, hardware identity, audit records, and all deployment state in the Cloud Control Plane before any reboot or reclaim. A successful ISO boot is not permission to mutate Ironic, BMC, CAPI, or target-cluster resources.
