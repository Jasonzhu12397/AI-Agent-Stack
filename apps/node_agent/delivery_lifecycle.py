from __future__ import annotations

import ipaddress
from typing import Any


class DeliveryLifecycleError(ValueError):
    pass


PHASES = (
    "eph_single_node_ready",
    "target_control_plane_ready",
    "target_agent_ready",
    "capi_pivot_verified",
    "bmh_registry_migrated",
    "eph_cleanup_verified",
    "worker_provisioning",
    "workers_ready",
    "completed",
)

NEXT_PHASE = {current: PHASES[index + 1] for index, current in enumerate(PHASES[:-1])}

REQUIRED_EVIDENCE = {
    "target_control_plane_ready": (
        "target_api_ready",
        "target_etcd_healthy",
        "target_core_network_ready",
        "target_control_plane_chronyd_verified",
        "target_internal_ntp_service_ready",
    ),
    "target_agent_ready": (
        "target_agent_on_control_plane",
        "target_agent_heartbeat",
        "checkpoint_restored",
    ),
    "capi_pivot_verified": (
        "target_capi_controllers_ready",
        "cluster_uid_preserved",
        "pivot_inventory_reconciled",
    ),
    "bmh_registry_migrated": (
        "source_bmh_inventory_complete",
        "target_bmh_inventory_complete",
        "bmh_identity_sets_match",
        "target_bmh_controller_owns_inventory",
    ),
    "eph_cleanup_verified": (
        "eph_agent_stopped",
        "eph_bootstrap_components_removed",
        "eph_credentials_revoked",
    ),
    "workers_ready": (
        "expected_workers_ready",
        "eph_chronyd_verified",
        "target_control_plane_chronyd_verified",
        "worker_chronyd_verified",
        "worker_network_postflight_passed",
    ),
    "completed": ("target_cluster_postflight_passed",),
}


def validate_topology(topology: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(topology, dict):
        raise DeliveryLifecycleError("DELIVERY_TOPOLOGY_INVALID")
    eph_nodes = topology.get("eph_kubernetes_node_count")
    eph_mode = topology.get("eph_mode")
    if (
        eph_mode != "single_node"
        or not isinstance(eph_nodes, int)
        or isinstance(eph_nodes, bool)
        or eph_nodes != 1
    ):
        raise DeliveryLifecycleError("EPH_MUST_BE_SINGLE_NODE")
    if topology.get("target_agent_placement") != "target_control_plane":
        raise DeliveryLifecycleError("TARGET_AGENT_MUST_RUN_ON_CONTROL_PLANE")
    if topology.get("chronyd_scope") != "eph_and_target_kubernetes_nodes":
        raise DeliveryLifecycleError("CHRONYD_SCOPE_MUST_INCLUDE_EPH_AND_TARGET_NODES")
    return {
        "valid": True,
        "eph_kubernetes_node_count": 1,
        "eph_mode": "single_node",
        "target_agent_placement": "target_control_plane",
        "chronyd_scope": "eph_and_target_kubernetes_nodes",
    }


def validate_chronyd_scope(node_role: str, action: str) -> dict[str, Any]:
    allowed_roles = {"eph", "target_control_plane", "target_worker"}
    allowed_actions = {"install", "configure", "enable", "verify"}
    if node_role not in allowed_roles:
        raise DeliveryLifecycleError("CHRONYD_NODE_ROLE_INVALID")
    if action not in allowed_actions:
        raise DeliveryLifecycleError("CHRONYD_ACTION_INVALID")
    return {
        "allowed": True,
        "node_role": node_role,
        "action": action,
        "required_before_kubeadm": node_role in {"eph", "target_control_plane"},
        "required_before_node_join": node_role == "target_worker",
    }



def validate_ntp_topology(
    node_role: str,
    configured_sources: list[str],
    *,
    source_scope: str,
    ironic_internal_ip: str | None = None,
) -> dict[str, Any]:
    if not isinstance(configured_sources, list) or not configured_sources:
        raise DeliveryLifecycleError("NTP_SOURCES_REQUIRED")
    if any(not isinstance(source, str) or not source.strip() for source in configured_sources):
        raise DeliveryLifecycleError("NTP_SOURCE_INVALID")
    sources = [source.strip() for source in configured_sources]
    if len(set(sources)) != len(sources):
        raise DeliveryLifecycleError("NTP_SOURCE_DUPLICATED")

    if node_role == "target_worker":
        if source_scope != "ironic_internal" or not ironic_internal_ip:
            raise DeliveryLifecycleError("WORKER_NTP_MUST_USE_IRONIC_INTERNAL_IP")
        try:
            expected_ip = ipaddress.ip_address(ironic_internal_ip)
            actual_ips = [ipaddress.ip_address(source) for source in sources]
        except ValueError as exc:
            raise DeliveryLifecycleError("WORKER_NTP_SOURCE_MUST_BE_IP") from exc
        if not expected_ip.is_private or actual_ips != [expected_ip]:
            raise DeliveryLifecycleError("WORKER_NTP_MUST_USE_ONLY_IRONIC_INTERNAL_IP")
        return {
            "valid": True,
            "node_role": node_role,
            "source_scope": source_scope,
            "sources": [str(expected_ip)],
            "external_ntp_allowed": False,
        }

    if node_role == "target_control_plane":
        if source_scope != "external":
            raise DeliveryLifecycleError("CONTROL_PLANE_NTP_MUST_USE_EXTERNAL_SOURCES")
        if not isinstance(ironic_internal_ip, str):
            raise DeliveryLifecycleError("CONTROL_PLANE_IRONIC_INTERNAL_IP_REQUIRED")
        try:
            internal_ip = ipaddress.ip_address(ironic_internal_ip)
        except ValueError as exc:
            raise DeliveryLifecycleError("IRONIC_INTERNAL_IP_INVALID") from exc
        if not internal_ip.is_private:
            raise DeliveryLifecycleError("IRONIC_INTERNAL_IP_MUST_BE_PRIVATE")
        return {
            "valid": True,
            "node_role": node_role,
            "source_scope": source_scope,
            "sources": sources,
            "internal_ntp_listener_ip": str(internal_ip),
            "serves_internal_worker_ntp": True,
        }

    if node_role == "eph":
        if source_scope != "bootstrap":
            raise DeliveryLifecycleError("EPH_NTP_BOOTSTRAP_SOURCE_REQUIRED")
        return {
            "valid": True,
            "node_role": node_role,
            "source_scope": source_scope,
            "sources": sources,
        }

    raise DeliveryLifecycleError("CHRONYD_NODE_ROLE_INVALID")


def evaluate_transition(
    current_phase: str,
    requested_phase: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if current_phase not in PHASES or requested_phase not in PHASES:
        raise DeliveryLifecycleError("DELIVERY_PHASE_UNKNOWN")
    expected = NEXT_PHASE.get(current_phase)
    if requested_phase != expected:
        raise DeliveryLifecycleError("DELIVERY_PHASE_TRANSITION_INVALID")
    if not isinstance(evidence, dict):
        raise DeliveryLifecycleError("DELIVERY_EVIDENCE_INVALID")
    missing = [
        key
        for key in REQUIRED_EVIDENCE.get(requested_phase, ())
        if evidence.get(key) is not True
    ]
    if missing:
        raise DeliveryLifecycleError("DELIVERY_EVIDENCE_INCOMPLETE:" + ",".join(missing))
    return {
        "accepted": True,
        "current_phase": current_phase,
        "next_phase": requested_phase,
        "missing_evidence": [],
        "cloud_approval_required": False,
        "target_agent_controls_handoff": True,
        "eph_cleanup_allowed": requested_phase == "eph_cleanup_verified",
        "worker_provisioning_allowed": requested_phase
        in {"eph_cleanup_verified", "worker_provisioning"},
        "side_effects": False,
    }


def validate_bmh_migration(
    source_hosts: list[dict[str, Any]],
    target_hosts: list[dict[str, Any]],
    *,
    target_controller_owns_inventory: bool,
) -> dict[str, Any]:
    if not isinstance(source_hosts, list) or not isinstance(target_hosts, list):
        raise DeliveryLifecycleError("BMH_INVENTORY_INVALID")

    def identities(hosts: list[dict[str, Any]]) -> set[tuple[str, ...]]:
        values: set[tuple[str, ...]] = set()
        names: set[tuple[str, str]] = set()
        hardware_ids: set[tuple[str, str]] = set()
        for host in hosts:
            if not isinstance(host, dict):
                raise DeliveryLifecycleError("BMH_INVENTORY_INVALID")
            name = host.get("name")
            namespace = host.get("namespace", "default")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(namespace, str)
                or not namespace
            ):
                raise DeliveryLifecycleError("BMH_IDENTITY_INCOMPLETE")
            boot_mac = host.get("boot_mac")
            bmc_address = host.get("bmc_address")
            serial_number = host.get("serial_number")
            system_uuid = host.get("system_uuid")
            hardware_values = [
                value for value in (boot_mac, bmc_address, serial_number, system_uuid)
                if isinstance(value, str) and value.strip()
            ]
            if not hardware_values:
                raise DeliveryLifecycleError("BMH_HARDWARE_IDENTITY_MISSING")
            normalized = tuple(
                value.strip().lower() if isinstance(value, str) else ""
                for value in (boot_mac, bmc_address, serial_number, system_uuid)
            )
            name_key = (namespace, name)
            hardware_key = (namespace, "|".join(normalized))
            if name_key in names or hardware_key in hardware_ids:
                raise DeliveryLifecycleError("BMH_IDENTITY_DUPLICATED")
            names.add(name_key)
            hardware_ids.add(hardware_key)
            values.add((namespace, name, *normalized))
        return values

    source = identities(source_hosts)
    target = identities(target_hosts)
    if not source:
        raise DeliveryLifecycleError("BMH_SOURCE_INVENTORY_EMPTY")
    if source != target:
        raise DeliveryLifecycleError("BMH_REGISTRY_HARDWARE_IDENTITY_MISMATCH")
    if target_controller_owns_inventory is not True:
        raise DeliveryLifecycleError("TARGET_BMH_CONTROLLER_OWNERSHIP_UNVERIFIED")
    return {
        "migration_verified": True,
        "source_count": len(source),
        "target_count": len(target),
        "hardware_identity_sets_match": True,
        "kubernetes_uids_used_as_identity": False,
        "target_controller_owns_inventory": True,
        "source_mutation_performed": False,
        "target_mutation_performed": False,
    }
