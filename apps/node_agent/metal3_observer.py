import re
from collections.abc import Callable
from typing import Any


class Metal3ObservationError(Exception):
    pass


def _valid_name(value: Any) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value)
    )


def observe_cluster_metal3(
    payload: dict[str, Any],
    get_item: Callable[[str], dict[str, Any]],
    list_items: Callable[[str], list[dict[str, Any]]],
    ironic_lookup: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cluster_name = payload.get("cluster_name")
    namespace = payload.get("namespace", "default")
    host_namespace = payload.get("baremetal_host_namespace", namespace)
    if not _valid_name(cluster_name):
        raise Metal3ObservationError("INVALID_CLUSTER_NAME")
    if not _valid_name(namespace) or not _valid_name(host_namespace):
        raise Metal3ObservationError("INVALID_NAMESPACE")

    discovery = get_item("/apis")
    groups = {
        item.get("name"): item
        for item in discovery.get("groups", [])
        if isinstance(item, dict)
    }
    versions = {}
    required_groups = (
        "cluster.x-k8s.io",
        "infrastructure.cluster.x-k8s.io",
        "metal3.io",
    )
    for group_name in required_groups:
        group = groups.get(group_name)
        version = (
            group.get("preferredVersion", {}).get("version")
            if isinstance(group, dict)
            else None
        )
        if not isinstance(version, str) or not re.fullmatch(
            r"v[0-9]+(alpha[0-9]+|beta[0-9]+)?", version
        ):
            return {
                "ready": False,
                "cluster_name": cluster_name,
                "namespace": namespace,
                "machines": [],
                "blockers": ["required_api_group_unavailable:" + group_name],
                "side_effects": False,
                "ironic": {"status": "not_configured"},
            }
        versions[group_name] = version

    capi_version = versions["cluster.x-k8s.io"]
    infra_version = versions["infrastructure.cluster.x-k8s.io"]
    metal3_version = versions["metal3.io"]
    machine_path = (
        f"/apis/cluster.x-k8s.io/{capi_version}"
        f"/namespaces/{namespace}/machines"
    )
    host_path = (
        f"/apis/metal3.io/{metal3_version}"
        f"/namespaces/{host_namespace}/baremetalhosts"
    )
    machines = list_items(machine_path)
    hosts = list_items(host_path)
    nodes = list_items("/api/v1/nodes")
    nodes_by_name = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise Metal3ObservationError("WORKLOAD_NODE_INVENTORY_INVALID")
        node_metadata = node.get("metadata", {})
        node_name = node_metadata.get("name") if isinstance(node_metadata, dict) else None
        node_uid = node_metadata.get("uid") if isinstance(node_metadata, dict) else None
        if (
            not _valid_name(node_name)
            or not isinstance(node_uid, str)
            or not node_uid
            or node_name in nodes_by_name
        ):
            raise Metal3ObservationError("WORKLOAD_NODE_IDENTITY_INVALID")
        nodes_by_name[node_name] = node
    selected = [
        item
        for item in machines
        if isinstance(item, dict)
        and item.get("spec", {}).get("clusterName") == cluster_name
    ]
    results = []
    blockers = []
    seen_uids = set()
    ironic_applicable_count = 0
    ironic_verified_count = 0

    for machine in selected:
        metadata = machine.get("metadata", {})
        machine_name = metadata.get("name")
        machine_uid = metadata.get("uid")
        if (
            not _valid_name(machine_name)
            or not isinstance(machine_uid, str)
            or not machine_uid
            or machine_uid in seen_uids
        ):
            raise Metal3ObservationError("CAPI_MACHINE_IDENTITY_INVALID")
        seen_uids.add(machine_uid)

        infrastructure_ref = machine.get("spec", {}).get("infrastructureRef")
        if not isinstance(infrastructure_ref, dict):
            blocker = "machine_infrastructure_ref_missing"
            results.append(
                {
                    "machine": machine_name,
                    "machine_uid": machine_uid,
                    "status": "unresolved",
                    "blocker": blocker,
                }
            )
            blockers.append(blocker)
            continue

        expected_api_version = "infrastructure.cluster.x-k8s.io/" + infra_version
        valid_ref = (
            infrastructure_ref.get("kind") == "Metal3Machine"
            and infrastructure_ref.get("apiVersion") == expected_api_version
            and _valid_name(infrastructure_ref.get("name"))
            and infrastructure_ref.get("namespace", namespace) == namespace
        )
        if not valid_ref:
            blocker = "machine_infrastructure_ref_invalid_or_cross_namespace"
            results.append(
                {
                    "machine": machine_name,
                    "machine_uid": machine_uid,
                    "status": "unresolved",
                    "blocker": blocker,
                }
            )
            blockers.append(blocker)
            continue

        metal3_machine_name = infrastructure_ref["name"]
        metal3_machine_path = (
            f"/apis/infrastructure.cluster.x-k8s.io/{infra_version}"
            f"/namespaces/{namespace}/metal3machines/{metal3_machine_name}"
        )
        try:
            metal3_machine = get_item(metal3_machine_path)
        except Exception as exc:
            if str(exc) != "KUBERNETES_API_HTTP_404":
                raise
            blocker = "metal3_machine_not_found"
            results.append(
                {
                    "machine": machine_name,
                    "machine_uid": machine_uid,
                    "metal3_machine": metal3_machine_name,
                    "status": "unresolved",
                    "blocker": blocker,
                }
            )
            blockers.append(blocker)
            continue

        mm_metadata = metal3_machine.get("metadata", {})
        mm_uid = mm_metadata.get("uid")
        if (
            mm_metadata.get("name") != metal3_machine_name
            or mm_metadata.get("namespace") != namespace
            or not isinstance(mm_uid, str)
            or not mm_uid
        ):
            raise Metal3ObservationError("METAL3_MACHINE_IDENTITY_MISMATCH")

        machine_spec = machine.get("spec", {})
        machine_status = machine.get("status", {})
        metal3_spec = metal3_machine.get("spec", {})
        metal3_status = metal3_machine.get("status", {})
        machine_phase = machine_status.get("phase")
        node_ref = machine_status.get("nodeRef", {})
        node_name = node_ref.get("name") if isinstance(node_ref, dict) else None
        machine_provider_id = machine_spec.get("providerID")
        metal3_provider_id = metal3_spec.get("providerID")
        machine_blockers = []
        if machine_phase != "Running":
            machine_blockers.append("capi_machine_not_running")
        if metal3_status.get("ready") is not True:
            machine_blockers.append("metal3_machine_not_ready")
        if (
            not isinstance(machine_provider_id, str)
            or not machine_provider_id
            or not isinstance(metal3_provider_id, str)
            or not metal3_provider_id
            or machine_provider_id != metal3_provider_id
        ):
            machine_blockers.append("capi_metal3_provider_id_mismatch")
        workload_node = nodes_by_name.get(node_name) if _valid_name(node_name) else None
        if workload_node is None:
            machine_blockers.append("workload_node_reference_not_found")
            node_provider_id = None
            node_ready = False
        else:
            node_spec = workload_node.get("spec", {})
            node_status = workload_node.get("status", {})
            node_provider_id = node_spec.get("providerID")
            node_conditions = node_status.get("conditions", [])
            ready_condition = next(
                (
                    item for item in node_conditions
                    if isinstance(item, dict) and item.get("type") == "Ready"
                ),
                {},
            )
            node_ready = ready_condition.get("status") == "True"
            if node_provider_id != machine_provider_id:
                machine_blockers.append("workload_node_provider_id_mismatch")
            if not node_ready:
                machine_blockers.append("workload_node_not_ready")
        blockers.extend(machine_blockers)

        matching_hosts = []
        for host in hosts:
            consumer = host.get("spec", {}).get("consumerRef")
            if not isinstance(consumer, dict):
                continue
            consumer_matches = (
                consumer.get("kind") == "Metal3Machine"
                and consumer.get("apiVersion") == expected_api_version
                and consumer.get("name") == metal3_machine_name
                and consumer.get("namespace", namespace) == namespace
                and consumer.get("uid") == mm_uid
            )
            if consumer_matches:
                matching_hosts.append(host)

        if len(matching_hosts) != 1:
            blocker = (
                "baremetal_host_consumer_not_found"
                if not matching_hosts
                else "baremetal_host_consumer_ambiguous"
            )
            results.append(
                {
                    "machine": machine_name,
                    "machine_uid": machine_uid,
                    "metal3_machine": metal3_machine_name,
                    "metal3_machine_uid": mm_uid,
                    "status": "unresolved",
                    "blocker": blocker,
                }
            )
            blockers.append(blocker)
            continue

        host = matching_hosts[0]
        host_metadata = host.get("metadata", {})
        host_spec = host.get("spec", {})
        host_status = host.get("status", {})
        host_name = host_metadata.get("name")
        host_uid = host_metadata.get("uid")
        if (
            not _valid_name(host_name)
            or not isinstance(host_uid, str)
            or not host_uid
            or host_metadata.get("namespace") != host_namespace
        ):
            raise Metal3ObservationError("BAREMETAL_HOST_IDENTITY_INVALID")
        provisioning = host_status.get("provisioning", {})
        provisioning_state = (
            provisioning.get("state") if isinstance(provisioning, dict) else None
        )
        operational_status = host_status.get("operationalStatus")
        has_error = bool(host_status.get("error")) or bool(
            host_status.get("errorMessage")
        )
        host_ready = (
            host_spec.get("online") is True
            and provisioning_state in ("provisioned", "externally provisioned")
            and operational_status == "OK"
            and not has_error
        )
        blocker = (
            None if host_ready else "baremetal_host_not_provisioned_or_unhealthy"
        )
        if blocker:
            blockers.append(blocker)

        ironic_node = None
        ironic_blocker = None
        if ironic_lookup is not None and provisioning_state == "provisioned":
            ironic_applicable_count += 1
            ironic_uuid = (
                provisioning.get("ID") or provisioning.get("id")
                if isinstance(provisioning, dict)
                else None
            )
            valid_uuid = isinstance(ironic_uuid, str) and bool(
                re.fullmatch(
                    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                    ironic_uuid,
                )
            )
            if not valid_uuid:
                ironic_blocker = "ironic_node_uuid_missing_or_invalid"
            else:
                try:
                    ironic_node = ironic_lookup(ironic_uuid)
                except Exception:
                    ironic_blocker = "ironic_api_unavailable_or_node_unreadable"
                if ironic_node is not None:
                    returned_uuid = ironic_node.get("uuid")
                    ironic_healthy = (
                        isinstance(returned_uuid, str)
                        and returned_uuid.lower() == ironic_uuid.lower()
                        and ironic_node.get("provision_state") == "active"
                        and ironic_node.get("maintenance") is False
                        and ironic_node.get("target_provision_state") in (None, "")
                        and ironic_node.get("has_last_error") is False
                    )
                    if ironic_healthy:
                        ironic_verified_count += 1
                    else:
                        ironic_blocker = "ironic_node_not_active_or_unhealthy"
            if ironic_blocker:
                blockers.append(ironic_blocker)

        results.append(
            {
                "machine": machine_name,
                "machine_uid": machine_uid,
                "metal3_machine": metal3_machine_name,
                "metal3_machine_uid": mm_uid,
                "baremetal_host": host_name,
                "baremetal_host_uid": host_uid,
                "baremetal_host_namespace": host_metadata.get("namespace"),
                "baremetal_host_online": host_spec.get("online") is True,
                "baremetal_host_provisioning_state": provisioning_state,
                "baremetal_host_operational_status": operational_status,
                "baremetal_host_has_error": has_error,
                "machine_phase": machine_phase,
                "metal3_machine_ready": metal3_status.get("ready") is True,
                "node_name": node_name,
                "node_uid": (
                    workload_node.get("metadata", {}).get("uid")
                    if isinstance(workload_node, dict)
                    else None
                ),
                "machine_provider_id": machine_provider_id,
                "metal3_machine_provider_id": metal3_provider_id,
                "node_provider_id": node_provider_id,
                "node_ready": node_ready,
                "machine_blockers": machine_blockers,
                "ironic_node": ironic_node,
                "ironic_blocker": ironic_blocker,
                "status": (
                    "linked"
                    if host_ready and not ironic_blocker and not machine_blockers
                    else "linked_unhealthy"
                ),
                "blocker": blocker,
            }
        )

    if not selected:
        blockers.append("cluster_has_no_capi_machines")
    resolved_count = sum(item["status"] == "linked" for item in results)
    return {
        "ready": bool(selected) and not blockers,
        "cluster_name": cluster_name,
        "namespace": namespace,
        "baremetal_host_namespace": host_namespace,
        "machine_count": len(selected),
        "resolved_machine_count": resolved_count,
        "machines": results[:1000],
        "blockers": sorted(set(blockers)),
        "side_effects": False,
        "ironic": {
            "status": (
                "not_configured"
                if ironic_lookup is None
                else "not_applicable"
                if ironic_applicable_count == 0
                else "verified"
                if ironic_verified_count == ironic_applicable_count
                else "partially_verified"
                if ironic_verified_count
                else "unhealthy"
            ),
            "applicable_node_count": ironic_applicable_count,
            "verified_node_count": ironic_verified_count,
            "reason": (
                "No authenticated Ironic API adapter is configured; BMH status "
                "is not proof of direct Ironic API health"
                if ironic_lookup is None
                else None
            ),
        },
    }
