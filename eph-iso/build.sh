#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$ROOT_DIR/.." && pwd)"
K3S_VERSION="${K3S_VERSION:-}"
K3S_SHA256_AMD64="${K3S_SHA256_AMD64:-}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/out}"
BUILD_DIR="$ROOT_DIR/.build"
ARCHITECTURE="${ARCHITECTURE:-amd64}"

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || fail "BUILD_MUST_RUN_AS_ROOT"
[[ "$K3S_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+\+k3s[0-9]+$ ]] || fail "K3S_VERSION_INVALID"
[[ "$K3S_SHA256_AMD64" =~ ^[a-fA-F0-9]{64}$ ]] || fail "K3S_SHA256_AMD64_INVALID"
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
manifest_dir="$BUILD_DIR/config/includes.chroot/usr/share/ai-agent-studio/eph-manifests"
cp "$REPO_ROOT/deploy/agents/namespace.yaml" "$manifest_dir/namespace.yaml"
cp "$ROOT_DIR/eph-rbac.yaml" "$manifest_dir/rbac.yaml"
cp "$REPO_ROOT/deploy/agents/eph-node-agent.yaml" "$manifest_dir/eph-node-agent.yaml"

wget -q -O "$BUILD_DIR/k3s" "https://github.com/k3s-io/k3s/releases/download/$K3S_VERSION/k3s"
printf '%s  %s\n' "$K3S_SHA256_AMD64" "$BUILD_DIR/k3s" |
    sha256sum -c - || fail "K3S_CHECKSUM_MISMATCH"
install -m 0755 "$BUILD_DIR/k3s" "$BUILD_DIR/config/includes.chroot/usr/local/bin/k3s"

cd "$BUILD_DIR"
lb config \
    --distribution bookworm \
    --architectures "$ARCHITECTURE" \
    --binary-images iso-hybrid \
    --archive-areas "main contrib non-free-firmware" \
    --apt-recommends false \
    --debian-installer false \
    --bootappend-live "boot=live components toram noeject hostname=eph-node" \
    --iso-application "AI Agent Studio EPH Live" \
    --iso-publisher "AI Agent Studio" \
    --iso-volume "AIAgentEPH"

lb build

iso_file="$(find "$BUILD_DIR" -maxdepth 1 -type f -name '*.iso' -print -quit)"
[ -n "$iso_file" ] || fail "ISO_ARTIFACT_NOT_FOUND"
version_tag="${K3S_VERSION#v}"
output_file="$OUTPUT_DIR/ai-agent-eph-live-${version_tag}-amd64.iso"
install -m 0644 "$iso_file" "$output_file"
(
    cd "$OUTPUT_DIR"
    sha256sum "$(basename "$output_file")" > "$(basename "$output_file").sha256"
)
printf 'EPH_ISO=%s\n' "$output_file"
printf 'EPH_ISO_SHA256_FILE=%s.sha256\n' "$output_file"
