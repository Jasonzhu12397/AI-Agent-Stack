from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from apps.cloud_control_agent.network_evidence import (
    NetworkEvidenceError,
    drift_report,
    evidence_entries,
    network_evidence_required,
    require_current_network_evidence,
)

OBSERVED = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def _machine(revision: int, observed_at=OBSERVED):
    return {"inventory_revision": revision, "observed_at": observed_at}


def test_deployment_without_network_profile_skips_the_gate():
    assert network_evidence_required({"cluster_name": "lab"}) is False
    # Missing evidence is correct here: nothing was bound, so nothing to verify.
    require_current_network_evidence({"cluster_name": "lab"}, {})


def test_profile_without_bound_evidence_fails_closed():
    spec = {"cluster_name": "lab", "infra_network_profile": {"networks": []}}
    assert network_evidence_required(spec) is True
    with pytest.raises(NetworkEvidenceError) as excinfo:
        require_current_network_evidence(spec, {})
    assert str(excinfo.value) == "NETWORK_EVIDENCE_MISSING"


def test_matching_revision_and_timestamp_pass():
    machine_id = str(uuid4())
    entries = [
        {
            "machine_id": machine_id,
            "inventory_revision": 4,
            "observed_at": OBSERVED.isoformat(),
        }
    ]
    require_current_network_evidence({"network_evidence": entries}, {machine_id: _machine(4)})


def test_recollected_machine_blocks_the_handoff():
    machine_id = str(uuid4())
    entries = [
        {
            "machine_id": machine_id,
            "inventory_revision": 3,
            "observed_at": OBSERVED.isoformat(),
        }
    ]
    machines = {machine_id: _machine(4, OBSERVED + timedelta(minutes=5))}
    drift = drift_report(entries, machines)
    assert drift == [
        {
            "machine_id": machine_id,
            "code": "NETWORK_EVIDENCE_STALE",
            "expected_revision": 3,
            "current_revision": 4,
            "expected_observed_at": OBSERVED.isoformat(),
            "current_observed_at": OBSERVED + timedelta(minutes=5),
        }
    ]
    with pytest.raises(NetworkEvidenceError) as excinfo:
        require_current_network_evidence({"network_evidence": entries}, machines)
    assert str(excinfo.value) == "NETWORK_EVIDENCE_DRIFT"


def test_equivalent_timestamps_are_compared_as_instants():
    machine_id = str(uuid4())
    entries = [
        {
            "machine_id": machine_id,
            "inventory_revision": 2,
            "observed_at": OBSERVED.astimezone().isoformat(),
        }
    ]
    require_current_network_evidence({"network_evidence": entries}, {machine_id: _machine(2)})


def test_removed_machine_is_reported_as_missing():
    entries = [
        {"machine_id": str(uuid4()), "inventory_revision": 1, "observed_at": OBSERVED.isoformat()}
    ]
    drift = drift_report(entries, {})
    assert drift == [
        {"machine_id": entries[0]["machine_id"], "code": "NETWORK_EVIDENCE_MACHINE_MISSING"}
    ]


def test_malformed_evidence_never_passes():
    entries = [{"machine_id": "not-a-uuid", "inventory_revision": 1}]
    drift = drift_report(entries, {})
    assert drift == [{"machine_id": "not-a-uuid", "code": "NETWORK_EVIDENCE_INVALID"}]


def test_non_dict_evidence_entry_is_invalid():
    entries = ["machine-1"]
    assert drift_report(entries, {}) == [
        {"machine_id": None, "code": "NETWORK_EVIDENCE_INVALID"}
    ]


def test_evidence_entries_normalizes_empty_values():
    assert evidence_entries({"network_evidence": []}) is None
    assert evidence_entries({"network_evidence": {}}) is None
    assert evidence_entries(None) is None
