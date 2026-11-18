#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CLOUD_CONTEXT="${CLOUD_CONTEXT:-}"
EPH_CONTEXT="${EPH_CONTEXT:-}"
TARGET_CONTEXT="${TARGET_CONTEXT:-}"
CLOUD_CONTROL_IMAGE="${CLOUD_CONTROL_IMAGE:-}"
EPH_AGENT_IMAGE="${EPH_AGENT_IMAGE:-}"
TARGET_AGENT_IMAGE="${TARGET_AGENT_IMAGE:-}"
DEPLOYMENT_ID="${DEPLOYMENT_ID:-}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-10m}"
IMAGE_PATTERN='^[A-Za-z0-9._/:@-]+@sha256:[a-f0-9]{64}$'

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

require_context() {
    local context="$1"
    kubectl config get-contexts -o name | grep -Fxq "$context" || fail "KUBECTL_CONTEXT_NOT_FOUND:$context"
    kubectl --context "$context" get --raw=/readyz >/dev/null
}

require_secret_keys() {
    local context="$1"
    local secret="$2"
    shift 2
    local keys
    keys="$(kubectl --context "$context" --namespace ai-agent-system get secret "$secret" -o go-template='{{range $key, $value := .data}}{{printf "%s\n" $key}}{{end}}')" || fail "REQUIRED_SECRET_NOT_FOUND:$context:$secret"
    for key in "$@"; do
        printf '%s\n' "$keys" | grep -Fxq "$key" || fail "REQUIRED_SECRET_KEY_MISSING:$context:$secret:$key"
    done
}

mark_eph_cleanup_resources() {
    local context="$1"
    local kind="$2"
    local name="$3"
    kubectl --context "$context" --namespace ai-agent-system label "$kind" "$name" \
        app.kubernetes.io/managed-by=ai-agent-stack \
        ai-agent-stack.io/eph-cleanup=true \
        "ai-agent-stack.io/deployment-id=$DEPLOYMENT_ID" --overwrite
    kubectl --context "$context" --namespace ai-agent-system annotate "$kind" "$name" \
        ai-agent-stack.io/eph-cleanup=true --overwrite
}

mark_eph_agent_pod_template() {
    local patch
    patch="$(printf '{"spec":{"template":{"metadata":{"labels":{"app.kubernetes.io/managed-by":"ai-agent-stack","ai-agent-stack.io/eph-cleanup":"true","ai-agent-stack.io/deployment-id":"%s"},"annotations":{"ai-agent-stack.io/eph-cleanup":"true"}}}}}' "$DEPLOYMENT_ID")"
    kubectl --context "$EPH_CONTEXT" --namespace ai-agent-system patch deployment eph-agent \
        --type merge --patch "$patch"
}

rendered_apply() {
    local context="$1"
    local file="$2"
    local image="$3"
    local placeholder="$4"
    local mode="$5"
    if [ "$mode" = "dry-run" ]; then
        sed "s|$placeholder|$image|g" "$file" |
            kubectl --context "$context" apply --dry-run=server -f -
    else
        sed "s|$placeholder|$image|g" "$file" |
            kubectl --context "$context" apply -f -
    fi
}

[ -n "$CLOUD_CONTEXT" ] || fail "CLOUD_CONTEXT_REQUIRED"
[ -n "$EPH_CONTEXT" ] || fail "EPH_CONTEXT_REQUIRED"
[ -n "$TARGET_CONTEXT" ] || fail "TARGET_CONTEXT_REQUIRED"
[[ "$DEPLOYMENT_ID" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || fail "DEPLOYMENT_ID_MUST_BE_A_DNS_LABEL"
[ "$CLOUD_CONTEXT" != "$EPH_CONTEXT" ] || fail "CLOUD_AND_EPH_CONTEXTS_MUST_DIFFER"
[ "$CLOUD_CONTEXT" != "$TARGET_CONTEXT" ] || fail "CLOUD_AND_TARGET_CONTEXTS_MUST_DIFFER"
[ "$EPH_CONTEXT" != "$TARGET_CONTEXT" ] || fail "EPH_AND_TARGET_CONTEXTS_MUST_DIFFER"
[[ "$CLOUD_CONTROL_IMAGE" =~ $IMAGE_PATTERN ]] || fail "CLOUD_CONTROL_IMAGE_MUST_BE_PINNED_BY_SHA256_DIGEST"
[[ "$EPH_AGENT_IMAGE" =~ $IMAGE_PATTERN ]] || fail "EPH_AGENT_IMAGE_MUST_BE_PINNED_BY_SHA256_DIGEST"
[[ "$TARGET_AGENT_IMAGE" =~ $IMAGE_PATTERN ]] || fail "TARGET_AGENT_IMAGE_MUST_BE_PINNED_BY_SHA256_DIGEST"

require_context "$CLOUD_CONTEXT"
require_context "$EPH_CONTEXT"
require_context "$TARGET_CONTEXT"

for context in "$CLOUD_CONTEXT" "$EPH_CONTEXT" "$TARGET_CONTEXT"; do
    kubectl --context "$context" apply -f "$SCRIPT_DIR/namespace.yaml"
done

require_secret_keys "$CLOUD_CONTEXT" cloud-control-agent-database database-url
require_secret_keys "$CLOUD_CONTEXT" cloud-control-agent-mtls tls.crt tls.key client-ca.crt
require_secret_keys "$EPH_CONTEXT" eph-agent-credentials control-url agent-id installation-id
require_secret_keys "$EPH_CONTEXT" agent-control-plane-mtls ca.crt tls.crt tls.key
require_secret_keys "$EPH_CONTEXT" capi-pivot-source-kubeconfig kubeconfig
require_secret_keys "$TARGET_CONTEXT" target-agent-credentials control-url agent-id installation-id cluster-id ironic-api-url ironic-api-token ironic-internal-ntp-ip
require_secret_keys "$TARGET_CONTEXT" target-agent-source-observer kubeconfig
require_secret_keys "$TARGET_CONTEXT" agent-control-plane-mtls ca.crt tls.crt tls.key

rendered_apply "$CLOUD_CONTEXT" "$SCRIPT_DIR/cloud-control-agent.yaml" "$CLOUD_CONTROL_IMAGE" "REPLACE_WITH_REGISTRY/ai-agent-cloud-control-agent:VERSION" dry-run
rendered_apply "$EPH_CONTEXT" "$SCRIPT_DIR/eph-agent.yaml" "$EPH_AGENT_IMAGE" "REPLACE_WITH_REGISTRY/ai-agent-eph-agent:VERSION" dry-run
rendered_apply "$TARGET_CONTEXT" "$SCRIPT_DIR/target-cluster-agent.yaml" "$TARGET_AGENT_IMAGE" "REPLACE_WITH_REGISTRY/ai-agent-target-agent:VERSION" dry-run

rendered_apply "$CLOUD_CONTEXT" "$SCRIPT_DIR/cloud-control-agent.yaml" "$CLOUD_CONTROL_IMAGE" "REPLACE_WITH_REGISTRY/ai-agent-cloud-control-agent:VERSION" apply
rendered_apply "$EPH_CONTEXT" "$SCRIPT_DIR/eph-agent.yaml" "$EPH_AGENT_IMAGE" "REPLACE_WITH_REGISTRY/ai-agent-eph-agent:VERSION" apply
rendered_apply "$TARGET_CONTEXT" "$SCRIPT_DIR/target-cluster-agent.yaml" "$TARGET_AGENT_IMAGE" "REPLACE_WITH_REGISTRY/ai-agent-target-agent:VERSION" apply

mark_eph_cleanup_resources "$EPH_CONTEXT" deployment eph-agent
mark_eph_agent_pod_template
for secret in eph-agent-credentials agent-control-plane-mtls capi-pivot-source-kubeconfig; do
    mark_eph_cleanup_resources "$EPH_CONTEXT" secret "$secret"
    kubectl --context "$EPH_CONTEXT" --namespace ai-agent-system annotate secret "$secret" \
        ai-agent-stack.io/credential-scope=eph-agent --overwrite
done
if kubectl --context "$EPH_CONTEXT" --namespace ai-agent-system get secret \
    capi-pivot-target-kubeconfig >/dev/null 2>&1; then
    mark_eph_cleanup_resources "$EPH_CONTEXT" secret capi-pivot-target-kubeconfig
    kubectl --context "$EPH_CONTEXT" --namespace ai-agent-system annotate secret \
        capi-pivot-target-kubeconfig ai-agent-stack.io/credential-scope=eph-agent --overwrite
fi
mark_eph_cleanup_resources "$TARGET_CONTEXT" secret target-agent-source-observer

kubectl --context "$CLOUD_CONTEXT" --namespace ai-agent-system rollout status deployment/cloud-control-agent --timeout="$ROLLOUT_TIMEOUT"
kubectl --context "$EPH_CONTEXT" --namespace ai-agent-system rollout status deployment/eph-agent --timeout="$ROLLOUT_TIMEOUT"
kubectl --context "$TARGET_CONTEXT" --namespace ai-agent-system rollout status deployment/target-agent --timeout="$ROLLOUT_TIMEOUT"

printf '%s\n' "THREE_AGENT_DEPLOYMENT_READY"
