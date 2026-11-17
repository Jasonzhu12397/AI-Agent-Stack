from __future__ import annotations

import re
from typing import Any

from .ntp_policy import NtpPolicyError, validate_control_plane_ntp_profile


class NtpAttestationError(ValueError):
    pass


def verify_control_plane_ntp_bootstrap(
    machines_document: dict[str, Any],
    kubeadmconfigs_document: dict[str, Any],
    approved_external_sources: Any,
    worker_networks: Any,
    required_master_count: int = 3,
) -> dict[str, Any]:
    try:
        approved, networks = validate_control_plane_ntp_profile(
            approved_external_sources,
            approved_external_sources,
            worker_networks,
        )
    except (NtpPolicyError, TypeError, ValueError) as exc:
        raise NtpAttestationError(str(exc)) from exc
    if (
        not isinstance(required_master_count, int)
        or isinstance(required_master_count, bool)
        or required_master_count < 3
    ):
        raise NtpAttestationError("TARGET_NTP_REQUIRED_MASTER_COUNT_INVALID")

    kubeadmconfigs = {}
    for item in kubeadmconfigs_document.get("items", []):
        metadata = item.get("metadata", {})
        name = metadata.get("name")
        if isinstance(name, str):
            kubeadmconfigs[name] = item

    control_plane_machines = [
        item
        for item in machines_document.get("items", [])
        if "cluster.x-k8s.io/control-plane" in item.get("metadata", {}).get("labels", {})
    ]
    if len(control_plane_machines) < required_master_count:
        raise NtpAttestationError("TARGET_NTP_CONTROL_PLANE_MACHINE_INVENTORY_INCOMPLETE")

    verified = []
    for machine in control_plane_machines:
        metadata = machine.get("metadata", {})
        machine_name = metadata.get("name")
        machine_uid = metadata.get("uid")
        reference = machine.get("spec", {}).get("bootstrap", {}).get("configRef", {})
        if (
            not isinstance(machine_name, str)
            or not isinstance(machine_uid, str)
            or reference.get("kind") != "KubeadmConfig"
            or not isinstance(reference.get("name"), str)
        ):
            raise NtpAttestationError("TARGET_NTP_MACHINE_BOOTSTRAP_REFERENCE_INVALID")
        config = kubeadmconfigs.get(reference["name"])
        if config is None:
            raise NtpAttestationError(f"TARGET_NTP_KUBEADMCONFIG_MISSING:{machine_name}")
        config_metadata = config.get("metadata", {})
        owned = any(
            owner.get("kind") == "Machine"
            and owner.get("name") == machine_name
            and owner.get("uid") == machine_uid
            and owner.get("controller") is True
            for owner in config_metadata.get("ownerReferences", [])
        )
        if not owned:
            raise NtpAttestationError(f"TARGET_NTP_KUBEADMCONFIG_OWNER_MISMATCH:{machine_name}")
        commands = config.get("spec", {}).get("preKubeadmCommands", [])
        if not isinstance(commands, list) or not commands or not all(
            isinstance(command, str) for command in commands
        ):
            raise NtpAttestationError(f"TARGET_NTP_PRE_KUBEADM_COMMAND_MISSING:{machine_name}")
        script = "\n".join(commands)
        configured_sources = tuple(re.findall(r"(?m)^\s*server\s+(\S+)", script))
        configured_networks = tuple(re.findall(r"(?m)^\s*allow\s+(\S+)", script))
        if set(configured_sources) != set(approved) or len(configured_sources) != len(approved):
            raise NtpAttestationError(f"TARGET_NTP_EXTERNAL_SOURCE_PROFILE_MISMATCH:{machine_name}")
        if set(configured_networks) != set(networks) or len(configured_networks) != len(networks):
            raise NtpAttestationError(f"TARGET_NTP_WORKER_NETWORK_PROFILE_MISMATCH:{machine_name}")
        if re.search(r"(?m)^\s*pool\s+", script):
            raise NtpAttestationError(f"TARGET_NTP_EXTERNAL_FALLBACK_FOUND:{machine_name}")
        required_markers = (
            "chronyc -n sources",
            "chronyc tracking",
            "ss -lun",
            "systemctl enable",
            "systemctl restart",
        )
        if not all(marker in script for marker in required_markers):
            raise NtpAttestationError(f"TARGET_NTP_BOOTSTRAP_VERIFICATION_MISSING:{machine_name}")
        verified.append({
            "machine": machine_name,
            "kubeadmconfig": reference["name"],
            "approved_external_sources": list(approved),
            "worker_networks": list(networks),
            "pre_kubeadm_ntp_checks_present": True,
        })
    return {
        "target_control_plane_ntp_profile_verified": True,
        "target_control_plane_ntp_external_sync_verified": True,
        "verified_control_plane_machines": verified,
        "verified_master_count": len(verified),
        "side_effects": False,
    }
