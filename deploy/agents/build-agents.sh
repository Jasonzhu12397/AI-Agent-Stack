#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE_TAG="${IMAGE_TAG:-}"
CLOUD_CONTROL_IMAGE_REPOSITORY="${CLOUD_CONTROL_IMAGE_REPOSITORY:-}"
EPH_AGENT_IMAGE_REPOSITORY="${EPH_AGENT_IMAGE_REPOSITORY:-}"
TARGET_AGENT_IMAGE_REPOSITORY="${TARGET_AGENT_IMAGE_REPOSITORY:-}"
PLATFORMS="${PLATFORMS:-linux/amd64}"
CLUSTERCTL_VERSION="${CLUSTERCTL_VERSION:-}"
CLUSTERCTL_SHA256_AMD64="${CLUSTERCTL_SHA256_AMD64:-}"
KUBECTL_VERSION="${KUBECTL_VERSION:-}"
KUBECTL_SHA256_AMD64="${KUBECTL_SHA256_AMD64:-}"

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

valid_repository() {
    [[ "$1" =~ ^[A-Za-z0-9._:-]+(/[A-Za-z0-9._-]+)+$ ]]
}

valid_repository "$CLOUD_CONTROL_IMAGE_REPOSITORY" || fail "CLOUD_CONTROL_IMAGE_REPOSITORY_INVALID"
valid_repository "$EPH_AGENT_IMAGE_REPOSITORY" || fail "EPH_AGENT_IMAGE_REPOSITORY_INVALID"
valid_repository "$TARGET_AGENT_IMAGE_REPOSITORY" || fail "TARGET_AGENT_IMAGE_REPOSITORY_INVALID"
[[ "$IMAGE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || fail "IMAGE_TAG_INVALID"
[ "$IMAGE_TAG" != "latest" ] || fail "MUTABLE_LATEST_TAG_NOT_ALLOWED"
[ "$PLATFORMS" = "linux/amd64" ] || fail "ONLY_LINUX_AMD64_IS_CURRENTLY_VERIFIED"
[[ "$CLUSTERCTL_VERSION" =~ ^v[0-9]+[.][0-9]+[.][0-9]+$ ]] || fail "CLUSTERCTL_VERSION_INVALID"
[[ "$KUBECTL_VERSION" =~ ^v[0-9]+[.][0-9]+[.][0-9]+$ ]] || fail "KUBECTL_VERSION_INVALID"
[[ "$CLUSTERCTL_SHA256_AMD64" =~ ^[a-fA-F0-9]{64}$ ]] || fail "CLUSTERCTL_SHA256_AMD64_INVALID"
[[ "$KUBECTL_SHA256_AMD64" =~ ^[a-fA-F0-9]{64}$ ]] || fail "KUBECTL_SHA256_AMD64_INVALID"

docker buildx version >/dev/null

build_image() {
    local name="$1"
    local repository="$2"
    local dockerfile="$3"
    shift 3
    local metadata_file
    metadata_file="$(mktemp)"
    docker buildx build \
        --platform "$PLATFORMS" \
        --file "$ROOT_DIR/$dockerfile" \
        "$@" \
        --tag "$repository:$IMAGE_TAG" \
        --push \
        --metadata-file "$metadata_file" \
        "$ROOT_DIR"
    local digest
    digest="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["containerimage.digest"])' "$metadata_file")"
    rm -f "$metadata_file"
    [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || fail "${name}_IMAGE_DIGEST_INVALID"
    printf '%s_IMAGE_REF=%s@%s\n' "$name" "$repository" "$digest"
}

build_image CLOUD_CONTROL_AGENT "$CLOUD_CONTROL_IMAGE_REPOSITORY" Dockerfile.cloud-control-agent
build_image EPH_AGENT "$EPH_AGENT_IMAGE_REPOSITORY" Dockerfile.eph-agent \
    --build-arg "CLUSTERCTL_VERSION=$CLUSTERCTL_VERSION" \
    --build-arg "CLUSTERCTL_SHA256_AMD64=$CLUSTERCTL_SHA256_AMD64" \
    --build-arg "KUBECTL_VERSION=$KUBECTL_VERSION" \
    --build-arg "KUBECTL_SHA256_AMD64=$KUBECTL_SHA256_AMD64"
build_image TARGET_AGENT "$TARGET_AGENT_IMAGE_REPOSITORY" Dockerfile.target-agent \
    --build-arg "KUBECTL_VERSION=$KUBECTL_VERSION" \
    --build-arg "KUBECTL_SHA256_AMD64=$KUBECTL_SHA256_AMD64"
