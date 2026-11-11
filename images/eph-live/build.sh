#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$ROOT_DIR/../.." && pwd)"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/out}"
KUBERNETES_MINOR="${KUBERNETES_MINOR:-1.35}"
FLANNEL_VERSION="${FLANNEL_VERSION:-v0.27.4}"
FLANNEL_MANIFEST_SHA256="${FLANNEL_MANIFEST_SHA256:-}"
NODE_AGENT_IMAGE="${NODE_AGENT_IMAGE:-}"
BUILDER_IMAGE="${BUILDER_IMAGE:-ai-agent-eph-live-builder:local}"
fail() { printf '%s\n' "$1" >&2; exit 1; }
[[ "$KUBERNETES_MINOR" =~ ^1\.(33|34|35|36)$ ]] || fail "KUBERNETES_MINOR_UNSUPPORTED"
[[ "$FLANNEL_MANIFEST_SHA256" =~ ^[a-fA-F0-9]{64}$ ]] || fail "FLANNEL_MANIFEST_SHA256_REQUIRED"
[[ "$NODE_AGENT_IMAGE" =~ @sha256:[a-f0-9]{64}$ ]] || fail "NODE_AGENT_IMAGE_MUST_BE_DIGEST_PINNED"
command -v docker >/dev/null 2>&1 || fail "DOCKER_REQUIRED"
command -v curl >/dev/null 2>&1 || fail "CURL_REQUIRED"
command -v gpg >/dev/null 2>&1 || fail "GPG_REQUIRED"
mkdir -p "$OUTPUT_DIR"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
cp -a "$ROOT_DIR/." "$WORK_DIR/source/"
mkdir -p "$WORK_DIR/source/config/archives"
mkdir -p "$WORK_DIR/source/config/includes.chroot/usr/local/share/eph"
curl --fail --silent --show-error --location "https://pkgs.k8s.io/core:/stable:/v$KUBERNETES_MINOR/deb/Release.key" -o "$WORK_DIR/kubernetes-release.key"
cp "$WORK_DIR/kubernetes-release.key" "$WORK_DIR/source/config/archives/kubernetes.key.chroot"
printf 'deb https://pkgs.k8s.io/core:/stable:/%s/deb/ /\n' "v$KUBERNETES_MINOR" > "$WORK_DIR/source/config/archives/kubernetes.list.chroot"
curl --fail --silent --show-error --location "https://github.com/flannel-io/flannel/releases/download/$FLANNEL_VERSION/kube-flannel.yml" -o "$WORK_DIR/source/config/includes.chroot/usr/local/share/eph/flannel.yaml"
printf '%s  %s\n' "$FLANNEL_MANIFEST_SHA256" "$WORK_DIR/source/config/includes.chroot/usr/local/share/eph/flannel.yaml" | sha256sum -c -
sed -i 's|FLANNEL_MANIFEST_URL|file:///usr/local/share/eph/flannel.yaml|g' "$WORK_DIR/source/config/includes.chroot/usr/local/sbin/eph-k8s-bootstrap"
chmod 0755 "$WORK_DIR/source/auto/config" "$WORK_DIR/source/config/includes.chroot/usr/local/sbin/eph-k8s-bootstrap"
docker build -f "$ROOT_DIR/Dockerfile.builder" -t "$BUILDER_IMAGE" "$ROOT_DIR"
docker run --rm --privileged -v "$WORK_DIR/source:/work" -v "$OUTPUT_DIR:/output" "$BUILDER_IMAGE" -ec '
  cd /work
  lb config
  lb build
  install -m 0644 live-image-amd64.hybrid.iso /output/eph-live-amd64.iso
  sha256sum /output/eph-live-amd64.iso > /output/eph-live-amd64.iso.sha256
'
printf 'ISO_PATH=%s/eph-live-amd64.iso\n' "$OUTPUT_DIR"
