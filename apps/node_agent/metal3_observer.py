import re
from typing import Any, Callable


class Metal3ObservationError(Exception):
    pass


def _valid_name(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value))


def observe_cluster_metal3(payload: dict[str, Any], get_item: Callable[[str], dict[str, Any]], list_items: Callable[[str], list[dict[str, Any]]]) -> dict[str, Any]:
    cluster_name = payload.get("cluster_name")
    namespace = payload.get("namespace", "default")
    host_namespace = payload.get("baremetal_host_namespace", namespace)
    if not _valid_name(cluster_name):
        raise Metal3ObservationError("INVALID_CLUSTER_NAME")
    if not _valid_name(namespace) or not _valid_name(host_namespace):
        raise Metal3ObservationError("INVALID_NAMESPACE")
    groups = {item.get("name"): item for item in get_item("/apis").get("groups", []) if isinstance(item, dict)}
    versions = {}
    for group_name in ("cluster.x-k8s.io", "infrastructure.cluster.x-k8s.io", "metal3.io"):
        group = groups.get(group_name)
        version = group.get("preferredVersion", {}).get("version") if isinstance(group, dict) else None
        if not isinstance(version, str) or not re.fullmatch(r"v[0-9]+(alpha[0-9]+|beta[0-9]+)?", version):
            return {"ready": False, "cluster_name": cluster_name, "namespace": namespace, "machines": [], "blockers": ["required_api_group_unavailable:" + group_name], "side_effects": False, "ironic": {"status": "not_configured"}}
        versions[group_name] = version
    capi, infra, metal3 = (versions["cluster.x-k8s.io"], versions["infrastructure.cluster.x-k8s.io"], versions["metal3.io"])
    machines = list_items(f"/apis/cluster.x-k8s.io/{capi}/namespaces/{namespace}/machines")
    hosts = list_items(f"/apis/metal3.io/{metal3}/namespaces/{host_namespace}/baremetalhosts")
    selected = [item for item in machines if isinstance(item, dict) and item.get("spec", {}).get("clusterName") == cluster_name]
    results, blockers, seen_uids = [], [], set()
    for machine in selected:
        md = machine.get("metadata", {})
        name, uid = md.get("name"), md.get("uid")
        if not _valid_name(name) or not isinstance(uid, str) or not uid or uid in seen_uids:
            raise Metal3ObservationError("CAPI_MACHINE_IDENTITY_INVALID")
        seen_uids.add(uid)
        ref = machine.get("spec", {}).get("infrastructureRef")
        if not isinstance(ref, dict):
            blocker = "machine_infrastructure_ref_missing"
            results.append({"machine": name, "machine_uid": uid, "status": "unresolved", "blocker": blocker})
            blockers.append(blocker)
            continue
        if ref.get("kind") != "Metal3Machine" or ref.get("apiVersion") != "infrastructure.cluster.x-k8s.io/" + infra or not _valid_name(ref.get("name")) or ref.get("namespace", namespace) != namespace:
            blocker = "machine_infrastructure_ref_invalid_or_cross_namespace"
            results.append({"machine": name, "machine_uid": uid, "status": "unresolved", "blocker": blocker})
            blockers.append(blocker)
            continue
        mm_name = ref["name"]
        path = f"/apis/infrastructure.cluster.x-k8s.io/{infra}/namespaces/{namespace}/metal3machines/{mm_name}"
        try:
            mm = get_item(path)
        except Exception as exc:
            if str(exc) != "KUBERNETES_API_HTTP_404":
                raise
            blocker = "metal3_machine_not_found"
            results.append({"machine": name, "machine_uid": uid, "metal3_machine": mm_name, "status": "unresolved", "blocker": blocker})
            blockers.append(blocker)
            continue
        mm_meta = mm.get("metadata", {})
        mm_uid = mm_meta.get("uid")
        if mm_meta.get("name") != mm_name or mm_meta.get("namespace") != namespace or not isinstance(mm_uid, str) or not mm_uid:
            raise Metal3ObservationError("METAL3_MACHINE_IDENTITY_MISMATCH")
        matches = []
        for host in hosts:
            consumer = host.get("spec", {}).get("consumerRef")
            if isinstance(consumer, dict) and consumer.get("kind") == "Metal3Machine" and consumer.get("apiVersion") == "infrastructure.cluster.x-k8s.io/" + infra and consumer.get("name") == mm_name and consumer.get("namespace", host_namespace) == namespace and consumer.get("uid") == mm_uid:
                matches.append(host)
        if len(matches) != 1:
            blocker = "baremetal_host_consumer_not_found" if not matches else "baremetal_host_consumer_ambiguous"
            results.append({"machine": name, "machine_uid": uid, "metal3_machine": mm_name, "metal3_machine_uid": mm_uid, "status": "unresolved", "blocker": blocker})
            blockers.append(blocker)
            continue
        host = matches[0]
        hmeta, spec, status = host.get("metadata", {}), host.get("spec", {}), host.get("status", {})
        provisioning = status.get("provisioning", {})
        state = provisioning.get("state") if isinstance(provisioning, dict) else None
        operational = status.get("operationalStatus")
        has_error = bool(status.get("error")) or bool(status.get("errorMessage"))
        ready = spec.get("online") is True and state in ("provisioned", "externally provisioned") and operational in (None, "OK") and not has_error
        blocker = None if ready else "baremetal_host_not_provisioned_or_unhealthy"
        if blocker:
            blockers.append(blocker)
        results.append({"machine": name, "machine_uid": uid, "metal3_machine": mm_name, "metal3_machine_uid": mm_uid, "baremetal_host": hmeta.get("name"), "baremetal_host_uid": hmeta.get("uid"), "baremetal_host_namespace": hmeta.get("namespace"), "baremetal_host_online": spec.get("online") is True, "baremetal_host_provisioning_state": state, "baremetal_host_operational_status": operational, "baremetal_host_has_error": has_error, "status": "linked" if ready else "linked_unhealthy", "blocker": blocker})
    if not selected:
        blockers.append("cluster_has_no_capi_machines")
    return {"ready": bool(selected) and not blockers, "cluster_name": cluster_name, "namespace": namespace, "baremetal_host_namespace": host_namespace, "machine_count": len(selected), "resolved_machine_count": sum(1 for item in results if item["status"] == "linked"), "machines": results[:1000], "blockers": sorted(set(blockers)), "side_effects": False, "ironic": {"status": "not_configured", "reason": "No authenticated Ironic API adapter is configured; BMH status is not proof of direct Ironic API health"}}
