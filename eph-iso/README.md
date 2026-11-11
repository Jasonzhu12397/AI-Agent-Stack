# EPH Live ISO

This directory builds the first repeatable x86_64 EPH Live ISO runtime. The ISO boots a Debian Live system with `toram`, runs a single-node K3s Kubernetes control plane, and deploys the EPH Agent as a Kubernetes Deployment Pod. The ISO does not install an operating system to the node's local disk.

## Build prerequisites

Run the builder on a Debian 12/bookworm build host. Install the required packages with:

```bash
sudo apt-get update
sudo apt-get install -y live-build xorriso squashfs-tools wget ca-certificates debootstrap
```

The build currently targets amd64 only. The build currently targets amd64 only. Pin the K3s release and provide its independently verified SHA-256 checksum:

```bash
sudo env \
  K3S_VERSION=v1.35.0+k3s1 \
  K3S_SHA256_AMD64=<verified-64-character-sha256> \
  bash eph-iso/build.sh
```

The version above is an example input, not a claim that its checksum has been verified. Obtain the checksum from the official K3s release artifact and verify it independently. The build fails closed if the version or checksum is missing or malformed. The resulting ISO and checksum file are written to `eph-iso/out/`.

## Boot and enrollment

1. Write the generated ISO to virtual media or boot it through the site's approved PXE/iPXE workflow. The default kernel command line uses `toram`; the live root and K3s state are RAM-backed. No local disk installation is performed.
2. Prepare a separate FAT or ext filesystem volume labelled `EPHBOOT`. Never embed node credentials in the generic ISO.
3. Put a `bootstrap.json` file at the root of that volume with this schema:

```json
{
  "control_url": "https://agent-control.example.com",
  "node_id": "00000000-0000-4000-8000-000000000001",
  "enrollment_token": "<one-time-agent-enrollment-token>",
  "agent_image": "registry.example.com/ai-agent-node-agent@sha256:<64-lowercase-hex-digest>"
}
```

4. Put the mTLS files at `tls/ca.crt`, `tls/tls.crt`, and `tls/tls.key`. Optionally put both CAPI source and target kubeconfigs at `capi/source.kubeconfig` and `capi/target.kubeconfig`; provide both or neither. The config volume is mounted read-only with `nosuid,nodev,noexec`.
5. Boot the ISO. K3s starts as a single-node Kubernetes control plane. The bootstrap service creates EPH credentials and mTLS Secrets, optionally creates the CAPI kubeconfig Secret when both kubeconfigs are present, applies dedicated EPH-only ServiceAccount/RBAC/Deployment manifests, and waits for the Agent Deployment rollout.
6. Confirm the node heartbeat and `node.health` result in the Cloud Control Plane before starting any bootstrap orchestration.

The enrollment token is read from the removable bootstrap volume and copied into a Kubernetes Secret using temporary mode-0600 files. The bootstrap service calls `k3s kubectl` directly, so the ISO does not need a separate kubectl binary. It is not written into the ISO, the kernel command line, or shell history. The one-time token and client certificate must be issued for the intended node by the existing Agent enrollment flow. Never reuse a Target Agent identity for the EPH runtime.

## Per-node network configuration

The ISO mounts the separate `EPHBOOT` volume before systemd-networkd starts. Put one or more site-specific networkd files in its `network/` directory. The bootstrap service stages regular `.network`, `.netdev`, and `.link` files into `/etc/systemd/network` before the network service starts. Filenames are restricted and each file is limited to 32 KiB.

For DHCP, copy and adapt `eph-iso/examples/network/10-uplink-dhcp.network` to `EPHBOOT/network/10-uplink.network`. For static addressing, adapt `eph-iso/examples/network/10-uplink-static.network`. Replace the example interface name and documentation-only IP addresses with values from the site network plan. Do not assume a fixed interface name or MAC address. Bond, VLAN, bridge, and link settings can be supplied as additional networkd `.netdev`, `.network`, and `.link` files.

The example profiles are not automatically installed into the ISO. A boot volume without a usable network profile may boot Kubernetes but cannot enroll the EPH Agent with the Cloud Control Plane.

## Per-node network configuration

The ISO mounts the separate `EPHBOOT` volume before systemd-networkd starts. Put site-specific networkd files in its `network/` directory. The bootstrap service stages regular `.network`, `.netdev`, and `.link` files into `/etc/systemd/network` before the network service starts. Filenames are restricted and each file is limited to 32 KiB.

For DHCP, adapt `eph-iso/examples/network/10-uplink-dhcp.network` and copy it to `EPHBOOT/network/10-uplink.network`. For static addressing, adapt `eph-iso/examples/network/10-uplink-static.network`. Replace the example interface name and documentation-only IP addresses with values from the site network plan. Do not assume a fixed interface name or MAC address. Bond, VLAN, bridge, and link settings can be supplied as additional networkd `.netdev`, `.network`, and `.link` files.

The example profiles are not automatically installed into the ISO. A boot volume without a usable network profile may boot Kubernetes but cannot enroll the EPH Agent with the Cloud Control Plane.

## Runtime layout

- Debian Live root filesystem: booted with `toram`; no disk installation.
- K3s server: systemd service, single-node Kubernetes control plane.
- `/var/lib/rancher/k3s` and `/var/lib/kubelet`: tmpfs mounts; all cluster state is disposable.
- EPH Agent: ordinary Kubernetes Deployment Pod in namespace `ai-agent-system`, not Docker Compose and not a host process.
- Container image: required to be pinned by immutable SHA-256 digest.
- CNI: K3s default Flannel for the temporary EPH cluster. This is independent from the target cluster's Calico/Multus/OVS/SR-IOV data-plane design.

## Current limitations and safety boundary

This first increment provides an amd64 ISO build and Kubernetes-native EPH Agent bootstrap. It does not yet install CAPI/Metal3 providers, perform a CAPI pivot, provision hardware, or reclaim/reimage the host. Those remain explicit orchestration phases and must be independently versioned and tested. Do not treat a successful ISO boot as permission to mutate Ironic, BMC, CAPI or target-cluster resources.

The ISO has no automatic disk persistence. Any durable deployment state, checkpoint, image metadata, physical identity, approval and audit record must be stored in the Cloud Control Plane before reboot/reclaim. The current K3s embedded datastore and kubelet state are intentionally lost on power-off.
