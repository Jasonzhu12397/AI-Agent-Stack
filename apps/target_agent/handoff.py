from __future__ import annotations

from enum import StrEnum
from typing import Any

from .ntp_policy import NtpPolicyError, validate_ironic_internal_ntp_ip


class HandoffError(ValueError):
    def __init__(self, message: str, side_effects: bool = False) -> None:
        super().__init__(message)
        self.side_effects = side_effects


class HandoffPhase(StrEnum):
    EPH_SINGLE_NODE_READY = "eph_single_node_ready"
    BMH_REGISTERED = "bmh_registered"
    BMH_READY = "bmh_ready"
    TARGET_CONTROL_PLANE_READY = "target_control_plane_ready"
    TARGET_AGENT_READY = "target_agent_ready"
    CAPI_PIVOT_VERIFIED = "capi_pivot_verified"
    BMH_REGISTRY_MIGRATED = "bmh_registry_migrated"
    EPH_CLEANUP_VERIFIED = "eph_cleanup_verified"
    WORKER_PROVISIONING = "worker_provisioning"
    WORKERS_READY = "workers_ready"
    COMPLETED = "completed"


PHASES = tuple(HandoffPhase)


def _require(evidence: dict[str, Any], key: str, expected: Any = True) -> None:
    actual = evidence.get(key)
    if expected is True:
        valid = actual is True
    elif expected is False:
        valid = actual is False
    else:
        valid = actual == expected
    if not valid:
        raise HandoffError(f"HANDOFF_EVIDENCE_REQUIRED:{key}")


def _require_positive_count(evidence: dict[str, Any], key: str, error: str) -> int:
    value = evidence.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise HandoffError(error)
    return value


def _require_internal_ntp_ip(evidence: dict[str, Any]) -> str:
    try:
        return validate_ironic_internal_ntp_ip(
            str(evidence.get("ironic_internal_ntp_ip", ""))
        )
    except NtpPolicyError as exc:
        raise HandoffError("HANDOFF_IRONIC_INTERNAL_NTP_IP_INVALID") from exc


def _bmh_identity_set(value: Any, field: str) -> set[tuple[str, str, str]]:
    if not isinstance(value, list) or not value:
        raise HandoffError(f"HANDOFF_BMH_INVENTORY_MISSING:{field}")
    identities = []
    for item in value:
        if not isinstance(item, dict):
            raise HandoffError(f"HANDOFF_BMH_IDENTITY_INVALID:{field}")
        namespace = item.get("namespace")
        name = item.get("name")
        hardware_identity = item.get("hardware_identity")
        if not all(isinstance(part, str) and part.strip() for part in (
            namespace, name, hardware_identity
        )):
            raise HandoffError(f"HANDOFF_BMH_IDENTITY_INVALID:{field}")
        identities.append((namespace, name, hardware_identity.strip().lower()))
    if len(identities) != len(set(identities)):
        raise HandoffError(f"HANDOFF_BMH_IDENTITY_DUPLICATE:{field}")
    return set(identities)


def validate_phase_evidence(phase: HandoffPhase, evidence: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise HandoffError("HANDOFF_EVIDENCE_OBJECT_REQUIRED")
    normalized = dict(evidence)
    if phase is HandoffPhase.EPH_SINGLE_NODE_READY:
        _require(evidence, "eph_ready")
        if (
            not isinstance(evidence.get("eph_node_count"), int)
            or isinstance(evidence.get("eph_node_count"), bool)
            or evidence.get("eph_node_count") != 1
        ):
            raise HandoffError("HANDOFF_EPH_MUST_BE_SINGLE_NODE")
        _require(evidence, "eph_source_preflight_passed")
    elif phase is HandoffPhase.BMH_REGISTERED:
        _require(evidence, "bmh_registration_applied")
        _require(evidence, "bmh_secret_refs_verified")
        count = evidence.get("bmh_count")
        inventory_count = evidence.get("bmh_inventory_count")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
            or not isinstance(inventory_count, int)
            or isinstance(inventory_count, bool)
            or inventory_count != count
        ):
            raise HandoffError("HANDOFF_BMH_REGISTRATION_INVENTORY_INCOMPLETE")
    elif phase is HandoffPhase.BMH_READY:
        expected = _require_positive_count(
            evidence, "expected_bmh_count", "HANDOFF_BMH_READINESS_INCOMPLETE"
        )
        ready = _require_positive_count(
            evidence, "bmh_ready_count", "HANDOFF_BMH_READINESS_INCOMPLETE"
        )
        if ready != expected:
            raise HandoffError("HANDOFF_BMH_READINESS_INCOMPLETE")
        _require(evidence, "bmh_all_operational")
        _require(evidence, "ironic_inventory_verified")
    elif phase is HandoffPhase.TARGET_CONTROL_PLANE_READY:
        _require(evidence, "target_control_plane_ready")
        ready_master_count = _require_positive_count(
            evidence,
            "ready_master_count",
            "HANDOFF_THREE_TARGET_MASTERS_REQUIRED",
        )
        if ready_master_count < 3:
            raise HandoffError("HANDOFF_THREE_TARGET_MASTERS_REQUIRED")
        normalized["ironic_internal_ntp_ip"] = _require_internal_ntp_ip(evidence)
        _require(evidence, "target_control_plane_ntp_profile_verified")
        _require(evidence, "target_control_plane_ntp_bootstrap_sync_gate_verified")
        _require(evidence, "ironic_internal_ntp_service_reachable")
    elif phase is HandoffPhase.TARGET_AGENT_READY:
        _require(evidence, "target_agent_ready")
        _require(evidence, "target_agent_leader_elected")
        target_agent_replica_count = _require_positive_count(
            evidence,
            "target_agent_replica_count",
            "HANDOFF_THREE_TARGET_AGENT_REPLICAS_REQUIRED",
        )
        if target_agent_replica_count != 3:
            raise HandoffError("HANDOFF_THREE_TARGET_AGENT_REPLICAS_REQUIRED")
    elif phase is HandoffPhase.CAPI_PIVOT_VERIFIED:
        _require(evidence, "capi_pivot_succeeded")
        _require(evidence, "target_controllers_ready")
        source_uid = evidence.get("source_cluster_uid")
        target_uid = evidence.get("target_cluster_uid")
        source_annotation = evidence.get("target_source_cluster_uid_annotation")
        source_identity = evidence.get("source_stable_cluster_identity")
        target_identity = evidence.get("target_stable_cluster_identity")
        if not isinstance(source_uid, str) or not source_uid:
            raise HandoffError("HANDOFF_SOURCE_CLUSTER_UID_REQUIRED")
        if not isinstance(target_uid, str) or not target_uid:
            raise HandoffError("HANDOFF_TARGET_CLUSTER_UID_REQUIRED")
        if source_annotation != source_uid:
            raise HandoffError("HANDOFF_SOURCE_UID_ANNOTATION_MISMATCH")
        if not isinstance(source_identity, str) or not source_identity:
            raise HandoffError("HANDOFF_SOURCE_STABLE_IDENTITY_REQUIRED")
        if target_identity != source_identity:
            raise HandoffError("HANDOFF_STABLE_CLUSTER_IDENTITY_NOT_PRESERVED")
        _require(evidence, "source_cluster_absent")
        _require(evidence, "target_cluster_ownership_verified")
    elif phase is HandoffPhase.BMH_REGISTRY_MIGRATED:
        source = _bmh_identity_set(evidence.get("source_bmh_identities"), "source")
        target = _bmh_identity_set(evidence.get("target_bmh_identities"), "target")
        if source != target:
            raise HandoffError("HANDOFF_BMH_IDENTITY_SETS_DIFFER")
        _require(evidence, "target_metal3_ownership_verified")
        _require(evidence, "target_metal3_reconciliation_healthy")
        _require(evidence, "target_ironic_inventory_verified")
    elif phase is HandoffPhase.EPH_CLEANUP_VERIFIED:
        _require(evidence, "target_agent_owns_handoff")
        _require(evidence, "eph_credentials_revoked")
        _require(evidence, "eph_workloads_absent")
        _require(evidence, "eph_api_no_longer_required")
    elif phase is HandoffPhase.WORKER_PROVISIONING:
        normalized["ironic_internal_ntp_ip"] = _require_internal_ntp_ip(evidence)
        _require(evidence, "ironic_internal_ntp_service_reachable")
        _require(evidence, "target_agent_leader_elected")
        _require(evidence, "worker_provisioning_resources_applied")
        _require(evidence, "worker_provisioning_server_dry_run_passed")
        _require(evidence, "worker_provisioning_controller_observed")
        _require(evidence, "worker_network_contract_validated")
        _require(evidence, "preprovision_bmh_capacity_verified")
        _require_positive_count(
            evidence,
            "desired_worker_count",
            "HANDOFF_WORKER_PROVISIONING_REPLICA_COUNT_INVALID",
        )
    elif phase is HandoffPhase.WORKERS_READY:
        desired = _require_positive_count(
            evidence, "desired_worker_count", "HANDOFF_WORKER_READINESS_INCOMPLETE"
        )
        ready = _require_positive_count(
            evidence, "ready_worker_count", "HANDOFF_WORKER_READINESS_INCOMPLETE"
        )
        if ready != desired:
            raise HandoffError("HANDOFF_WORKER_READINESS_INCOMPLETE")
        ntp_ip = _require_internal_ntp_ip(evidence)
        sources = evidence.get("worker_ntp_bootstrap_sources")
        if not isinstance(sources, dict) or len(sources) != desired:
            raise HandoffError("HANDOFF_WORKER_NTP_EVIDENCE_INCOMPLETE")
        for worker, configured in sources.items():
            if configured != [ntp_ip]:
                raise HandoffError(f"HANDOFF_WORKER_NTP_SOURCE_MISMATCH:{worker}")
    elif phase is HandoffPhase.COMPLETED:
        _require(evidence, "target_postflight_passed")
        _require(evidence, "worker_network_postflight_passed")
        _require(evidence, "worker_chronyd_bootstrap_sync_verified")
        _require(evidence, "data_plane_pod_packet_path_verified")
        _require(evidence, "deployment_checkpoint_durable")
    return normalized


def evaluate_transition(
    current_phase: str | None,
    next_phase: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    try:
        target = HandoffPhase(next_phase)
        current = HandoffPhase(current_phase) if current_phase else None
    except ValueError as exc:
        raise HandoffError("HANDOFF_PHASE_INVALID") from exc
    expected_index = 0 if current is None else PHASES.index(current) + 1
    if expected_index >= len(PHASES) or PHASES[expected_index] is not target:
        raise HandoffError("HANDOFF_PHASE_TRANSITION_INVALID")
    validated = validate_phase_evidence(target, evidence)
    return {
        "accepted": True,
        "from_phase": current.value if current else None,
        "to_phase": target.value,
        "evidence": validated,
        "side_effects": False,
    }
