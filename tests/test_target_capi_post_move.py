import copy

import pytest

from apps.target_agent.capi_post_move import CapiPostMoveError, verify_post_move

PROVIDER_ID = "metal3://metal3-system/control-plane-0/cp-0-m3m"


def documents():
    machine = {
        "metadata": {"name": "cp-0", "uid": "machine-uid"},
        "spec": {"providerID": PROVIDER_ID},
        "status": {"phase": "Running", "nodeRef": {"name": "cp-0", "uid": "node-uid"}},
    }
    metal3machine = {
        "metadata": {
            "name": "cp-0-m3m",
            "ownerReferences": [
                {
                    "apiVersion": "cluster.x-k8s.io/v1beta1",
                    "kind": "Machine",
                    "name": "cp-0",
                    "uid": "machine-uid",
                    "controller": True,
                }
            ],
        },
        "spec": {"providerID": PROVIDER_ID},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    bmh = {
        "metadata": {"name": "control-plane-0"},
        "status": {
            "provisioning": {"state": "provisioned"},
            "operationalStatus": "OK",
        },
    }
    node = {
        "metadata": {"name": "cp-0", "uid": "node-uid"},
        "spec": {"providerID": PROVIDER_ID},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    return {
        "machines": {"items": [machine]},
        "metal3machines": {"items": [metal3machine]},
        "baremetalhosts": {"items": [bmh]},
        "nodes": {"items": [node]},
    }


def verify(documents_):
    return verify_post_move(
        "metal3-system",
        documents_["machines"],
        documents_["metal3machines"],
        documents_["baremetalhosts"],
        documents_["nodes"],
    )


def test_verifies_moved_machine_identity_chain():
    evidence = verify(documents())

    assert evidence["post_move_verified"] is True
    assert evidence["machine_count"] == 1
    assert evidence["machines"][0] == {
        "machine_name": "cp-0",
        "machine_uid": "machine-uid",
        "metal3machine_name": "cp-0-m3m",
        "bmh_name": "control-plane-0",
        "provider_id": PROVIDER_ID,
        "node_name": "cp-0",
        "node_uid": "node-uid",
        "bmh_provisioning_state": "provisioned",
        "bmh_operational_status": "OK",
        "metal3machine_ready": True,
        "machine_running": True,
        "node_ready": True,
    }
    assert len(evidence["post_move_digest"]) == 64


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda docs: docs["machines"]["items"][0]["status"].update({"phase": "Provisioning"}),
            "CAPI_POST_MOVE_MACHINE_NOT_RUNNING",
        ),
        (
            lambda docs: docs["metal3machines"]["items"][0]["status"].update(
                {"conditions": [{"type": "Ready", "status": "False"}]}
            ),
            "CAPI_POST_MOVE_METAL3MACHINE_NOT_READY",
        ),
        (
            lambda docs: docs["metal3machines"]["items"][0]["metadata"].update(
                {"ownerReferences": []}
            ),
            "CAPI_POST_MOVE_METAL3MACHINE_OWNER_MISSING",
        ),
        (
            lambda docs: docs["metal3machines"]["items"][0]["metadata"][
                "ownerReferences"
            ][0].update({"uid": "another-machine"}),
            "CAPI_POST_MOVE_METAL3MACHINE_OWNER_MISSING",
        ),
        (
            lambda docs: docs["baremetalhosts"]["items"][0]["status"]["provisioning"].update(
                {"state": "provisioning"}
            ),
            "CAPI_POST_MOVE_BMH_NOT_PROVISIONED",
        ),
        (
            lambda docs: docs["baremetalhosts"]["items"][0]["status"].update(
                {"operationalStatus": "error"}
            ),
            "CAPI_POST_MOVE_BMH_NOT_OPERATIONAL",
        ),
        (
            lambda docs: docs["nodes"]["items"][0]["spec"].update(
                {"providerID": "metal3://metal3-system/other-bmh/cp-0-m3m"}
            ),
            "CAPI_POST_MOVE_NODE_PROVIDER_ID_MISMATCH",
        ),
        (
            lambda docs: docs["nodes"]["items"][0]["status"].update(
                {"conditions": [{"type": "Ready", "status": "Unknown"}]}
            ),
            "CAPI_POST_MOVE_NODE_NOT_READY",
        ),
        (
            lambda docs: docs["machines"]["items"][0]["status"].update({"nodeRef": None}),
            "CAPI_POST_MOVE_MACHINE_NODE_REF_MISSING",
        ),
        (
            lambda docs: docs["machines"]["items"][0]["spec"].update(
                {"providerID": "metal3://metal3-system/control-plane-0/cp-0-m3m-other"}
            ),
            "CAPI_POST_MOVE_PROVIDER_ID_MISMATCH",
        ),
        (
            lambda docs: [
                document["spec"].update(
                    {"providerID": "metal3://other-system/control-plane-0/cp-0-m3m"}
                )
                for document in (
                    docs["machines"]["items"][0],
                    docs["metal3machines"]["items"][0],
                    docs["nodes"]["items"][0],
                )
            ],
            "CAPI_POST_MOVE_PROVIDER_ID_NAMESPACE_MISMATCH",
        ),
    ],
)
def test_post_move_failures_fail_closed(mutate, code):
    docs = copy.deepcopy(documents())
    mutate(docs)

    with pytest.raises(CapiPostMoveError) as excinfo:
        verify(docs)

    assert excinfo.value.code == code


def test_provider_id_must_use_canonical_description_not_bmh_uuid():
    docs = documents()
    old_provider_id = "metal3://metal3-system/bmh-uuid"
    docs["machines"]["items"][0]["spec"]["providerID"] = old_provider_id
    docs["nodes"]["items"][0]["spec"]["providerID"] = old_provider_id

    with pytest.raises(CapiPostMoveError) as excinfo:
        verify(docs)

    assert excinfo.value.code == "CAPI_POST_MOVE_MACHINE_PROVIDER_ID_INVALID"


def test_provider_id_must_point_at_the_owning_metal3machine():
    docs = documents()
    provider_id = "metal3://metal3-system/control-plane-0/another-m3m"
    docs["machines"]["items"][0]["spec"]["providerID"] = provider_id
    docs["metal3machines"]["items"][0]["spec"]["providerID"] = provider_id
    docs["nodes"]["items"][0]["spec"]["providerID"] = provider_id

    with pytest.raises(CapiPostMoveError) as excinfo:
        verify(docs)

    assert excinfo.value.code == "CAPI_POST_MOVE_PROVIDER_ID_METAL3MACHINE_MISMATCH"


@pytest.mark.parametrize(
    "status",
    [
        {"ready": True},
        {"initialization": {"provisioned": True}},
        {"ready": False, "initialization": {"provisioned": True}},
    ],
)
def test_metal3machine_ready_accepts_every_capm3_status_shape(status):
    docs = documents()
    docs["metal3machines"]["items"][0]["status"] = status

    assert verify(docs)["post_move_verified"] is True


def test_empty_machine_list_fails_closed():
    docs = documents()
    docs["machines"]["items"] = []

    with pytest.raises(CapiPostMoveError) as excinfo:
        verify(docs)

    assert excinfo.value.code == "CAPI_POST_MOVE_NO_MACHINES"
