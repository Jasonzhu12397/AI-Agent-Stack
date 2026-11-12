#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$ROOT_DIR/.." && pwd)"
KUBERNETES_VERSION="${KUBERNETES_VERSION:-v1.35.3}"
KUBERNETES_MINOR=""
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/out}"
BUILD_DIR="$ROOT_DIR/.build"
ARCHITECTURE="${ARCHITECTURE:-amd64}"

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || fail "BUILD_MUST_RUN_AS_ROOT"
[[ "$KUBERNETES_VERSION" =~ ^v1\.(33|34|35|36)\.[0-9]+$ ]] || fail "KUBERNETES_VERSION_INVALID:$KUBERNETES_VERSION"
KUBERNETES_MINOR="${KUBERNETES_VERSION%.*}"
case "$KUBERNETES_MINOR" in
    v1.33) CALICO_VERSION=v3.31.6 ;;
    v1.34) CALICO_VERSION=v3.32.1 ;;
    v1.35|v1.36) CALICO_VERSION=v3.33.0 ;;
    *) fail "KUBERNETES_MINOR_VERSION_NOT_SUPPORTED:$KUBERNETES_MINOR" ;;
esac
[ "$ARCHITECTURE" = amd64 ] || fail "ARCHITECTURE_NOT_SUPPORTED"

for command_name in lb xorriso mksquashfs wget sha256sum; do
    command -v "$command_name" >/dev/null 2>&1 || fail "BUILD_DEPENDENCY_MISSING:$command_name"
done

mkdir -p "$OUTPUT_DIR"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cp -a "$ROOT_DIR/config" "$BUILD_DIR/config"
install -d "$BUILD_DIR/config/includes.chroot/usr/local/lib"
install -d "$BUILD_DIR/config/includes.chroot/usr/local/bin"
install -d "$BUILD_DIR/config/includes.chroot/usr/share/ai-agent-studio/eph-manifests"
install -d "$BUILD_DIR/config/includes.chroot/usr/local/sbin"
cp "$ROOT_DIR/bootstrap.py" "$BUILD_DIR/config/includes.chroot/usr/local/lib/eph-bootstrap.py"
cp "$ROOT_DIR/network-config.py" "$BUILD_DIR/config/includes.chroot/usr/local/lib/eph-network-config.py"
manifest_dir="$BUILD_DIR/config/includes.chroot/usr/share/ai-agent-studio/eph-manifests"
cp "$REPO_ROOT/deploy/agents/namespace.yaml" "$manifest_dir/namespace.yaml"
cp "$ROOT_DIR/eph-rbac.yaml" "$manifest_dir/rbac.yaml"
cp "$REPO_ROOT/deploy/agents/eph-node-agent.yaml" "$manifest_dir/eph-node-agent.yaml"

for binary_name in kubeadm kubelet kubectl; do
    download_url="https://dl.k8s.io/release/$KUBERNETES_VERSION/bin/linux/$ARCHITECTURE/$binary_name"
    wget -q -O "$BUILD_DIR/$binary_name" "$download_url"
    wget -q -O "$BUILD_DIR/$binary_name.sha256" "$download_url.sha256"
    expected_sha256="$(tr -d '[:space:]' < "$BUILD_DIR/$binary_name.sha256")"
    [[ "$expected_sha256" =~ ^[a-fA-F0-9]{64}$ ]] || fail "KUBERNETES_BINARY_CHECKSUM_INVALID:$binary_name"
    printf '%s  %s\n' "$expected_sha256" "$BUILD_DIR/$binary_name" | sha256sum -c - ||
        fail "KUBERNETES_BINARY_CHECKSUM_MISMATCH:$binary_name"
    install -m 0755 "$BUILD_DIR/$binary_name" "$BUILD_DIR/config/includes.chroot/usr/local/bin/$binary_name"
done
install -d "$BUILD_DIR/config/includes.chroot/etc/default"
install -d "$BUILD_DIR/config/includes.chroot/usr/share/ai-agent-studio"
wget -q -O "$BUILD_DIR/config/includes.chroot/usr/share/ai-agent-studio/calico.yaml" "https://raw.githubusercontent.com/projectcalico/calico/$CALICO_VERSION/manifests/calico.yaml"
printf 'KUBERNETES_VERSION=%s\n' "$KUBERNETES_VERSION" > "$BUILD_DIR/config/includes.chroot/etc/default/eph-node"

cd "$BUILD_DIR"
lb config \
    --mode debian \
    --distribution bookworm \
    --architectures "$ARCHITECTURE" \
    --binary-images iso-hybrid \
    --mirror-bootstrap https://deb.debian.org/debian/ \
    --mirror-chroot https://deb.debian.org/debian/ \
    --mirror-chroot-security https://security.debian.org/debian-security/ \
    --mirror-binary https://deb.debian.org/debian/ \
    --mirror-binary-security https://security.debian.org/debian-security/ \
    --archive-areas "main contrib non-free-firmware" \
    --apt-recommends false \
    --debian-installer false \
    --bootappend-live "boot=live components toram noeject hostname=eph-node console=tty0 console=ttyS0,115200n8 systemd.show_status=1 loglevel=7" \
    --iso-application "AI Agent Studio EPH Live" \
    --iso-publisher "AI Agent Studio" \
    --iso-volume "AIAgentEPH"

lb build

iso_file="$(find "$BUILD_DIR" -maxdepth 1 -type f -name '*.iso' -print -quit)"
[ -n "$iso_file" ] || fail "ISO_ARTIFACT_NOT_FOUND"
version_tag="${KUBERNETES_VERSION#v}"
output_file="$OUTPUT_DIR/ai-agent-eph-live-${version_tag}-amd64.iso"
install -m 0644 "$iso_file" "$output_file"
(
    cd "$OUTPUT_DIR"
    sha256sum "$(basename "$output_file")" > "$(basename "$output_file").sha256"
)
iso_sha256="$(sha256sum "$output_file" | awk '{print $1}')"
metadata_file="$output_file.release.json"
printf '{\n  "kubernetes_version": "%s",\n  "calico_version": "%s",\n  "runtime": "kubeadm-containerd",\n  "architecture": "%s",\n  "iso_file": "%s",\n  "sha256": "%s"\n}\n' \
    "$KUBERNETES_VERSION" "$CALICO_VERSION" "$ARCHITECTURE" \
    "$(basename "$output_file")" "$iso_sha256" > "$metadata_file"

printf 'EPH_CALICO_VERSION=%s\n' "$CALICO_VERSION"
printf 'EPH_ISO=%s\n' "$output_file"
printf 'EPH_ISO_SHA256_FILE=%s.sha256\n' "$output_file"

printf 'EPH_ISO_METADATA=%s\n' "$metadata_file"