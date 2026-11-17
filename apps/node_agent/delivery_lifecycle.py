from __future__ import annotations

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
    "target_control_plane_ready": ("target_api_ready", "target_etcd_healthy", "target_core_network_ready"),
    "target_agent_ready": ("target_agent_on_control_plane", "target_agent_heartbeat", "checkpoint_restored"),
    "capi_pivot_verified": ("target_capi_controllers_ready", "cluster_uid_preserved", "pivot_inventory_reconciled"),
    "bmh_registry_migrated": ("source_bmh_inventory_complete", "target_bmh_inventory_complete", "bmh_identity_sets_match", "target_bmh_controller_owns_inventory"),
    "eph_cleanup_verified": ("eph_agent_stopped", "eph_bootstrap_components_removed", "eph_credentials_revoked"),
    "workers_ready": ("expected_workers_ready", "worker_chronyd_verified", "worker_network_postflight_passed"),
    "completed": ("target_cluster_postflight_passed",),
}


def validate_topology(topology: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(topology, dict):
        raise DeliveryLifecycleError("DELIVERY_TOPOLOGY_INVALID")
    eph_nodes = topology.get("eph_kubernetes_node_count")
    eph_mode = topology.get("eph_mode")
    if eph_mode != "single_node" or not isinstance(eph_nodes, int) or isinstance(eph_nodes, bool) or eph_nodes != 1:
        raise DeliveryLifecycleError("EPH_MUST_BE_SINGLE_NODE")
    if topology.get("target_agent_placement") != "target_control_plane":
        raise DeliveryLifecycleError("TARGET_AGENT_MUST_RUN_ON_CONTROL_PLANE")
    if topology.get("worker_chronyd_scope") != "target_workers_only":
        raise DeliveryLifecycleError("CHRONYD_SCOPE_MUST_BE_TARGET_WORKERS_ONLY")
    return {
        "valid": True,
        "eph_kubernetes_node_count": 1,
        "eph_mode": "single_node",
        "target_agent_placement": "target_control_plane",
        "worker_chronyd_scope": "target_workers_only",
    }


def validate_chronyd_scope(node_role: str, action: str) -> dict[str, Any]:
    if node_role == "eph" and action in {"install", "enable", "configure"}:
        raise DeliveryLifecycleError("CHRONYD_NOT_APPLICABLE_TO_EPH")
    if node_role != "target_worker":
        raise DeliveryLifecycleError("CHRONYD_ONLY_ALLOWED_ON_TARGET_WORKERS")
    if action not in {"install", "enable", "configure", "verify"}:
        raise DeliveryLifecycleError("CHRONYD_ACTION_INVALID")
    return {"allowed": True, "node_role": node_role, "action": action}


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
    missing = [key for key in REQUIRED_EVIDENCE.get(requested_phase, ()) if evidence.get(key) is not True]
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
        "worker_provisioning_allowed": requested_phase in {"eph_cleanup_verified", "worker_provisioning"},
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

    def identities(hosts: list[dict[str, Any]]) -> set[tuple[str, str]]:
        values: set[tuple[str, str]] = set()
        names: set[str] = set()
        uids: set[str] = set()
        for host in hosts:
            if not isinstance(host, dict):
                raise DeliveryLifecycleError("BMH_INVENTORY_INVALID")
            name, uid = host.get("name"), host.get("uid")
            if not isinstance(name, str) or not name or not isinstance(uid, str) or not uid:
                raise DeliveryLifecycleError("BMH_IDENTITY_INCOMPLETE")
            identity = (name, uid)
            if identity in values or name in names or uid in uids:
                raise DeliveryLifecycleError("BMH_IDENTITY_DUPLICATED")
            values.add(identity)
            names.add(name)
            uids.add(uid)
        return values

    source = identities(source_hosts)
    target = identities(target_hosts)
    if not source:
        raise DeliveryLifecycleError("BMH_SOURCE_INVENTORY_EMPTY")
    if source != target:
        raise DeliveryLifecycleError("BMH_REGISTRY_IDENTITY_MISMATCH")
    if target_controller_owns_inventory is not True:
        raise DeliveryLifecycleError("TARGET_BMH_CONTROLLER_OWNERSHIP_UNVERIFIED")
    return {
        "migration_verified": True,
        "source_count": len(source),
        "target_count": len(target),
        "identity_sets_match": True,
        "target_controller_owns_inventory": True,
        "source_mutation_performed": False,
        "target_mutation_performed": False,
    }
