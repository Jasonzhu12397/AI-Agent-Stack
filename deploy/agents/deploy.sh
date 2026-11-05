#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EPH_CONTEXT="${EPH_CONTEXT:-}"
TARGET_CONTEXT="${TARGET_CONTEXT:-}"
AGENT_IMAGE="${AGENT_IMAGE:-}"
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
    keys="$(kubectl --context "$context" --namespace ai-agent-system get secret "$secret" -o go-template='{{range $key, $value := .data}}{{printf "%s\\n" $key}}{{end}}')" || fail "REQUIRED_SECRET_NOT_FOUND:$context:$secret"
    for key in "$@"; do
        printf '%s\n' "$keys" | grep -Fxq "$key" || fail "REQUIRED_SECRET_KEY_MISSING:$context:$secret:$key"
    done
}

apply_common_resources() {
    local context="$1"
    kubectl --context "$context" apply -f "$SCRIPT_DIR/namespace.yaml"
    kubectl --context "$context" apply --dry-run=server -f "$SCRIPT_DIR/rbac.yaml"
    kubectl --context "$context" apply -f "$SCRIPT_DIR/rbac.yaml"
}

rendered_apply() {
    local context="$1"
    local file="$2"
    local mode="$3"
    if [ "$mode" = "dry-run" ]; then
        sed "s|REPLACE_WITH_REGISTRY/ai-agent-node-agent:VERSION|$AGENT_IMAGE|g" "$file" |
            kubectl --context "$context" apply --dry-run=server -f -
    else
        sed "s|REPLACE_WITH_REGISTRY/ai-agent-node-agent:VERSION|$AGENT_IMAGE|g" "$file" |
            kubectl --context "$context" apply -f -
    fi
}

[ -n "$EPH_CONTEXT" ] || fail "EPH_CONTEXT_REQUIRED"
[ -n "$TARGET_CONTEXT" ] || fail "TARGET_CONTEXT_REQUIRED"
[ "$EPH_CONTEXT" != "$TARGET_CONTEXT" ] || fail "EPH_AND_TARGET_CONTEXTS_MUST_DIFFER"
[[ "$AGENT_IMAGE" =~ $IMAGE_PATTERN ]] || fail "AGENT_IMAGE_MUST_BE_PINNED_BY_SHA256_DIGEST"

require_context "$EPH_CONTEXT"
require_context "$TARGET_CONTEXT"
kubectl --context "$EPH_CONTEXT" apply -f "$SCRIPT_DIR/namespace.yaml"
kubectl --context "$TARGET_CONTEXT" apply -f "$SCRIPT_DIR/namespace.yaml"
require_secret_keys "$EPH_CONTEXT" eph-node-agent-credentials control-url node-id token
require_secret_keys "$EPH_CONTEXT" capi-pivot-kubeconfigs source.kubeconfig target.kubeconfig
require_secret_keys "$TARGET_CONTEXT" target-cluster-agent-credentials control-url node-id token
require_secret_keys "$TARGET_CONTEXT" ironic-api-credentials api-url token

apply_common_resources "$EPH_CONTEXT"
apply_common_resources "$TARGET_CONTEXT"

rendered_apply "$EPH_CONTEXT" "$SCRIPT_DIR/eph-node-agent.yaml" dry-run
rendered_apply "$TARGET_CONTEXT" "$SCRIPT_DIR/target-cluster-agent.yaml" dry-run
rendered_apply "$TARGET_CONTEXT" "$SCRIPT_DIR/network-inventory-agent.yaml" dry-run

rendered_apply "$EPH_CONTEXT" "$SCRIPT_DIR/eph-node-agent.yaml" apply
rendered_apply "$TARGET_CONTEXT" "$SCRIPT_DIR/target-cluster-agent.yaml" apply
rendered_apply "$TARGET_CONTEXT" "$SCRIPT_DIR/network-inventory-agent.yaml" apply

kubectl --context "$EPH_CONTEXT" --namespace ai-agent-system rollout status deployment/eph-node-agent --timeout="$ROLLOUT_TIMEOUT"
kubectl --context "$TARGET_CONTEXT" --namespace ai-agent-system rollout status deployment/target-cluster-agent --timeout="$ROLLOUT_TIMEOUT"
kubectl --context "$TARGET_CONTEXT" --namespace ai-agent-system rollout status daemonset/network-inventory-agent --timeout="$ROLLOUT_TIMEOUT"

printf '%s\n' "DUAL_AGENT_DEPLOYMENT_READY"
