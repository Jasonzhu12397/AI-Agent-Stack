#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-}"
IMAGE_TAG="${IMAGE_TAG:-}"
PLATFORMS="${PLATFORMS:-linux/amd64}"
CLUSTERCTL_VERSION="${CLUSTERCTL_VERSION:-}"
CLUSTERCTL_SHA256_AMD64="${CLUSTERCTL_SHA256_AMD64:-}"
CLUSTERCTL_SHA256_ARM64="${CLUSTERCTL_SHA256_ARM64:-}"
KUBECTL_VERSION="${KUBECTL_VERSION:-}"
KUBECTL_SHA256_AMD64="${KUBECTL_SHA256_AMD64:-}"
KUBECTL_SHA256_ARM64="${KUBECTL_SHA256_ARM64:-}"

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

[[ "$IMAGE_REPOSITORY" =~ ^[A-Za-z0-9._:-]+(/[A-Za-z0-9._-]+)+$ ]] || fail "IMAGE_REPOSITORY_INVALID"
[[ "$IMAGE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || fail "IMAGE_TAG_INVALID"
[ "$IMAGE_TAG" != "latest" ] || fail "MUTABLE_LATEST_TAG_NOT_ALLOWED"
[[ "$CLUSTERCTL_VERSION" =~ ^v[0-9]+[.][0-9]+[.][0-9]+$ ]] || fail "CLUSTERCTL_VERSION_INVALID"
[[ "$CLUSTERCTL_SHA256_AMD64" =~ ^[a-fA-F0-9]{64}$ ]] || fail "CLUSTERCTL_SHA256_AMD64_INVALID"
[[ "$CLUSTERCTL_SHA256_ARM64" =~ ^[a-fA-F0-9]{64}$ ]] || fail "CLUSTERCTL_SHA256_ARM64_INVALID"
[[ "$KUBECTL_VERSION" =~ ^v[0-9]+[.][0-9]+[.][0-9]+$ ]] || fail "KUBECTL_VERSION_INVALID"
[[ "$KUBECTL_SHA256_AMD64" =~ ^[a-fA-F0-9]{64}$ ]] || fail "KUBECTL_SHA256_AMD64_INVALID"
[[ "$KUBECTL_SHA256_ARM64" =~ ^[a-fA-F0-9]{64}$ ]] || fail "KUBECTL_SHA256_ARM64_INVALID"
[[ "$PLATFORMS" =~ ^linux/(amd64|arm64)(,linux/(amd64|arm64))*$ ]] || fail "PLATFORMS_INVALID"

docker buildx version >/dev/null
metadata_file="$(mktemp)"
trap 'rm -f "$metadata_file"' EXIT

docker buildx build \
    --platform "$PLATFORMS" \
    --file "$ROOT_DIR/Dockerfile.node-agent" \
    --build-arg "CLUSTERCTL_VERSION=$CLUSTERCTL_VERSION" \
    --build-arg "CLUSTERCTL_SHA256_AMD64=$CLUSTERCTL_SHA256_AMD64" \\
    --build-arg "CLUSTERCTL_SHA256_ARM64=$CLUSTERCTL_SHA256_ARM64" \
    --build-arg "KUBECTL_VERSION=$KUBECTL_VERSION" \
    --build-arg "KUBECTL_SHA256_AMD64=$KUBECTL_SHA256_AMD64" \\
    --build-arg "KUBECTL_SHA256_ARM64=$KUBECTL_SHA256_ARM64" \
    --tag "$IMAGE_REPOSITORY:$IMAGE_TAG" \
    --push \
    --metadata-file "$metadata_file" \
    "$ROOT_DIR"

digest="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["containerimage.digest"])' "$metadata_file")"
[[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || fail "BUILT_IMAGE_DIGEST_INVALID"
printf 'AGENT_IMAGE_REF=%s@%s\n' "$IMAGE_REPOSITORY" "$digest"
