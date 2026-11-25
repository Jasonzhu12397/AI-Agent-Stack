"""Fail-closed post-move verification for a CAPI pivot.

A successful ``clusterctl move`` only proves that objects were copied. The
takeover is not verified until the moved Metal3 objects still describe the same
machines and the Target cluster can resolve each Machine to a running workload
Node:

* every BareMetalHost is provisioned and operational;
* every Metal3Machine is Ready, is controlled by the expected CAPI Machine and
  carries the canonical ``metal3://<namespace>/<bmh>/<m3m>`` provider ID;
* the CAPI Machine is Running and advertises the same provider ID;
* the referenced workload Node is Ready and advertises the same provider ID.

The module is pure validation over already-fetched Kubernetes documents so it
can be exercised without a live cluster and reused by the Target Agent.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

PROVIDER_ID_PATTERN = re.compile(r"^metal3://([a-z0-9](?:[-a-z0-9]*[a-z0-9])?)/([^/]+)/([^/]+)$")


class CapiPostMoveError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _items(document: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise CapiPostMoveError(f"CAPI_POST_MOVE_{field}_DOCUMENT_INVALID")
    items = document["items"]
    if not all(isinstance(item, dict) for item in items):
        raise CapiPostMoveError(f"CAPI_POST_MOVE_{field}_DOCUMENT_INVALID")
    return items


def _condition_true(document: dict[str, Any], condition_type: str) -> bool:
    status = document.get("status")
    if not isinstance(status, dict):
        return False
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return False
    return any(
        isinstance(condition, dict)
        and condition.get("type") == condition_type
        and condition.get("status") == "True"
        for condition in conditions
    )


def _provisioning_state(bmh: dict[str, Any]) -> Any:
    status = bmh.get("status")
    if not isinstance(status, dict):
        return None
    provisioning = status.get("provisioning")
    if not isinstance(provisioning, dict):
        return None
    return provisioning.get("state")


def _controlled_by_machine(item: dict[str, Any], machine_uid: str) -> bool:
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        return False
    owners = metadata.get("ownerReferences")
    if not isinstance(owners, list):
        return False
    return any(
        isinstance(owner, dict) and owner.get("uid") == machine_uid
        for owner in owners
    )


def _owner_machine(
    metal3machine: dict[str, Any],
    machines_by_uid: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    metadata = metal3machine.get("metadata")
    if not isinstance(metadata, dict):
        raise CapiPostMoveError("CAPI_POST_MOVE_METAL3MACHINE_METADATA_INVALID")
    owners = metadata.get("ownerReferences")
    if not isinstance(owners, list):
        raise CapiPostMoveError("CAPI_POST_MOVE_METAL3MACHINE_OWNER_INVALID")
    controller_owners = [
        owner
        for owner in owners
        if isinstance(owner, dict)
        and owner.get("kind") == "Machine"
        and owner.get("controller") is True
    ]
    if len(controller_owners) != 1:
        raise CapiPostMoveError("CAPI_POST_MOVE_METAL3MACHINE_OWNER_INVALID")
    owner = controller_owners[0]
    machine = machines_by_uid.get(owner.get("uid"))
    if machine is None:
        raise CapiPostMoveError("CAPI_POST_MOVE_METAL3MACHINE_OWNER_MACHINE_MISSING")
    owner_name = machine.get("metadata", {}).get("name")
    if owner.get("name") != owner_name:
        raise CapiPostMoveError("CAPI_POST_MOVE_METAL3MACHINE_OWNER_NAME_MISMATCH")
    return machine


def _provider_id(document: dict[str, Any], field: str) -> str:
    spec = document.get("spec")
    if not isinstance(spec, dict):
        raise CapiPostMoveError(f"CAPI_POST_MOVE_{field}_SPEC_INVALID")
    provider_id = spec.get("providerID")
    if not isinstance(provider_id, str) or not provider_id:
        raise CapiPostMoveError(f"CAPI_POST_MOVE_{field}_PROVIDER_ID_MISSING")
    if not PROVIDER_ID_PATTERN.fullmatch(provider_id):
        raise CapiPostMoveError(f"CAPI_POST_MOVE_{field}_PROVIDER_ID_INVALID")
    return provider_id


def _node_for_machine(
    machine: dict[str, Any],
    nodes_by_uid: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    status = machine.get("status")
    if not isinstance(status, dict):
        raise CapiPostMoveError("CAPI_POST_MOVE_MACHINE_STATUS_INVALID")
    node_ref = status.get("nodeRef")
    if not isinstance(node_ref, dict):
        raise CapiPostMoveError("CAPI_POST_MOVE_MACHINE_NODE_REF_MISSING")
    node_uid = node_ref.get("uid")
    if not isinstance(node_uid, str) or not node_uid:
        raise CapiPostMoveError("CAPI_POST_MOVE_MACHINE_NODE_REF_UID_MISSING")
    node = nodes_by_uid.get(node_uid)
    if node is None:
        raise CapiPostMoveError("CAPI_POST_MOVE_MACHINE_NODE_MISSING")
    node_name = node.get("metadata", {}).get("name")
    if node_ref.get("name") != node_name:
        raise CapiPostMoveError("CAPI_POST_MOVE_MACHINE_NODE_REF_NAME_MISMATCH")
    return node


def verify_post_move(
    namespace: str,
    machines_document: dict[str, Any],
    metal3machines_document: dict[str, Any],
    baremetalhosts_document: dict[str, Any],
    nodes_document: dict[str, Any],
) -> dict[str, Any]:
    """Verify every moved machine resolves to a delivered Metal3 identity."""

    if not isinstance(namespace, str) or not namespace:
        raise CapiPostMoveError("CAPI_POST_MOVE_NAMESPACE_REQUIRED")
    machines = _items(machines_document, "MACHINES")
    metal3machines = _items(metal3machines_document, "METAL3MACHINES")
    baremetalhosts = _items(baremetalhosts_document, "BARE_METAL_HOSTS")
    nodes = _items(nodes_document, "NODES")
    if not machines:
        raise CapiPostMoveError("CAPI_POST_MOVE_NO_MACHINES")
    if not metal3machines:
        raise CapiPostMoveError("CAPI_POST_MOVE_NO_METAL3MACHINES")

    machines_by_uid = {
        item.get("metadata", {}).get("uid"): item
        for item in machines
        if isinstance(item.get("metadata"), dict)
    }
    nodes_by_uid = {
        item.get("metadata", {}).get("uid"): item
        for item in nodes
        if isinstance(item.get("metadata"), dict)
    }
    bmhs_by_name = {
        item.get("metadata", {}).get("name"): item
        for item in baremetalhosts
        if isinstance(item.get("metadata"), dict)
    }

    verified: list[dict[str, Any]] = []
    for machine in machines:
        metadata = machine.get("metadata")
        if not isinstance(metadata, dict):
            raise CapiPostMoveError("CAPI_POST_MOVE_MACHINE_METADATA_INVALID")
        machine_name = metadata.get("name")
        machine_uid = metadata.get("uid")
        if not isinstance(machine_name, str) or not machine_name:
            raise CapiPostMoveError("CAPI_POST_MOVE_MACHINE_NAME_MISSING")
        if not isinstance(machine_uid, str) or not machine_uid:
            raise CapiPostMoveError("CAPI_POST_MOVE_MACHINE_UID_MISSING")
        status = machine.get("status")
        if not isinstance(status, dict) or status.get("phase") != "Running":
            raise CapiPostMoveError("CAPI_POST_MOVE_MACHINE_NOT_RUNNING")
        machine_provider_id = _provider_id(machine, "MACHINE")

        owned = [
            item for item in metal3machines if _controlled_by_machine(item, machine_uid)
        ]
        if len(owned) != 1:
            raise CapiPostMoveError("CAPI_POST_MOVE_METAL3MACHINE_OWNER_MISSING")
        metal3machine = owned[0]
        metal3machine_name = metal3machine.get("metadata", {}).get("name")
        if not isinstance(metal3machine_name, str) or not metal3machine_name:
            raise CapiPostMoveError("CAPI_POST_MOVE_METAL3MACHINE_NAME_MISSING")
        if _owner_machine(metal3machine, machines_by_uid) is not machine:
            raise CapiPostMoveError("CAPI_POST_MOVE_METAL3MACHINE_OWNER_MISMATCH")
        if not _condition_true(metal3machine, "Ready"):
            raise CapiPostMoveError("CAPI_POST_MOVE_METAL3MACHINE_NOT_READY")
        metal3_provider_id = _provider_id(metal3machine, "METAL3MACHINE")
        if metal3_provider_id != machine_provider_id:
            raise CapiPostMoveError("CAPI_POST_MOVE_PROVIDER_ID_MISMATCH")

        match = PROVIDER_ID_PATTERN.fullmatch(metal3_provider_id)
        if match is None:
            raise CapiPostMoveError("CAPI_POST_MOVE_PROVIDER_ID_INVALID")
        provider_namespace, bmh_name, provider_metal3machine_name = match.groups()
        if provider_namespace != namespace:
            raise CapiPostMoveError("CAPI_POST_MOVE_PROVIDER_ID_NAMESPACE_MISMATCH")
        if provider_metal3machine_name != metal3machine_name:
            raise CapiPostMoveError("CAPI_POST_MOVE_PROVIDER_ID_METAL3MACHINE_MISMATCH")
        bmh = bmhs_by_name.get(bmh_name)
        if bmh is None:
            raise CapiPostMoveError("CAPI_POST_MOVE_PROVIDER_ID_BMH_MISSING")
        if _provisioning_state(bmh) != "provisioned":
            raise CapiPostMoveError("CAPI_POST_MOVE_BMH_NOT_PROVISIONED")
        bmh_status = bmh.get("status")
        if not isinstance(bmh_status, dict) or bmh_status.get("operationalStatus") != "OK":
            raise CapiPostMoveError("CAPI_POST_MOVE_BMH_NOT_OPERATIONAL")

        node = _node_for_machine(machine, nodes_by_uid)
        if not _condition_true(node, "Ready"):
            raise CapiPostMoveError("CAPI_POST_MOVE_NODE_NOT_READY")
        node_spec = node.get("spec")
        if not isinstance(node_spec, dict):
            raise CapiPostMoveError("CAPI_POST_MOVE_NODE_SPEC_INVALID")
        if node_spec.get("providerID") != metal3_provider_id:
            raise CapiPostMoveError("CAPI_POST_MOVE_NODE_PROVIDER_ID_MISMATCH")

        verified.append(
            {
                "machine_name": machine_name,
                "machine_uid": machine_uid,
                "metal3machine_name": metal3machine_name,
                "bmh_name": bmh_name,
                "provider_id": metal3_provider_id,
                "node_name": node.get("metadata", {}).get("name"),
                "node_uid": node.get("metadata", {}).get("uid"),
                "bmh_provisioning_state": "provisioned",
                "bmh_operational_status": "OK",
                "metal3machine_ready": True,
                "machine_running": True,
                "node_ready": True,
            }
        )

    digest = hashlib.sha256(
        json.dumps(verified, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "post_move_verified": True,
        "machine_count": len(verified),
        "machines": verified,
        "post_move_digest": digest,
    }
