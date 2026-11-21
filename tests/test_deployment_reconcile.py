"""The detail page reconciles itself with what the EPH already reported.

Two manual sync buttons used to be the only way ``preflight_result`` learned
anything. These tests pin the pull-on-read path: the detail page stores the
newest snapshot without a click, never rewrites an unchanged one, and still
renders when the bridge is unavailable.
"""

import hashlib

import pytest
from core import deployment_reconcile
from core.deployment_reconcile import BMH_INVENTORY_KEY, EPH_PREFLIGHT_KEY
from core.models import (
    AgentNode,
    ClusterDeployment,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient


class _StubClient:
    def __init__(self, results):
        self._results = results
        self.closed = False

    def get_deployment_action_result(self, deployment_id, action):
        value = self._results.get(action)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return {"available": False, "reason": "NOT_RUN"}
        return {"available": True, **value}

    def close(self):
        self.closed = True


@pytest.fixture
def deployment(db):
    user = get_user_model().objects.create_user(
        username="reconcile-owner",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Reconcile tenant", slug="reconcile-tenant")
    TenantMembership.objects.create(
        tenant=tenant,
        user=user,
        role=TenantMembership.Role.OWNER,
    )
    project = Project.objects.create(
        tenant=tenant,
        name="Reconcile project",
        slug="reconcile-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="reconcile-eph",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash=hashlib.sha256(b"reconcile-token").hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    record = ClusterDeployment.objects.create(
        tenant=tenant,
        project=project,
        eph_node=node,
        requested_by=user,
        cluster_name="reconcile-lab",
        kubernetes_version="v1.37.2",
        spec={"cluster_name": "reconcile-lab", "kubernetes_version": "v1.37.2"},
        preflight_result={"network_defaults": {"warnings": ["IRONIC_WORKLOAD_CR_REQUIRED"]}},
        status=ClusterDeployment.Status.PROVISIONING,
        idempotency_key="reconcile-key",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return record, client


def _preflight_payload(observed_at="2026-11-20T01:02:03+00:00", command_id="cmd-1"):
    return {
        "status": "succeeded",
        "command_id": command_id,
        "output": {
            "eph_ready": False,
            "warnings": ["IRONIC_IMAGE_REQUIRED", "IRONIC_IMAGE_REQUIRED"],
            "observed_at": observed_at,
        },
    }


def test_detail_read_stores_the_preflight_without_a_button(deployment, monkeypatch):
    record, client = deployment
    stub = _StubClient({"eph.preflight": _preflight_payload()})
    monkeypatch.setattr(
        "core.deployment_reconcile.build_cloud_control_client",
        lambda: stub,
    )

    response = client.get(f"/api/v1/cluster-deployments/{record.id}/")

    assert response.status_code == 200
    stored = response.data["preflight_result"][EPH_PREFLIGHT_KEY]
    assert stored["warnings"] == ["IRONIC_IMAGE_REQUIRED"]
    assert stored["eph_ready"] is False
    assert stub.closed is True
    # The unrelated snapshot survives the merge.
    assert response.data["preflight_result"]["network_defaults"]["warnings"] == [
        "IRONIC_WORKLOAD_CR_REQUIRED"
    ]


def test_unchanged_snapshot_is_not_rewritten(deployment):
    record, _client = deployment
    payload = _preflight_payload()

    assert deployment_reconcile.store_snapshot(
        record,
        EPH_PREFLIGHT_KEY,
        deployment_reconcile.eph_preflight_snapshot(payload),
    )
    updated_at = record.updated_at
    assert not deployment_reconcile.store_snapshot(
        record,
        EPH_PREFLIGHT_KEY,
        deployment_reconcile.eph_preflight_snapshot(payload),
    )
    record.refresh_from_db()
    assert record.updated_at == updated_at


def test_a_new_observation_replaces_the_snapshot(deployment):
    record, _client = deployment
    preflight = deployment_reconcile.eph_preflight_snapshot(_preflight_payload())
    deployment_reconcile.store_snapshot(record, EPH_PREFLIGHT_KEY, preflight)
    newer = _preflight_payload(observed_at="2026-11-20T02:00:00+00:00", command_id="cmd-2")

    changed = deployment_reconcile.reconcile_deployment(
        record,
        client=_StubClient({"eph.preflight": newer}),
    )

    assert changed[EPH_PREFLIGHT_KEY] is True
    record.refresh_from_db()
    assert record.preflight_result[EPH_PREFLIGHT_KEY]["command_id"] == "cmd-2"


def test_unavailable_bridge_still_renders_the_detail_page(deployment, monkeypatch):
    record, client = deployment
    monkeypatch.setattr(
        "core.deployment_reconcile.build_cloud_control_client",
        lambda: (_ for _ in ()).throw(OSError("bridge down")),
    )

    response = client.get(f"/api/v1/cluster-deployments/{record.id}/")

    assert response.status_code == 200
    assert EPH_PREFLIGHT_KEY not in response.data["preflight_result"]


def test_failed_inspection_is_not_trusted(deployment, monkeypatch):
    record, client = deployment
    stub = _StubClient(
        {
            "metal3.host.inspect": {
                "status": "failed",
                "command_id": "cmd-3",
                "output": {"observed_at": "2026-11-20T03:00:00+00:00"},
            }
        }
    )
    monkeypatch.setattr(
        "core.deployment_reconcile.build_cloud_control_client",
        lambda: stub,
    )

    changed = deployment_reconcile.reconcile_deployment(record, client=stub)

    # The failure is stored as the current verdict; the previous successful
    # snapshot must not survive as if the inventory were still in sync.
    assert changed[BMH_INVENTORY_KEY] is True
    record.refresh_from_db()
    snapshot = record.preflight_result[BMH_INVENTORY_KEY]
    assert snapshot["status"] == "failed"
    assert snapshot["reason"] == "BMH_INSPECT_NOT_SUCCEEDED"
    assert snapshot["host_count"] == 0
    assert snapshot["matched"] == 0


def test_a_changed_verdict_under_the_same_command_is_rewritten(deployment):
    record, _client = deployment
    first = deployment_reconcile.eph_preflight_snapshot(_preflight_payload())
    assert deployment_reconcile.store_snapshot(record, EPH_PREFLIGHT_KEY, first)

    # The bridge can return the same command with a different outcome; the
    # identifiers are unchanged, so this used to be dropped as "not stale".
    same_ids = dict(first, warnings=["IRONIC_IMAGE_REQUIRED", "ETCD_SNAPSHOT_MISSING"])
    assert deployment_reconcile.store_snapshot(record, EPH_PREFLIGHT_KEY, same_ids)

    record.refresh_from_db()
    assert record.preflight_result[EPH_PREFLIGHT_KEY]["warnings"] == [
        "IRONIC_IMAGE_REQUIRED",
        "ETCD_SNAPSHOT_MISSING",
    ]


def test_a_failed_inspection_replaces_a_previous_success(deployment, monkeypatch):
    record, _client = deployment
    good = {
        "available": True,
        "reason": None,
        "status": "succeeded",
        "error_code": None,
        "command_id": "cmd-ok",
        "host_count": 3,
        "matched": 3,
        "unmatched": [],
        "warnings": [],
        "observed_at": "2026-11-20T02:00:00+00:00",
        "synced_at": "2026-11-20T02:00:01+00:00",
    }
    assert deployment_reconcile.store_snapshot(record, BMH_INVENTORY_KEY, good)
    record.refresh_from_db()

    stub = _StubClient(
        {
            "metal3.host.inspect": {
                "status": "failed",
                "command_id": "cmd-bad",
                "output": {"observed_at": "2026-11-20T03:00:00+00:00"},
            }
        }
    )
    monkeypatch.setattr(
        "core.deployment_reconcile.build_cloud_control_client",
        lambda: stub,
    )
    deployment_reconcile.reconcile_deployment(record, client=stub)
    record.refresh_from_db()

    snapshot = record.preflight_result[BMH_INVENTORY_KEY]
    assert snapshot["command_id"] == "cmd-bad"
    assert snapshot["status"] == "failed"
