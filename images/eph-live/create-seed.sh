#!/usr/bin/env bash
set -euo pipefail
OUTPUT_ISO="${OUTPUT_ISO:-$PWD/eph-seed.iso}"
KUBERNETES_VERSION="${KUBERNETES_VERSION:-}"
POD_CIDR="${POD_CIDR:-10.244.0.0/16}"
SERVICE_CIDR="${SERVICE_CIDR:-10.96.0.0/12}"
FLANNEL_MANIFEST_SHA256="${FLANNEL_MANIFEST_SHA256:-}"
NODE_AGENT_IMAGE="${NODE_AGENT_IMAGE:-}"
CONTROL_URL="${CONTROL_URL:-}"
NODE_ID="${NODE_ID:-}"
NODE_TOKEN="${NODE_TOKEN:-}"
CA_CERT_FILE="${CA_CERT_FILE:-}"
CLIENT_CERT_FILE="${CLIENT_CERT_FILE:-}"
CLIENT_KEY_FILE="${CLIENT_KEY_FILE:-}"
FLANNEL_MANIFEST_URL="${FLANNEL_MANIFEST_URL:-file:///usr/local/share/eph/flannel.yaml}"
fail() { printf '%s\n' "$1" >&2; exit 1; }
[[ "$KUBERNETES_VERSION" =~ ^v1\.(33|34|35|36)\.[0-9]+$ ]] || fail "KUBERNETES_VERSION_INVALID"
[[ "$FLANNEL_MANIFEST_SHA256" =~ ^[a-fA-F0-9]{64}$ ]] || fail "FLANNEL_MANIFEST_SHA256_INVALID"
[[ "$NODE_AGENT_IMAGE" =~ @sha256:[a-f0-9]{64}$ ]] || fail "NODE_AGENT_IMAGE_MUST_BE_DIGEST_PINNED"
[[ "$CONTROL_URL" =~ ^https://[^[:space:]]+$ ]] || fail "CONTROL_URL_INVALID"
[[ "$NODE_ID" =~ ^[A-Za-z0-9._:-]{1,128}$ ]] || fail "NODE_ID_INVALID"
[[ "$NODE_TOKEN" =~ ^[A-Za-z0-9._~-]{24,4096}$ ]] || fail "NODE_TOKEN_INVALID"
for file in "$CA_CERT_FILE" "$CLIENT_CERT_FILE" "$CLIENT_KEY_FILE"; do
    [[ -f "$file" && -r "$file" ]] || fail "MTLS_FILE_UNREADABLE"
done
command -v cloud-localds >/dev/null 2>&1 || fail "CLOUD_LOCALDS_REQUIRED"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
chmod 0700 "$WORK_DIR"
mkdir -p "$WORK_DIR/seed"
chmod 0700 "$WORK_DIR/seed"
{
    printf 'KUBERNETES_VERSION=%q\n' "$KUBERNETES_VERSION"
    printf 'POD_CIDR=%q\n' "$POD_CIDR"
    printf 'SERVICE_CIDR=%q\n' "$SERVICE_CIDR"
    printf 'FLANNEL_MANIFEST_URL=%q\n' "$FLANNEL_MANIFEST_URL"
    printf 'FLANNEL_MANIFEST_SHA256=%q\n' "$FLANNEL_MANIFEST_SHA256"
    printf 'NODE_AGENT_IMAGE=%q\n' "$NODE_AGENT_IMAGE"
    printf 'CONTROL_URL=%q\n' "$CONTROL_URL"
    printf 'NODE_ID=%q\n' "$NODE_ID"
    printf 'NODE_TOKEN=%q\n' "$NODE_TOKEN"
} > "$WORK_DIR/bootstrap.env"
chmod 0600 "$WORK_DIR/bootstrap.env"
{
    printf '#cloud-config\n'
    printf 'write_files:\n'
    for item in "bootstrap.env:$WORK_DIR/bootstrap.env" "ca.crt:$CA_CERT_FILE" "tls.crt:$CLIENT_CERT_FILE" "tls.key:$CLIENT_KEY_FILE"; do
        name="${item%%:*}"
        source="${item#*:}"
        printf '  - path: /run/eph-bootstrap/%s\n' "$name"
        printf '    owner: root:root\n'
        printf '    permissions: "%s"\n' "$([[ "$name" == "tls.key" || "$name" == "bootstrap.env" ]] && printf 0600 || printf 0644)"
        printf '    encoding: b64\n'
        printf '    content: %s\n' "$(base64 -w0 "$source")"
    done
} > "$WORK_DIR/seed/user-data"
printf 'instance-id: eph-%s\nlocal-hostname: eph-node\n' "$NODE_ID" > "$WORK_DIR/seed/meta-data"
chmod 0600 "$WORK_DIR/seed/user-data" "$WORK_DIR/seed/meta-data"
mkdir -p "$(dirname -- "$OUTPUT_ISO")"
cloud-localds "$OUTPUT_ISO" "$WORK_DIR/seed/user-data" "$WORK_DIR/seed/meta-data"
chmod 0600 "$OUTPUT_ISO"
sha256sum "$OUTPUT_ISO" > "$OUTPUT_ISO.sha256"
chmod 0600 "$OUTPUT_ISO.sha256"
printf 'SEED_ISO_PATH=%s\n' "$OUTPUT_ISO"
