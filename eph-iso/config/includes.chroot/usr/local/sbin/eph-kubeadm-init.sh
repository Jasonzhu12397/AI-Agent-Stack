#!/usr/bin/env bash
set -euo pipefail

KUBERNETES_VERSION="${KUBERNETES_VERSION:-}"
POD_CIDR="${EPH_POD_CIDR:-10.244.0.0/16}"
CRI_SOCKET="unix:///run/containerd/containerd.sock"
FLANNEL_MANIFEST="/usr/share/ai-agent-studio/kube-flannel.yml"

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

[[ "$KUBERNETES_VERSION" =~ ^v1\.(33|34|35|36)\.[0-9]+$ ]] || fail "KUBERNETES_VERSION_INVALID"
command -v kubeadm >/dev/null 2>&1 || fail "KUBEADM_BINARY_MISSING"
command -v kubectl >/dev/null 2>&1 || fail "KUBECTL_BINARY_MISSING"
command -v kubelet >/dev/null 2>&1 || fail "KUBELET_BINARY_MISSING"
command -v containerd >/dev/null 2>&1 || fail "CONTAINERD_BINARY_MISSING"
[ -s "$FLANNEL_MANIFEST" ] || fail "FLANNEL_MANIFEST_MISSING"
[ -x /usr/lib/cni/bridge ] || fail "CNI_BRIDGE_PLUGIN_MISSING"
[ -x /usr/lib/cni/host-local ] || fail "CNI_HOST_LOCAL_PLUGIN_MISSING"
[ -x /usr/lib/cni/loopback ] || fail "CNI_LOOPBACK_PLUGIN_MISSING"
install -d -m 0755 /opt/cni
if [ ! -e /opt/cni/bin ]; then
    ln -s /usr/lib/cni /opt/cni/bin
fi
[ -x /opt/cni/bin/bridge ] || fail "CNI_PLUGIN_PATH_INVALID"

swapoff -a
modprobe overlay
modprobe br_netfilter
sysctl -w net.bridge.bridge-nf-call-iptables=1
sysctl -w net.bridge.bridge-nf-call-ip6tables=1
sysctl -w net.ipv4.ip_forward=1

install -d -m 0755 /etc/containerd /etc/kubernetes
containerd config default > /etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sed -i -E 's#sandbox_image = "registry.k8s.io/pause:[^"]+"#sandbox_image = "registry.k8s.io/pause:3.10"#' /etc/containerd/config.toml
systemctl restart containerd
systemctl enable containerd kubelet

if [ ! -s /etc/kubernetes/admin.conf ]; then
    kubeadm init \
        --kubernetes-version "$KUBERNETES_VERSION" \
        --pod-network-cidr "$POD_CIDR" \
        --cri-socket "$CRI_SOCKET" \
        --skip-token-print
fi

export KUBECONFIG=/etc/kubernetes/admin.conf
for attempt in $(seq 1 60); do
    if kubectl get --raw=/readyz 2>/dev/null | grep -qx ok; then
        break
    fi
    if [ "$attempt" -eq 60 ]; then
        fail "KUBERNETES_API_NOT_READY"
    fi
    sleep 5
done

kubectl apply -f "$FLANNEL_MANIFEST"
kubectl taint nodes --all node-role.kubernetes.io/control-plane- || true
kubectl wait --for=condition=Ready nodes --all --timeout=300s
