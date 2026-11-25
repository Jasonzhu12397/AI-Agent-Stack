from pathlib import Path

import yaml

from apps.target_agent.write_permissions import (
    NETWORK_APPLY_PERMISSIONS,
    WORKER_PROVISION_PERMISSIONS,
)

MANIFEST = Path(__file__).resolve().parents[1] / "deploy/agents/target-cluster-agent.yaml"


def _rules(role_name: str) -> list[dict]:
    for document in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")):
        if not document:
            continue
        if document.get("metadata", {}).get("name") == role_name:
            return document.get("rules", [])
    raise AssertionError(f"{role_name} missing from the Target Agent manifest")


def _grants(rules: list[dict], verb: str, resource: str) -> bool:
    base_resource = resource.split(".", 1)[0]
    for rule in rules:
        resources = rule.get("resources", [])
        verbs = rule.get("verbs", [])
        if base_resource not in resources:
            continue
        if verb in verbs or "*" in verbs:
            return True
    return False


def test_target_operator_role_covers_every_namespaced_write_preflight():
    rules = _rules("target-agent-metal3-operator")
    required = {
        (item.verb, item.resource)
        for item in (*WORKER_PROVISION_PERMISSIONS, *NETWORK_APPLY_PERMISSIONS)
        if item.namespace_scoped
    }

    missing = sorted(
        f"{verb}:{resource}"
        for verb, resource in required
        if not _grants(rules, verb, resource)
    )
    assert missing == []


def test_target_storageclass_writer_covers_cluster_scoped_preflight():
    rules = _rules("target-agent-storageclass-writer")
    required = {
        (item.verb, item.resource)
        for item in NETWORK_APPLY_PERMISSIONS
        if not item.namespace_scoped
        and item.resource != "customresourcedefinitions.apiextensions.k8s.io"
    }

    missing = sorted(
        f"{verb}:{resource}"
        for verb, resource in required
        if not _grants(rules, verb, resource)
    )
    assert missing == []


def test_target_agent_never_gets_wildcard_permissions():
    for document in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")):
        if not document or document.get("kind") not in {"Role", "ClusterRole"}:
            continue
        for rule in document.get("rules", []):
            assert "*" not in rule.get("verbs", [])
            assert "*" not in rule.get("resources", [])
