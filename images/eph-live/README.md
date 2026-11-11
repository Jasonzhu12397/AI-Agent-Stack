# EPH RAM Live ISO

This directory builds the initial reproducible EPH Live ISO. The intended runtime is a RAM-resident Debian Live OS, a single-node kubeadm Kubernetes cluster, and the EPH Agent as a Kubernetes Deployment. The EPH Agent is not started by Docker Compose or as a standalone host process.

## Boot contract

The ISO uses Debian Live with the toram boot parameter. The live filesystem is copied into RAM before the root filesystem is switched. The temporary root, containerd state, kubelet state, Kubernetes etcd data and Agent Pod writable storage are volatile. The bootstrap workflow does not partition, format, mount, or write to the host disk.

The image includes containerd, kubeadm, kubelet, kubectl, cloud-init, DHCP network configuration and network diagnostics. Kubernetes packages come from the version-specific official pkgs.k8s.io repository. The Agent image must be referenced by immutable registry digest.

## Build

The build host requires Docker, curl, gpg and sha256sum. The builder uses Debian Trixie live-build and emits a hybrid bootable ISO.

    KUBERNETES_MINOR=1.35
    FLANNEL_VERSION=v0.27.4
    FLANNEL_MANIFEST_SHA256=<verified-64-character-sha256>
    NODE_AGENT_IMAGE=registry.example.com/ai-agent-node-agent@sha256:<64-character-digest>
    OUTPUT_DIR="$PWD/out"
    bash images/eph-live/build.sh

The Flannel checksum must be obtained from a trusted release verification process. The builder refuses an empty or malformed checksum and refuses mutable Agent image tags. Never copy a checksum from an untrusted mirror.

## First-boot bootstrap input

The first implementation expects a NoCloud config-drive seed to provide /run/eph-bootstrap/bootstrap.env and mTLS files ca.crt, tls.crt, and tls.key under /run/eph-bootstrap/. The environment file is loaded by systemd and must define:

- KUBERNETES_VERSION (optional exact patch override; if omitted, the ISO uses its installed kubeadm patch from the selected 1.33 through 1.36 minor channel)
- POD_CIDR and SERVICE_CIDR
- FLANNEL_MANIFEST_URL (use file:///usr/local/share/eph/flannel.yaml)
- FLANNEL_MANIFEST_SHA256
- NODE_AGENT_IMAGE (immutable @sha256: reference)
- CONTROL_URL, NODE_ID, and short-lived NODE_TOKEN

Generate a per-node seed ISO with images/eph-live/create-seed.sh after installing cloud-localds (cloud-image-utils). Supply values as environment variables and mTLS file paths as CA_CERT_FILE, CLIENT_CERT_FILE, and CLIENT_KEY_FILE. Keep the seed ISO access-controlled. The seed must be delivered through protected virtual media or an equivalent out-of-band channel. Do not put secrets in kernel command-line parameters, ISO contents, Git, or image build arguments. The seed is volatile and should be removed after successful enrollment. The Agent credential is one-time/short-lived and should be revoked during EPH-to-Target handoff.

The builder bundles a narrowly scoped EPH Agent ServiceAccount and read-only ClusterRole. It does not grant the EPH Agent CAPI, Metal3, Ironic, or general workload mutation permissions.

## Startup sequence

1. Firmware boots the ISO in UEFI or supported legacy mode.
2. Debian Live copies its filesystem to RAM using toram.
3. cloud-init consumes the NoCloud seed and stages bootstrap files under /run/eph-bootstrap.
4. systemd starts DHCP networking, containerd and kubelet.
5. kubeadm initializes the single-node cluster using the configured Kubernetes version and CIDRs.
6. Flannel is applied from the checksum-verified bundled manifest.
7. The bootstrap creates the Agent namespace, read-only RBAC, short-lived credential and mTLS Secrets.
8. The EPH Agent Deployment is created and must become Ready before bootstrap is considered successful.

## Current limitations

This is an initial builder and bootstrap implementation, not a production-certified image. Hardware-specific NIC/firmware drivers, Secure Boot signing, static network policy, IPv6-only operation, proxy/mirror configuration, external secret-manager integration, a central NoCloud seed generator/API, QEMU boot smoke tests, RAM sizing validation, crash recovery, and target OS handoff still need implementation and validation. The ISO build and boot path must pass CI and QEMU before this artifact is treated as release-ready.
