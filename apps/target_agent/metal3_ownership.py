from __future__ import annotations

import re
from typing import Any


class Metal3OwnershipError(ValueError):
    pass


def _hardware_identity(host: dict[str, Any]) -> str:
    spec = host.get("spec", {})
    hardware = host.get("status", {}).get("hardware", {})
    system_uuid = hardware.get("systemUUID")
    serial = hardware.get("systemSerialNumber")
    boot_mac = spec.get("bootMACAddress")
    bmc_address = spec.get("bmc", {}).get("address")
    if isinstance(system_uuid, str) and system_uuid.strip():
        return "system-uuid:" + system_uuid.strip().lower()
    if isinstance(serial, str) and serial.strip():
        return "serial:" + serial.strip().lower()
    if (
        isinstance(boot_mac, str)
        and boot_mac.strip()
        and isinstance(bmc_address, str)
        and bmc_address.strip()
    ):
        safe_bmc = re.sub(r"(?<=://)[^/@]+@", "", bmc_address.strip(), count=1)
        return "bmc-bootmac:" + safe_bmc.lower() + "|" + boot_mac.strip().lower()
    raise Metal3OwnershipError("TARGET_BMH_STABLE_HARDWARE_IDENTITY_MISSING")


def _owner_reference(
    metadata: dict[str, Any],
    kind: str,
    name: str,
    uid: str,
) -> bool:
    return any(
        reference.get("kind") == kind
        and reference.get("name") == name
        and reference.get("uid") == uid
        and reference.get("controller") is True
        for reference in metadata.get("ownerReferences", [])
    )


def verify_metal3_ownership(
    cluster: dict[str, Any],
    machines_document: dict[str, Any],
    metal3machines_document: dict[str, Any],
    bmhs_document: dict[str, Any],
    source_bmh_identities: Any,
) -> dict[str, Any]:
    cluster_metadata = cluster.get("metadata", {})
    cluster_name = cluster_metadata.get("name")
    cluster_uid = cluster_metadata.get("uid")
    namespace = cluster_metadata.get("namespace")
    if not all(isinstance(value, str) and value for value in (
        cluster_name, cluster_uid, namespace
    )):
        raise Metal3OwnershipError("TARGET_CAPI_CLUSTER_IDENTITY_INVALID")

    machines = machines_document.get("items", [])
    metal3machines = metal3machines_document.get("items", [])
    bmhs = bmhs_document.get("items", [])
    if len(machines) < 3:
        raise Metal3OwnershipError("TARGET_CAPI_CONTROL_PLANE_MACHINES_INCOMPLETE")
    if not metal3machines:
        raise Metal3OwnershipError("TARGET_METAL3MACHINE_INVENTORY_EMPTY")
    if not bmhs:
        raise Metal3OwnershipError("TARGET_BMH_INVENTORY_EMPTY")
    if not isinstance(source_bmh_identities, list) or not source_bmh_identities:
        raise Metal3OwnershipError("SOURCE_BMH_IDENTITY_SET_REQUIRED")

    machine_by_name = {}
    for machine in machines:
        metadata = machine.get("metadata", {})
        name = metadata.get("name")
        uid = metadata.get("uid")
        if not isinstance(name, str) or not isinstance(uid, str):
            raise Metal3OwnershipError("TARGET_CAPI_MACHINE_IDENTITY_INVALID")
        if not _owner_reference(metadata, "Cluster", cluster_name, cluster_uid):
            raise Metal3OwnershipError(f"TARGET_MACHINE_CLUSTER_OWNER_MISMATCH:{name}")
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in machine.get("status", {}).get("conditions", [])
        )
        if not ready:
            raise Metal3OwnershipError(f"TARGET_CAPI_MACHINE_NOT_READY:{name}")
        machine_by_name[name] = machine

    metal3machine_by_name = {}
    for metal3machine in metal3machines:
        metadata = metal3machine.get("metadata", {})
        name = metadata.get("name")
        uid = metadata.get("uid")
        if not isinstance(name, str) or not isinstance(uid, str):
            raise Metal3OwnershipError("TARGET_METAL3MACHINE_IDENTITY_INVALID")
        owners = metadata.get("ownerReferences", [])
        machine_owner = next(
            (
                owner for owner in owners
                if owner.get("kind") == "Machine"
                and owner.get("name") in machine_by_name
                and owner.get("controller") is True
            ),
            None,
        )
        if machine_owner is None:
            raise Metal3OwnershipError(f"TARGET_METAL3MACHINE_MACHINE_OWNER_MISSING:{name}")
        machine = machine_by_name[machine_owner["name"]]
        if machine_owner.get("uid") != machine.get("metadata", {}).get("uid"):
            raise Metal3OwnershipError(f"TARGET_METAL3MACHINE_MACHINE_UID_MISMATCH:{name}")
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in metal3machine.get("status", {}).get("conditions", [])
        )
        if not ready:
            raise Metal3OwnershipError(f"TARGET_METAL3MACHINE_NOT_READY:{name}")
        metal3machine_by_name[name] = metal3machine

    if len(metal3machine_by_name) != len(machine_by_name):
        raise Metal3OwnershipError("TARGET_MACHINE_METAL3MACHINE_COUNT_MISMATCH")
    for machine_name, machine in machine_by_name.items():
        infrastructure_ref = machine.get("spec", {}).get("infrastructureRef", {})
        metal3machine = metal3machine_by_name.get(infrastructure_ref.get("name"))
        if (
            infrastructure_ref.get("kind") != "Metal3Machine"
            or not metal3machine
            or (infrastructure_ref.get("namespace") or namespace)
            != metal3machine.get("metadata", {}).get("namespace")
            or not str(infrastructure_ref.get("apiVersion", "")).startswith(
                "infrastructure.cluster.x-k8s.io/"
            )
        ):
            raise Metal3OwnershipError(
                f"TARGET_MACHINE_INFRASTRUCTURE_REFERENCE_MISMATCH:{machine_name}"
            )

    source_set = set()
    for identity in source_bmh_identities:
        if not isinstance(identity, dict):
            raise Metal3OwnershipError("SOURCE_BMH_IDENTITY_INVALID")
        values = (
            identity.get("namespace"),
            identity.get("name"),
            identity.get("hardware_identity"),
        )
        if not all(isinstance(value, str) and value for value in values):
            raise Metal3OwnershipError("SOURCE_BMH_IDENTITY_INVALID")
        source_set.add((values[0], values[1], values[2].lower()))
    source_hardware_ids = [item[2].lower() for item in source_set]
    if (
        len(source_set) != len(source_bmh_identities)
        or len(source_hardware_ids) != len(set(source_hardware_ids))
    ):
        raise Metal3OwnershipError("SOURCE_BMH_IDENTITY_DUPLICATE")

    target_identities = []
    hardware_ids = set()
    for host in bmhs:
        metadata = host.get("metadata", {})
        host_namespace = metadata.get("namespace")
        host_name = metadata.get("name")
        if not isinstance(host_namespace, str) or not isinstance(host_name, str):
            raise Metal3OwnershipError("TARGET_BMH_METADATA_INVALID")
        hardware_identity = _hardware_identity(host)
        if hardware_identity in hardware_ids:
            raise Metal3OwnershipError("TARGET_BMH_HARDWARE_IDENTITY_DUPLICATE")
        hardware_ids.add(hardware_identity)
        target_identities.append({
            "namespace": host_namespace,
            "name": host_name,
            "hardware_identity": hardware_identity,
        })
        status = host.get("status", {})
        provisioning = status.get("provisioning", {})
        state = provisioning.get("state")
        if status.get("operationalStatus") != "OK":
            raise Metal3OwnershipError(f"TARGET_BMH_NOT_OPERATIONAL:{host_name}")
        if state not in {"available", "ready", "provisioned", "externally provisioned"}:
            raise Metal3OwnershipError(f"TARGET_BMH_NOT_STABLE:{host_name}")
        if status.get("errorMessage") or status.get("errorType"):
            raise Metal3OwnershipError(f"TARGET_BMH_HAS_ERROR:{host_name}")
        consumer = host.get("spec", {}).get("consumerRef")
        if consumer:
            consumer_name = consumer.get("name")
            consumer_namespace = consumer.get("namespace") or host_namespace
            metal3machine = metal3machine_by_name.get(consumer_name)
            if (
                not metal3machine
                or consumer.get("kind") != "Metal3Machine"
                or consumer_namespace != metal3machine.get("metadata", {}).get("namespace")
                or consumer.get("uid") != metal3machine.get("metadata", {}).get("uid")
                or not str(consumer.get("apiVersion", "")).startswith(
                    "infrastructure.cluster.x-k8s.io/"
                )
            ):
                raise Metal3OwnershipError(f"TARGET_BMH_CONSUMER_REFERENCE_MISMATCH:{host_name}")
        elif state not in {"available", "ready"}:
            raise Metal3OwnershipError(f"TARGET_BMH_CONSUMER_REFERENCE_MISSING:{host_name}")
    target_set = {
        (item["namespace"], item["name"], item["hardware_identity"])
        for item in target_identities
    }
    if source_set != target_set:
        raise Metal3OwnershipError("TARGET_BMH_IDENTITY_SET_DIFFERS_FROM_SOURCE")
    target_identities.sort(
        key=lambda item: (item["namespace"], item["name"], item["hardware_identity"])
    )
    return {
        "target_cluster_uid": cluster_uid,
        "machine_count": len(machines),
        "metal3machine_count": len(metal3machines),
        "target_bmh_identities": target_identities,
        "target_metal3_ownership_verified": True,
        "target_metal3_reconciliation_healthy": True,
        "source_target_bmh_identity_sets_equal": True,
        "side_effects": False,
    }
