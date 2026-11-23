"""Deploy the cloud-control, EPH and target agents to three clusters.

Python replacement for the former ``deploy/agents/deploy.sh``. Same inputs
(environment variables), same validation error codes, same ordering: every
manifest is server-side dry-run rendered before it is applied, and the three
rollouts must settle before ``THREE_AGENT_DEPLOYMENT_READY`` is printed.

Usage:
    python3 deploy/agents/deploy_agents.py
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
QUANTITY_PATTERN = re.compile(r"^[0-9]+(Mi|Gi|Ti)$")
IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9._/:@-]+@sha256:[a-f0-9]{64}$")

SYSTEM_NAMESPACE = "ai-agent-system"
SOURCE_OPERATOR_SECRET = "target-agent-source-operator-token"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def fail(code: str) -> None:
    print(code, file=sys.stderr)
    raise SystemExit(1)


def require(condition: bool, code: str) -> None:
    if not condition:
        fail(code)


def kubectl(
    context: str,
    *args: str,
    namespace: str | None = None,
    check: bool = True,
    capture: bool = False,
    stdin: str | None = None,
) -> str:
    cmd = ["kubectl", "--context", context]
    if namespace:
        cmd.extend(["--namespace", namespace])
    cmd.extend(args)
    result = subprocess.run(
        cmd,
        check=False,
        text=True,
        input=stdin,
        capture_output=True,
    )
    if check and result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(result.returncode)
    return result.stdout if capture else ""


def require_context(context: str, code: str) -> None:
    names = kubectl(context, "config", "get-contexts", "-o", "name", capture=True)
    if context not in [line.strip() for line in names.splitlines()]:
        fail(code)
    kubectl(context, "get", "--raw=/readyz")


def require_secret_keys(context: str, secret: str, *keys: str) -> None:
    template = '{{range $key, $value := .data}}{{printf "%s\\n" $key}}{{end}}'
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "--namespace",
            SYSTEM_NAMESPACE,
            "get",
            "secret",
            secret,
            "-o",
            f"go-template={template}",
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        fail(f"REQUIRED_SECRET_NOT_FOUND:{context}:{secret}")
    present = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    for key in keys:
        if key not in present:
            fail(f"REQUIRED_SECRET_KEY_MISSING:{context}:{secret}:{key}")


SOURCE_OPERATOR_SECRET_MANIFEST = """apiVersion: v1
kind: Secret
metadata:
  name: target-agent-source-operator-token
  namespace: ai-agent-system
  annotations:
    kubernetes.io/service-account.name: target-agent-source-operator
type: kubernetes.io/service-account-token
"""


def create_target_source_operator_secret(
    eph_context: str, target_context: str
) -> None:
    kubectl(eph_context, "apply", "-f", "-", stdin=SOURCE_OPERATOR_SECRET_MANIFEST)
    token = ""
    ca_data = ""
    for _ in range(30):
        token = _secret_field(eph_context, "token")
        ca_data = _secret_field(eph_context, "ca.crt")
        if token and ca_data:
            break
        time.sleep(2)
    require(bool(token), "EPH_SOURCE_OPERATOR_TOKEN_NOT_READY")
    require(bool(ca_data), "EPH_SOURCE_OPERATOR_CA_NOT_READY")
    server = kubectl(
        eph_context,
        "config",
        "view",
        "--minify",
        "-o",
        "jsonpath={.clusters[0].cluster.server}",
        capture=True,
    ).strip()
    require(bool(server), "EPH_API_SERVER_ENDPOINT_MISSING")
    with tempfile.TemporaryDirectory() as tmpdir:
        ca_path = Path(tmpdir) / "ca.crt"
        ca_path.write_bytes(base64.b64decode(ca_data))
        kubeconfig = Path(tmpdir) / "source.kubeconfig"
        kubectl(
            eph_context,
            "config",
            f"--kubeconfig={kubeconfig}",
            "set-cluster",
            "eph-source",
            f"--server={server}",
            f"--certificate-authority={ca_path}",
            "--embed-certs=true",
        )
        kubectl(
            eph_context,
            "config",
            f"--kubeconfig={kubeconfig}",
            "set-credentials",
            "target-agent-source-operator",
            f"--token={token}",
        )
        kubectl(
            eph_context,
            "config",
            f"--kubeconfig={kubeconfig}",
            "set-context",
            "target-agent-source-operator",
            "--cluster=eph-source",
            "--user=target-agent-source-operator",
        )
        kubectl(
            eph_context,
            "config",
            f"--kubeconfig={kubeconfig}",
            "use-context",
            "target-agent-source-operator",
        )
        kubeconfig.chmod(0o600)
        rendered = kubectl(
            target_context,
            "create",
            "secret",
            "generic",
            "target-agent-source-operator",
            f"--from-file=kubeconfig={kubeconfig}",
            "--dry-run=client",
            "-o",
            "yaml",
            namespace=SYSTEM_NAMESPACE,
            capture=True,
        )
        kubectl(target_context, "apply", "-f", "-", stdin=rendered)


def _secret_field(context: str, field: str) -> str:
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "--namespace",
            SYSTEM_NAMESPACE,
            "get",
            "secret",
            SOURCE_OPERATOR_SECRET,
            "-o",
            f"jsonpath={{.data.{field.replace('.', chr(92) + '.')}}}",
        ],
        text=True,
        capture_output=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def mark_eph_cleanup_resources(
    context: str,
    kind: str,
    name: str,
    deployment_id: str,
    namespace: str = SYSTEM_NAMESPACE,
) -> None:
    scope = [] if kind in {"clusterrole", "clusterrolebinding"} else ["--namespace", namespace]
    kubectl(
        context,
        *scope,
        "label",
        kind,
        name,
        "app.kubernetes.io/managed-by=ai-agent-stack",
        "ai-agent-stack.io/eph-cleanup=true",
        f"ai-agent-stack.io/deployment-id={deployment_id}",
        "--overwrite",
    )
    kubectl(
        context,
        *scope,
        "annotate",
        kind,
        name,
        "ai-agent-stack.io/eph-cleanup=true",
        "--overwrite",
    )


def mark_eph_agent_pod_template(eph_context: str, deployment_id: str) -> None:
    patch = json.dumps(
        {
            "spec": {
                "template": {
                    "metadata": {
                        "labels": {
                            "app.kubernetes.io/managed-by": "ai-agent-stack",
                            "ai-agent-stack.io/eph-cleanup": "true",
                            "ai-agent-stack.io/deployment-id": deployment_id,
                        },
                        "annotations": {"ai-agent-stack.io/eph-cleanup": "true"},
                    }
                }
            }
        }
    )
    kubectl(
        eph_context,
        "patch",
        "deployment",
        "eph-agent",
        "--type",
        "merge",
        "--patch",
        patch,
        namespace=SYSTEM_NAMESPACE,
    )


def render_apply(
    context: str,
    manifest: Path,
    image: str,
    placeholder: str,
    mode: str,
    target_capi_namespace: str,
    sriov_device_plugin_namespace: str,
    software_workspace_size: str,
) -> None:
    rendered = manifest.read_text(encoding="utf-8")
    for needle, replacement in (
        (placeholder, image),
        ("REPLACE_WITH_TARGET_CAPI_NAMESPACE", target_capi_namespace),
        ("REPLACE_WITH_SRIOV_DEVICE_PLUGIN_NAMESPACE", sriov_device_plugin_namespace),
        ("REPLACE_WITH_SOFTWARE_WORKSPACE_SIZE", software_workspace_size),
    ):
        rendered = rendered.replace(needle, replacement)
    args = ["apply", "-f", "-"]
    if mode == "dry-run":
        args.insert(1, "--dry-run=server")
    kubectl(context, *args, stdin=rendered)


def main() -> None:
    cloud_context = env("CLOUD_CONTEXT")
    eph_context = env("EPH_CONTEXT")
    target_context = env("TARGET_CONTEXT")
    cloud_control_image = env("CLOUD_CONTROL_IMAGE")
    eph_agent_image = env("EPH_AGENT_IMAGE")
    target_agent_image = env("TARGET_AGENT_IMAGE")
    target_capi_namespace = env("TARGET_CAPI_NAMESPACE", "metal3")
    sriov_device_plugin_namespace = env(
        "SRIOV_DEVICE_PLUGIN_NAMESPACE", target_capi_namespace
    )
    deployment_id = env("DEPLOYMENT_ID")
    rollout_timeout = env("ROLLOUT_TIMEOUT", "10m")
    software_workspace_size = env("SOFTWARE_WORKSPACE_SIZE", "1Gi")

    require(bool(cloud_context), "CLOUD_CONTEXT_REQUIRED")
    require(bool(eph_context), "EPH_CONTEXT_REQUIRED")
    require(bool(target_context), "TARGET_CONTEXT_REQUIRED")
    require(
        bool(DNS_LABEL_PATTERN.match(target_capi_namespace))
        and len(target_capi_namespace) <= 63,
        "TARGET_CAPI_NAMESPACE_MUST_BE_A_DNS_LABEL",
    )
    require(
        bool(DNS_LABEL_PATTERN.match(sriov_device_plugin_namespace))
        and len(sriov_device_plugin_namespace) <= 63,
        "SRIOV_DEVICE_PLUGIN_NAMESPACE_MUST_BE_A_DNS_LABEL",
    )
    require(
        bool(DNS_LABEL_PATTERN.match(deployment_id)) and len(deployment_id) <= 63,
        "DEPLOYMENT_ID_MUST_BE_A_DNS_LABEL",
    )
    require(
        bool(QUANTITY_PATTERN.match(software_workspace_size)),
        "SOFTWARE_WORKSPACE_SIZE_MUST_BE_A_KUBERNETES_QUANTITY",
    )
    require(cloud_context != eph_context, "CLOUD_AND_EPH_CONTEXTS_MUST_DIFFER")
    require(cloud_context != target_context, "CLOUD_AND_TARGET_CONTEXTS_MUST_DIFFER")
    require(eph_context != target_context, "EPH_AND_TARGET_CONTEXTS_MUST_DIFFER")
    require(
        bool(IMAGE_PATTERN.match(cloud_control_image)),
        "CLOUD_CONTROL_IMAGE_MUST_BE_PINNED_BY_SHA256_DIGEST",
    )
    require(
        bool(IMAGE_PATTERN.match(eph_agent_image)),
        "EPH_AGENT_IMAGE_MUST_BE_PINNED_BY_SHA256_DIGEST",
    )
    require(
        bool(IMAGE_PATTERN.match(target_agent_image)),
        "TARGET_AGENT_IMAGE_MUST_BE_PINNED_BY_SHA256_DIGEST",
    )
    eph_local_registry = eph_agent_image.split("/", 1)[0]
    require(
        target_agent_image.split("/", 1)[0] == eph_local_registry,
        "TARGET_AGENT_IMAGE_MUST_USE_EPH_LOCAL_REGISTRY",
    )
    require(
        cloud_control_image.split("/", 1)[0] == eph_local_registry,
        "CLOUD_CONTROL_IMAGE_MUST_USE_EPH_LOCAL_REGISTRY",
    )

    requires = {
        cloud_context: "KUBECTL_CONTEXT_NOT_FOUND",
        eph_context: "KUBECTL_CONTEXT_NOT_FOUND",
        target_context: "KUBECTL_CONTEXT_NOT_FOUND",
    }
    for context, code in requires.items():
        require_context(context, f"{code}:{context}")

    namespace_manifest = SCRIPT_DIR / "namespace.yaml"
    for context in (cloud_context, eph_context, target_context):
        kubectl(context, "apply", "-f", str(namespace_manifest))

    require_secret_keys(cloud_context, "cloud-control-agent-database", "database-url")
    require_secret_keys(
        cloud_context, "cloud-control-agent-mtls", "tls.crt", "tls.key", "client-ca.crt"
    )
    require_secret_keys(
        eph_context, "eph-agent-credentials", "control-url", "agent-id", "installation-id"
    )
    require_secret_keys(
        eph_context, "agent-control-plane-mtls", "ca.crt", "tls.crt", "tls.key"
    )
    require_secret_keys(eph_context, "capi-pivot-source-kubeconfig", "kubeconfig")
    require_secret_keys(
        target_context,
        "target-agent-credentials",
        "control-url",
        "agent-id",
        "installation-id",
        "cluster-id",
        "ironic-api-url",
        "ironic-api-token",
        "ironic-internal-ntp-ip",
    )
    require_secret_keys(
        target_context, "agent-control-plane-mtls", "ca.crt", "tls.crt", "tls.key"
    )

    cloud_manifest = SCRIPT_DIR / "cloud-control-agent.yaml"
    eph_manifest = SCRIPT_DIR / "eph-agent.yaml"
    target_manifest = SCRIPT_DIR / "target-cluster-agent.yaml"

    render_apply(
        cloud_context,
        cloud_manifest,
        cloud_control_image,
        "REPLACE_WITH_REGISTRY/ai-agent-cloud-control-agent:VERSION",
        "dry-run",
        target_capi_namespace,
        sriov_device_plugin_namespace,
        software_workspace_size,
    )
    render_apply(
        eph_context,
        eph_manifest,
        eph_agent_image,
        "REPLACE_WITH_REGISTRY/ai-agent-eph-agent:VERSION",
        "dry-run",
        target_capi_namespace,
        sriov_device_plugin_namespace,
        software_workspace_size,
    )

    render_apply(
        cloud_context,
        cloud_manifest,
        cloud_control_image,
        "REPLACE_WITH_REGISTRY/ai-agent-cloud-control-agent:VERSION",
        "apply",
        target_capi_namespace,
        sriov_device_plugin_namespace,
        software_workspace_size,
    )
    render_apply(
        eph_context,
        eph_manifest,
        eph_agent_image,
        "REPLACE_WITH_REGISTRY/ai-agent-eph-agent:VERSION",
        "apply",
        target_capi_namespace,
        sriov_device_plugin_namespace,
        software_workspace_size,
    )

    create_target_source_operator_secret(eph_context, target_context)
    require_secret_keys(target_context, "target-agent-source-operator", "kubeconfig")
    render_apply(
        target_context,
        target_manifest,
        target_agent_image,
        "REPLACE_WITH_REGISTRY/ai-agent-target-agent:VERSION",
        "dry-run",
        target_capi_namespace,
        sriov_device_plugin_namespace,
        software_workspace_size,
    )
    render_apply(
        target_context,
        target_manifest,
        target_agent_image,
        "REPLACE_WITH_REGISTRY/ai-agent-target-agent:VERSION",
        "apply",
        target_capi_namespace,
        sriov_device_plugin_namespace,
        software_workspace_size,
    )

    cleanup_targets = (
        ("deployment", "eph-agent", SYSTEM_NAMESPACE),
        ("serviceaccount", "eph-agent", SYSTEM_NAMESPACE),
        ("clusterrole", "eph-agent-preflight", SYSTEM_NAMESPACE),
        ("clusterrolebinding", "eph-agent-preflight", SYSTEM_NAMESPACE),
        ("role", "eph-agent-metal3-deployer", "metal3"),
        ("rolebinding", "eph-agent-metal3-deployer", "metal3"),
        ("serviceaccount", "target-agent-source-operator", SYSTEM_NAMESPACE),
        ("clusterrole", "target-agent-source-operator", SYSTEM_NAMESPACE),
        ("clusterrolebinding", "target-agent-source-operator", SYSTEM_NAMESPACE),
        ("role", "target-agent-source-capi", "metal3"),
        ("rolebinding", "target-agent-source-capi", "metal3"),
        ("role", "target-agent-source-cleanup", SYSTEM_NAMESPACE),
        ("rolebinding", "target-agent-source-cleanup", SYSTEM_NAMESPACE),
    )
    for kind, name, namespace in cleanup_targets:
        mark_eph_cleanup_resources(eph_context, kind, name, deployment_id, namespace)

    mark_eph_agent_pod_template(eph_context, deployment_id)

    for secret in (
        "eph-agent-credentials",
        "agent-control-plane-mtls",
        "capi-pivot-source-kubeconfig",
        SOURCE_OPERATOR_SECRET,
    ):
        mark_eph_cleanup_resources(eph_context, "secret", secret, deployment_id)
        kubectl(
            eph_context,
            "annotate",
            "secret",
            secret,
            "ai-agent-stack.io/credential-scope=eph-agent",
            "--overwrite",
            namespace=SYSTEM_NAMESPACE,
        )
    if (
        subprocess.run(
            [
                "kubectl",
                "--context",
                eph_context,
                "--namespace",
                SYSTEM_NAMESPACE,
                "get",
                "secret",
                "capi-pivot-target-kubeconfig",
            ],
            capture_output=True,
        ).returncode
        == 0
    ):
        mark_eph_cleanup_resources(
            eph_context, "secret", "capi-pivot-target-kubeconfig", deployment_id
        )
        kubectl(
            eph_context,
            "annotate",
            "secret",
            "capi-pivot-target-kubeconfig",
            "ai-agent-stack.io/credential-scope=eph-agent",
            "--overwrite",
            namespace=SYSTEM_NAMESPACE,
        )
    mark_eph_cleanup_resources(
        target_context, "secret", "target-agent-source-operator", deployment_id
    )
    kubectl(
        target_context,
        "annotate",
        "secret",
        "target-agent-source-operator",
        "ai-agent-stack.io/credential-scope=target-agent",
        "--overwrite",
        namespace=SYSTEM_NAMESPACE,
    )

    for context, deployment in (
        (cloud_context, "cloud-control-agent"),
        (eph_context, "eph-agent"),
        (target_context, "target-agent"),
    ):
        kubectl(
            context,
            "rollout",
            "status",
            f"deployment/{deployment}",
            f"--timeout={rollout_timeout}",
            namespace=SYSTEM_NAMESPACE,
        )

    print("THREE_AGENT_DEPLOYMENT_READY")


if __name__ == "__main__":
    main()
