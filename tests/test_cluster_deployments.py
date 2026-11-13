# ruff: noqa: I001
import datetime
import hashlib
import json
import uuid

import pytest
from core.models import (
    AgentCommand,
    AgentNode,
    ClusterDeployment,
    ClusterDeploymentCheckpoint,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from core.cluster_deployment_services import (
    _maybe_queue_capi_pivot,
    _update_capi_pivot_provider_compatibility,
    handle_deployment_command_result,
)


@pytest.fixture
def cluster_delivery_workspace(db):
    user = get_user_model().objects.create_user(
        username="cluster-delivery-owner",
        password="test-password",
    )
    tenant = Tenant.objects.create(name="Delivery tenant", slug="delivery-tenant")
    TenantMembership.objects.create(
        tenant=tenant,
        user=user,
        role=TenantMembership.Role.OWNER,
    )
    project = Project.objects.create(
        tenant=tenant,
        name="Delivery project",
        slug="delivery-project",
    )
    ProjectMembership.objects.create(
        project=project,
        user=user,
        role=ProjectMembership.Role.ADMIN,
    )
    token = "node-agent-test-token"
    node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="delivery-eph",
        node_type=AgentNode.NodeType.EPH_NODE,
        credential_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, tenant, project, node, token, client


def cluster_spec():
    return {
        "cluster_name": "edge-lab",
        "kubernetes_version": "v1.32.2",
        "control_plane_replicas": 3,
        "worker_replicas": 4,
    }


def cluster_plan_hash():
    encoded = json.dumps(
        cluster_spec(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_deployment(client, tenant, project, node, key="cluster-plan-001"):
    return client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cluster-deployments/",
        {"eph_node_id": str(node.id), "cluster_spec": cluster_spec()},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )


@pytest.mark.django_db
def test_cluster_deployment_creates_idempotent_eph_preflight_command(
    cluster_delivery_workspace,
):
    user, tenant, project, node, token, client = cluster_delivery_workspace

    first = create_deployment(client, tenant, project, node)
    repeated = create_deployment(client, tenant, project, node)

    assert first.status_code == 202
    assert first.data["status"] == ClusterDeployment.Status.PREFLIGHT_QUEUED
    assert repeated.status_code == 200
    assert repeated.data["id"] == first.data["id"]
    assert AgentCommand.objects.filter(deployment_id=first.data["id"]).count() == 1
    command = AgentCommand.objects.get(deployment_id=first.data["id"])
    assert command.command_type == "host.preflight"
    assert command.node_id == node.id


@pytest.mark.django_db
def test_cluster_deployment_rejects_even_control_plane_replica_count(
    cluster_delivery_workspace,
):
    user, tenant, project, node, token, client = cluster_delivery_workspace
    spec = cluster_spec()
    spec["control_plane_replicas"] = 2

    response = client.post(
        f"/api/v1/tenants/{tenant.id}/projects/{project.id}/cluster-deployments/",
        {"eph_node_id": str(node.id), "cluster_spec": spec},
        format="json",
        HTTP_IDEMPOTENCY_KEY="cluster-plan-even-cp",
    )

    assert response.status_code == 400
    assert ClusterDeployment.objects.count() == 0


@pytest.mark.django_db
def test_cluster_deployment_requires_fresh_eph_agent_heartbeat(
    cluster_delivery_workspace,
):
    user, tenant, project, node, token, client = cluster_delivery_workspace
    node.last_seen_at = timezone.now() - datetime.timedelta(minutes=2)
    node.save(update_fields=["last_seen_at"])

    response = create_deployment(client, tenant, project, node)

    assert response.status_code == 409
    assert ClusterDeployment.objects.count() == 0


@pytest.mark.django_db
def test_cluster_delivery_runs_preflight_then_plan_and_persists_plan_result(
    cluster_delivery_workspace,
):
    user, tenant, project, node, token, client = cluster_delivery_workspace
    created = create_deployment(client, tenant, project, node)
    deployment_id = created.data["id"]
    agent_client = APIClient()

    preflight = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    preflight_command = preflight.data["command"]
    preflight_result = agent_client.post(
        f"/api/v1/agent-runtime/commands/{preflight_command['id']}/result/",
        {
            "node_id": str(node.id),
            "outcome": "succeeded",
            "result": {"cpu_count": 32, "root_disk_free_bytes": 1000000},
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_LEASE=preflight_command["lease_token"],
    )
    deployment = ClusterDeployment.objects.get(pk=deployment_id)
    plan_command = AgentCommand.objects.get(
        deployment=deployment,
        command_type="cluster.plan",
    )
    assert preflight_result.status_code == 200
    assert deployment.status == ClusterDeployment.Status.PLANNING
    assert plan_command.status == AgentCommand.Status.QUEUED

    plan = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    plan_command_data = plan.data["command"]
    plan_result = agent_client.post(
        f"/api/v1/agent-runtime/commands/{plan_command_data['id']}/result/",
        {
            "node_id": str(node.id),
            "outcome": "succeeded",
            "result": {
                "plan_status": "validated_dry_run",
                "cluster_name": "edge-lab",
                "kubernetes_version": "v1.32.2",
                "control_plane_replicas": 3,
                "worker_replicas": 4,
                "plan_hash": cluster_plan_hash(),
                "provisioning_ready": False,
                "missing_requirements": ["provider_versions.cluster_api"],
                "adapter_enabled": False,
                "side_effects": False,
            },
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_LEASE=plan_command_data["lease_token"],
    )

    deployment.refresh_from_db()
    assert plan_result.status_code == 200
    assert deployment.status == ClusterDeployment.Status.PLAN_READY
    assert deployment.plan_hash == cluster_plan_hash()
    assert deployment.plan_result["adapter_enabled"] is False
    assert deployment.plan_result["missing_requirements"] == [
        "provider_versions.cluster_api"
    ]


@pytest.mark.django_db
def test_cluster_deployment_cancel_stops_queued_commands(cluster_delivery_workspace):
    user, tenant, project, node, token, client = cluster_delivery_workspace
    created = create_deployment(client, tenant, project, node)
    deployment_id = created.data["id"]

    cancelled = client.post(
        f"/api/v1/cluster-deployments/{deployment_id}/cancel/",
        {},
        format="json",
    )

    command = AgentCommand.objects.get(deployment_id=deployment_id)
    assert cancelled.status_code == 200
    assert cancelled.data["status"] == ClusterDeployment.Status.CANCELLED
    assert command.status == AgentCommand.Status.CANCELLED


@pytest.mark.django_db
def test_deployment_cancel_marks_running_mutating_command_outcome_unknown(
    cluster_delivery_workspace,
):
    _, tenant, project, node, _, client = cluster_delivery_workspace
    created = create_deployment(client, tenant, project, node)
    command = AgentCommand.objects.get(deployment_id=created.data["id"])
    lease_token = uuid.uuid4()
    AgentCommand.objects.filter(pk=command.pk).update(
        command_type="cluster.capi.pivot.execute",
        status=AgentCommand.Status.RUNNING,
        lease_token=lease_token,
        lease_expires_at=timezone.now() + datetime.timedelta(seconds=60),
    )

    cancelled = client.post(
        f"/api/v1/cluster-deployments/{created.data['id']}/cancel/",
        {},
        format="json",
    )

    command.refresh_from_db()
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    assert cancelled.status_code == 200
    assert deployment.status == ClusterDeployment.Status.CANCELLED
    assert command.status == AgentCommand.Status.FAILED
    assert command.error["code"] == (
        "MUTATING_COMMAND_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED"
    )
    assert command.error["retryable"] is False
    assert command.lease_token is None
    assert command.lease_expires_at is None
    assert deployment.error["code"] == (
        "MUTATING_COMMAND_OUTCOME_UNKNOWN_MANUAL_RECONCILIATION_REQUIRED"
    )
    assert deployment.error["command_ids"] == [str(command.id)]



@pytest.mark.django_db
def test_target_postflight_rejects_dry_run_plan_even_if_prerequisites_are_present(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, eph_token, client = cluster_delivery_workspace
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="delivery-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(b"target-token").hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="target-postflight-dry-run",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    deployment.status = ClusterDeployment.Status.PLAN_READY
    deployment.plan_result = {
        "provisioning_ready": True,
        "adapter_enabled": False,
        "side_effects": False,
    }
    deployment.save(update_fields=["status", "plan_result"])

    response = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-postflight/",
        {
            "target_node_id": str(target_node.id),
            "confirmation": "cluster_provisioned",
        },
        format="json",
    )

    assert response.status_code == 409
    assert not AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.postflight",
    ).exists()


@pytest.mark.django_db
def test_target_postflight_rejects_plan_with_missing_prerequisites(cluster_delivery_workspace):
    user, tenant, project, eph_node, eph_token, client = cluster_delivery_workspace
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="delivery-target-incomplete",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(b"target-token").hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="target-postflight-incomplete",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    deployment.status = ClusterDeployment.Status.PLAN_READY
    deployment.plan_result = {"provisioning_ready": False}
    deployment.save(update_fields=["status", "plan_result"])

    response = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-postflight/",
        {
            "target_node_id": str(target_node.id),
            "confirmation": "cluster_provisioned",
        },
        format="json",
    )

    assert response.status_code == 409
    assert not AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.postflight",
    ).exists()



@pytest.mark.django_db
def test_cluster_plan_rejects_inconsistent_ready_flag_without_adapter(
    cluster_delivery_workspace,
):
    user, tenant, project, node, token, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        node,
        key="invalid-ready-plan",
    )
    agent_client = APIClient()
    preflight = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    preflight_command = preflight.data["command"]
    agent_client.post(
        f"/api/v1/agent-runtime/commands/{preflight_command['id']}/result/",
        {
            "node_id": str(node.id),
            "outcome": "succeeded",
            "result": {"cpu_count": 16},
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_LEASE=preflight_command["lease_token"],
    )
    plan = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    plan_command = plan.data["command"]
    result = agent_client.post(
        f"/api/v1/agent-runtime/commands/{plan_command['id']}/result/",
        {
            "node_id": str(node.id),
            "outcome": "succeeded",
            "result": {
                "plan_status": "validated_dry_run",
                "cluster_name": "edge-lab",
                "kubernetes_version": "v1.32.2",
                "control_plane_replicas": 3,
                "worker_replicas": 4,
                "plan_hash": cluster_plan_hash(),
                "provisioning_ready": True,
                "missing_requirements": [],
                "adapter_enabled": False,
                "side_effects": False,
            },
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_LEASE=plan_command["lease_token"],
    )

    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    assert result.status_code == 200
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "INVALID_OR_INCONSISTENT_CLUSTER_PLAN"


@pytest.mark.django_db
def test_target_postflight_rejects_boolean_node_count(cluster_delivery_workspace):
    user, tenant, project, eph_node, eph_token, client = cluster_delivery_workspace
    target_token = "target-postflight-bool-count"
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="delivery-target-bool-count",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(target_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="target-postflight-bool-count",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    deployment.status = ClusterDeployment.Status.WORKERS_READY
    deployment.plan_result = {
        "provisioning_ready": True,
        "adapter_enabled": True,
        "provisioning_completed": True,
    }
    deployment.save(update_fields=["status", "plan_result"])
    queued = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-postflight/",
        {
            "target_node_id": str(target_node.id),
            "confirmation": "cluster_provisioned",
        },
        format="json",
    )
    assert queued.status_code == 202
    repeated = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-postflight/",
        {
            "target_node_id": str(target_node.id),
            "confirmation": "cluster_provisioned",
        },
        format="json",
    )
    assert repeated.status_code == 200
    assert repeated.data["command_id"] == queued.data["command_id"]

    agent_client = APIClient()
    polled = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(target_node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {target_token}",
    )
    command = polled.data["command"]
    submitted = agent_client.post(
        f"/api/v1/agent-runtime/commands/{command['id']}/result/",
        {
            "node_id": str(target_node.id),
            "outcome": "succeeded",
            "result": {
                "node_count": True,
                "ready": True,
                "not_ready_nodes": [],
            },
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {target_token}",
        HTTP_X_AGENT_LEASE=command["lease_token"],
    )

    deployment.refresh_from_db()
    assert submitted.status_code == 200
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "TARGET_CLUSTER_POSTFLIGHT_FAILED"


@pytest.mark.django_db
def test_target_agent_restores_and_advances_durable_deployment_checkpoint(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, eph_token, client = cluster_delivery_workspace
    target_token = "target-checkpoint-agent-token"
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="checkpoint-target-agent",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(target_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="durable-target-checkpoint",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    deployment.status = ClusterDeployment.Status.CONTROL_PLANE_READY
    deployment.save(update_fields=["status"])
    handoff = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-agent/handoff/",
        {"target_node_id": str(target_node.id)},
        format="json",
    )
    assert handoff.status_code == 202
    assert handoff.data["worker_provisioning_allowed"] is False
    agent_client = APIClient()
    heartbeat = agent_client.post(
        "/api/v1/agent-runtime/heartbeat/",
        {"node_id": str(target_node.id), "agent_version": "0.1.0"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {target_token}",
    )
    assert heartbeat.status_code == 200
    assert heartbeat.data["active_deployments"] == [
        {
            "deployment_id": str(deployment.id),
            "cluster_name": deployment.cluster_name,
            "status": ClusterDeployment.Status.TARGET_AGENT_PENDING,
            "checkpoint_endpoint": (
                f"/api/v1/agent-runtime/deployments/{deployment.id}/checkpoint/"
            ),
        }
    ]

    url = f"/api/v1/agent-runtime/deployments/{deployment.id}/checkpoint/"
    headers = {"HTTP_AUTHORIZATION": f"Bearer {target_token}"}
    empty = agent_client.get(url, {"node_id": str(target_node.id)}, **headers)
    assert empty.status_code == 200
    assert empty.data["resume_available"] is False
    assert empty.data["generation"] == 1

    checkpoint_payload = {
        "node_id": str(target_node.id),
        "generation": 1,
        "sequence": 1,
        "phase": "target_agent_bootstrap",
        "data": {
            "cluster_name": "edge-lab",
            "completed_steps": [],
            "pending_step": "target_agent_bootstrap",
        },
    }
    saved = agent_client.post(url, checkpoint_payload, format="json", **headers)
    assert saved.status_code == 201
    assert saved.data["sequence"] == 1
    assert ClusterDeploymentCheckpoint.objects.filter(
        deployment=deployment,
        agent_node=target_node,
    ).count() == 1

    restored = agent_client.get(url, {"node_id": str(target_node.id)}, **headers)
    assert restored.status_code == 200
    assert restored.data["resume_available"] is True
    assert restored.data["data"] == checkpoint_payload["data"]

    checkpoint_payload["sequence"] = 2
    checkpoint_payload["phase"] = "capi_pivot_preflight"
    checkpoint_payload["data"]["completed_steps"].append("target_agent_bootstrap")
    checkpoint_payload["data"]["pending_step"] = "capi_pivot_preflight"
    advanced = agent_client.post(url, checkpoint_payload, format="json", **headers)
    assert advanced.status_code == 201
    checkpoint = ClusterDeploymentCheckpoint.objects.get(deployment=deployment)
    assert checkpoint.sequence == 2
    assert checkpoint.phase == "capi_pivot_preflight"

    deployment.plan_result = {
        "capi_pivot_source_preflight": {
            "source_cluster_identity": {"uid": "checkpoint-source-cluster-uid"}
        },
        "dual_agent": {
            "target_agent_owns_cluster": True,
            "target_agent_cluster_uid": "checkpoint-source-cluster-uid",
        },
    }
    deployment.save(update_fields=["plan_result"])
    checkpoint_payload["sequence"] = 3
    checkpoint_payload["phase"] = "capi_pivot_verified"
    checkpoint_payload["data"]["completed_steps"].append("capi_pivot_preflight")
    advanced = agent_client.post(url, checkpoint_payload, format="json", **headers)
    assert advanced.status_code == 201
    checkpoint = ClusterDeploymentCheckpoint.objects.get(deployment=deployment)
    assert checkpoint.sequence == 3
    assert checkpoint.phase == "capi_pivot_verified"

    checkpoint_payload["sequence"] = 5
    skipped = agent_client.post(url, checkpoint_payload, format="json", **headers)
    assert skipped.status_code == 409
    assert skipped.data["expected_sequence"] == 4
    assert skipped.data["received_sequence"] == 5
    checkpoint.refresh_from_db()
    assert checkpoint.sequence == 3
    assert checkpoint.phase == "capi_pivot_verified"

    checkpoint_payload["sequence"] = 4
    contiguous = agent_client.post(url, checkpoint_payload, format="json", **headers)
    assert contiguous.status_code == 201
    checkpoint.refresh_from_db()
    assert checkpoint.sequence == 4

    checkpoint_payload["sequence"] = 3
    stale = agent_client.post(url, checkpoint_payload, format="json", **headers)
    assert stale.status_code == 409
    checkpoint.refresh_from_db()
    assert checkpoint.sequence == 4


@pytest.mark.django_db
@pytest.mark.parametrize(
    "deployment_status",
    [ClusterDeployment.Status.PROVISIONING, ClusterDeployment.Status.WAITING_APPROVAL],
)
def test_target_agent_can_write_checkpoint_during_active_provisioning_states(
    cluster_delivery_workspace,
    deployment_status,
):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    target_token = "target-active-checkpoint-token"
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="target-active-checkpoint-agent",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(target_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="active-checkpoint-" + deployment_status.lower(),
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    deployment.status = ClusterDeployment.Status.CONTROL_PLANE_READY
    deployment.save(update_fields=["status"])
    handoff = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-agent/handoff/",
        {"target_node_id": str(target_node.id)},
        format="json",
    )
    assert handoff.status_code == 202
    deployment.refresh_from_db()
    deployment.status = deployment_status
    deployment.save(update_fields=["status"])

    response = APIClient().post(
        f"/api/v1/agent-runtime/deployments/{deployment.id}/checkpoint/",
        {
            "node_id": str(target_node.id),
            "generation": 1,
            "sequence": 1,
            "phase": "target_agent_bootstrap",
            "data": {"pending_step": "target_agent_bootstrap"},
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {target_token}",
    )

    assert response.status_code == 201
    assert response.data["sequence"] == 1
    checkpoint = ClusterDeploymentCheckpoint.objects.get(deployment=deployment)
    assert checkpoint.agent_node == target_node
    assert checkpoint.phase == "target_agent_bootstrap"


@pytest.mark.django_db
def test_target_agent_checkpoint_blocks_phase_skips_and_eph_reclaim(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    target_token = "target-checkpoint-gate-token"
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="checkpoint-gate-agent",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(target_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="checkpoint-phase-gates",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    deployment.status = ClusterDeployment.Status.CONTROL_PLANE_READY
    deployment.save(update_fields=["status"])
    handoff = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-agent/handoff/",
        {"target_node_id": str(target_node.id)},
        format="json",
    )
    assert handoff.status_code == 202

    agent_client = APIClient()
    url = f"/api/v1/agent-runtime/deployments/{deployment.id}/checkpoint/"
    headers = {"HTTP_AUTHORIZATION": f"Bearer {target_token}"}
    bootstrap = agent_client.post(
        url,
        {
            "node_id": str(target_node.id),
            "generation": 1,
            "sequence": 1,
            "phase": "target_agent_bootstrap",
            "data": {},
        },
        format="json",
        **headers,
    )
    assert bootstrap.status_code == 201

    skipped = agent_client.post(
        url,
        {
            "node_id": str(target_node.id),
            "generation": 1,
            "sequence": 2,
            "phase": "capi_pivot_verified",
            "data": {},
        },
        format="json",
        **headers,
    )
    assert skipped.status_code == 409

    reclaim = agent_client.post(
        url,
        {
            "node_id": str(target_node.id),
            "generation": 1,
            "sequence": 2,
            "phase": "eph_reclaim_pending",
            "data": {"intent_id": "must-not-authorize-reclaim"},
        },
        format="json",
        **headers,
    )
    assert reclaim.status_code == 409
    assert reclaim.data["code"] == "EPH_RECLAIM_EXECUTOR_NOT_ENABLED"
    checkpoint = ClusterDeploymentCheckpoint.objects.get(deployment=deployment)
    assert checkpoint.sequence == 1
    assert checkpoint.phase == "target_agent_bootstrap"



@pytest.mark.django_db
@pytest.mark.parametrize("worker_count", [3, 5])
def test_worker_pool_checkpoint_must_match_requested_replica_count(
    cluster_delivery_workspace,
    worker_count,
):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    target_token = "target-worker-count-gate-token"
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="worker-count-gate-agent",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(target_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="worker-count-gate-" + str(worker_count),
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    deployment.status = ClusterDeployment.Status.CONTROL_PLANE_READY
    deployment.save(update_fields=["status"])
    handoff = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-agent/handoff/",
        {"target_node_id": str(target_node.id)},
        format="json",
    )
    assert handoff.status_code == 202
    deployment.refresh_from_db()
    deployment.plan_result = {
        **(deployment.plan_result or {}),
        "dual_agent": {"target_agent_owns_cluster": True},
    }
    deployment.save(update_fields=["plan_result"])
    ClusterDeploymentCheckpoint.objects.create(
        deployment=deployment,
        agent_node=target_node,
        generation=1,
        sequence=1,
        phase="worker_provisioning",
        data={},
    )

    response = APIClient().post(
        f"/api/v1/agent-runtime/deployments/{deployment.id}/checkpoint/",
        {
            "node_id": str(target_node.id),
            "generation": 1,
            "sequence": 2,
            "phase": "worker_pool_verified",
            "data": {
                "worker_count": worker_count,
                "ready_worker_count": worker_count,
                "not_ready_worker_names": [],
            },
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {target_token}",
    )

    assert response.status_code == 409
    assert response.data["code"] == "WORKER_POOL_NOT_FULLY_READY"
    assert response.data["expected_worker_count"] == 4
    checkpoint = ClusterDeploymentCheckpoint.objects.get(deployment=deployment)
    assert checkpoint.phase == "worker_provisioning"
    assert checkpoint.sequence == 1



@pytest.mark.django_db
def test_worker_pool_checkpoint_accepts_exact_requested_replica_count(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    target_token = "target-worker-count-success-token"
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="worker-count-success-agent",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(target_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="worker-count-gate-exact",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    deployment.status = ClusterDeployment.Status.CONTROL_PLANE_READY
    deployment.save(update_fields=["status"])
    handoff = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-agent/handoff/",
        {"target_node_id": str(target_node.id)},
        format="json",
    )
    assert handoff.status_code == 202
    deployment.refresh_from_db()
    deployment.plan_result = {
        **(deployment.plan_result or {}),
        "dual_agent": {"target_agent_owns_cluster": True},
    }
    deployment.save(update_fields=["plan_result"])
    ClusterDeploymentCheckpoint.objects.create(
        deployment=deployment,
        agent_node=target_node,
        generation=1,
        sequence=1,
        phase="worker_provisioning",
        data={},
    )

    response = APIClient().post(
        f"/api/v1/agent-runtime/deployments/{deployment.id}/checkpoint/",
        {
            "node_id": str(target_node.id),
            "generation": 1,
            "sequence": 2,
            "phase": "worker_pool_verified",
            "data": {
                "worker_count": 4,
                "ready_worker_count": 4,
                "not_ready_worker_names": [],
            },
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {target_token}",
    )

    assert response.status_code == 201
    assert response.data["phase"] == "worker_pool_verified"
    checkpoint = ClusterDeploymentCheckpoint.objects.get(deployment=deployment)
    assert checkpoint.sequence == 2


@pytest.mark.django_db
def test_target_agent_checkpoint_rejects_replay_and_credentials(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, eph_token, client = cluster_delivery_workspace
    target_token = "target-checkpoint-replay-token"
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="checkpoint-replay-agent",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(target_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="durable-target-checkpoint-replay",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    deployment.status = ClusterDeployment.Status.CONTROL_PLANE_READY
    deployment.save(update_fields=["status"])
    handoff = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-agent/handoff/",
        {"target_node_id": str(target_node.id)},
        format="json",
    )
    assert handoff.status_code == 202
    assert handoff.data["worker_provisioning_allowed"] is False
    agent_client = APIClient()
    url = f"/api/v1/agent-runtime/deployments/{deployment.id}/checkpoint/"
    headers = {"HTTP_AUTHORIZATION": f"Bearer {target_token}"}
    payload = {
        "node_id": str(target_node.id),
        "generation": 1,
        "sequence": 1,
        "phase": "target_agent_bootstrap",
        "data": {"completed_steps": []},
    }
    first = agent_client.post(url, payload, format="json", **headers)
    repeated = agent_client.post(url, payload, format="json", **headers)
    assert first.status_code == 201
    assert repeated.status_code == 200
    assert repeated.data["idempotent"] is True

    advanced_payload = {
        **payload,
        "sequence": 2,
        "phase": "capi_pivot_preflight",
        "data": {"completed_steps": ["target_agent_bootstrap"]},
    }
    advanced = agent_client.post(url, advanced_payload, format="json", **headers)
    assert advanced.status_code == 201
    repeated_advanced = agent_client.post(
        url, advanced_payload, format="json", **headers
    )
    assert repeated_advanced.status_code == 200
    assert repeated_advanced.data["idempotent"] is True

    replay = dict(advanced_payload)
    replay["data"] = {"completed_steps": ["forged"]}
    rejected_replay = agent_client.post(url, replay, format="json", **headers)
    assert rejected_replay.status_code == 409

    credential_payload = dict(payload)
    credential_payload["sequence"] = 2
    credential_payload["data"] = {"access_token": "must-not-persist"}
    rejected_secret = agent_client.post(
        url,
        credential_payload,
        format="json",
        **headers,
    )
    assert rejected_secret.status_code == 400
    checkpoint = ClusterDeploymentCheckpoint.objects.get(deployment=deployment)
    assert checkpoint.sequence == 2
    assert "access_token" not in json.dumps(checkpoint.data)


@pytest.mark.django_db
def test_target_agent_handoff_dispatches_and_validates_capi_pivot_preflight(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, eph_token, client = cluster_delivery_workspace
    target_token = "target-capi-preflight-token"
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="capi-preflight-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(target_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="target-capi-preflight-dispatch",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    AgentCommand.objects.filter(
        deployment=deployment,
        command_type="host.preflight",
    ).update(
        status=AgentCommand.Status.SUCCEEDED,
        finished_at=timezone.now(),
    )
    deployment.status = ClusterDeployment.Status.CONTROL_PLANE_READY
    deployment.save(update_fields=["status"])

    handoff = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-agent/handoff/",
        {"target_node_id": str(target_node.id)},
        format="json",
    )

    assert handoff.status_code == 202
    assert handoff.data["preflight_command_id"] is None
    assert handoff.data["preflight_command_status"] == "waiting_for_source_inventory"
    assert handoff.data["worker_provisioning_allowed"] is False

    source_command = AgentCommand.objects.get(
        deployment=deployment,
        command_type="cluster.capi.pivot.source.preflight",
    )
    assert source_command.node_id == eph_node.id
    assert handoff.data["source_preflight_command_id"] == str(source_command.id)

    source_client = APIClient()
    source_poll = source_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(eph_node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
    )
    assert source_poll.status_code == 200
    source_command_data = source_poll.data["command"]
    assert source_command_data["command_type"] == "cluster.capi.pivot.source.preflight"
    source_groups = {
        "cluster.x-k8s.io",
        "bootstrap.cluster.x-k8s.io",
        "controlplane.cluster.x-k8s.io",
        "infrastructure.cluster.x-k8s.io",
        "metal3.io",
    }
    source_resources = [
        "cluster.x-k8s.io/clusters",
        "cluster.x-k8s.io/machines",
        "cluster.x-k8s.io/machinesets",
        "cluster.x-k8s.io/machinedeployments",
        "bootstrap.cluster.x-k8s.io/kubeadmconfigs",
        "bootstrap.cluster.x-k8s.io/kubeadmconfigtemplates",
        "controlplane.cluster.x-k8s.io/kubeadmcontrolplanes",
        "infrastructure.cluster.x-k8s.io/metal3clusters",
        "infrastructure.cluster.x-k8s.io/metal3machines",
        "infrastructure.cluster.x-k8s.io/metal3machinetemplates",
        "metal3.io/baremetalhosts",
    ]
    source_result = {
        "source_management_plane_ready": True,
        "source_cluster_identity": {
            "name": "edge-lab",
            "namespace": "capi-system",
            "uid": "cluster-uid",
            "api_version": "cluster.x-k8s.io/v1beta1",
        },
        "cluster_name": "edge-lab",
        "provider_versions": {
            group: "v1alpha1" if group == "metal3.io" else "v1beta1"
            for group in source_groups
        },
        "resource_counts": {
            resource: 1 if resource == "cluster.x-k8s.io/clusters" else 0
            for resource in source_resources
        },
        "inventory": [
            {
                "api_version": "cluster.x-k8s.io/v1beta1",
                "kind": "Cluster",
                "resource": "cluster.x-k8s.io/clusters",
                "name": "edge-lab",
                "namespace": "capi-system",
                "uid": "cluster-uid",
                "owner_references": [],
                "owner_references_truncated": False,
                "cluster_name": "edge-lab",
            }
        ],
        "owner_graph": {
            "complete": True,
            "root_cluster_uid": "cluster-uid",
            "object_count": 1,
            "unique_uid_count": 1,
            "reachable_object_count": 1,
            "cluster_labeled_object_count": 0,
            "duplicate_uids": [],
            "truncated_owner_references": [],
            "unresolved_owner_references": [],
            "orphaned_cluster_objects": [],
        },
        "inventory_truncated": False,
        "inventory_summary_truncated": False,
        "inventory_total_count": 1,
        "missing_resources": [],
        "pagination_required": [],
        "controllers": [
            {
                "namespace": namespace,
                "deployment": deployment_name,
                "ready": True,
            }
            for namespace, deployment_name in [
                ("capi-system", "capi-controller-manager"),
                (
                    "capi-kubeadm-bootstrap-system",
                    "capi-kubeadm-bootstrap-controller-manager",
                ),
                (
                    "capi-kubeadm-control-plane-system",
                    "capi-kubeadm-control-plane-controller-manager",
                ),
                ("capm3-system", "capm3-controller-manager"),
            ]
        ],
        "blockers": [],
        "side_effects": False,
    }
    source_submitted = source_client.post(
        f"/api/v1/agent-runtime/commands/{source_command_data['id']}/result/",
        {
            "node_id": str(eph_node.id),
            "outcome": "succeeded",
            "result": source_result,
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
        HTTP_X_AGENT_LEASE=source_command_data["lease_token"],
    )
    deployment.refresh_from_db()
    assert source_submitted.status_code == 200
    assert (
        deployment.plan_result["capi_pivot_source_preflight"]["preflight_validated"]
        is True
    )
    assert deployment.plan_result["dual_agent"]["phase"] == "target_preflight_pending"
    command = AgentCommand.objects.get(
        deployment=deployment,
        command_type="cluster.capi.pivot.target.preflight",
    )
    assert command.node_id == target_node.id
    assert command.payload["namespace"] == "capi-system"

    agent_client = APIClient()
    polled = agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(target_node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {target_token}",
    )
    assert polled.status_code == 200
    command_data = polled.data["command"]
    assert command_data["command_type"] == "cluster.capi.pivot.target.preflight"

    result = {
        "target_management_plane_ready": True,
        "pivot_execution_allowed": True,
        "cluster_name": "edge-lab",
        "namespace": "capi-system",
        "provider_versions": {
            "cluster.x-k8s.io": "v1beta1",
            "bootstrap.cluster.x-k8s.io": "v1beta1",
            "controlplane.cluster.x-k8s.io": "v1beta1",
            "infrastructure.cluster.x-k8s.io": "v1beta1",
            "metal3.io": "v1alpha1",
        },
        "missing_resources": [],
        "controllers": [
            {
                "namespace": namespace,
                "deployment": deployment_name,
                "ready": True,
            }
            for namespace, deployment_name in [
                ("capi-system", "capi-controller-manager"),
                (
                    "capi-kubeadm-bootstrap-system",
                    "capi-kubeadm-bootstrap-controller-manager",
                ),
                (
                    "capi-kubeadm-control-plane-system",
                    "capi-kubeadm-control-plane-controller-manager",
                ),
                ("capm3-system", "capm3-controller-manager"),
            ]
        ],
        "cluster_name_conflicts": [],
        "blockers": [],
        "side_effects": False,
    }
    submitted = agent_client.post(
        f"/api/v1/agent-runtime/commands/{command_data['id']}/result/",
        {
            "node_id": str(target_node.id),
            "outcome": "succeeded",
            "result": result,
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {target_token}",
        HTTP_X_AGENT_LEASE=command_data["lease_token"],
    )

    deployment.refresh_from_db()
    assert submitted.status_code == 200
    assert deployment.status == ClusterDeployment.Status.TARGET_AGENT_PENDING
    assert deployment.plan_result["capi_pivot_preflight"]["preflight_validated"] is True
    assert deployment.plan_result["capi_pivot_preflight"]["side_effects"] is False
    assert deployment.plan_result["capi_pivot_provider_compatibility"] == {
        "checked": True,
        "basis": "served_api_versions_only",
        "compatible": True,
        "mismatches": [],
        "execution_allowed": False,
    }
    assert deployment.plan_result["dual_agent"]["phase"] == "pivot_waiting_approval"
    pivot_command = AgentCommand.objects.get(
        deployment=deployment,
        command_type="cluster.capi.pivot.execute",
    )
    assert pivot_command.status == AgentCommand.Status.WAITING_APPROVAL
    assert pivot_command.node_id == eph_node.id
    assert deployment.error is None

    approver = get_user_model().objects.create_user(
        username="dual-agent-e2e-approver",
        password="test-password",
    )
    TenantMembership.objects.create(
        tenant=tenant,
        user=approver,
        role=TenantMembership.Role.ADMIN,
    )
    approver_client = APIClient()
    approver_client.force_authenticate(user=approver)
    approved = approver_client.post(
        f"/api/v1/agent-commands/{pivot_command.id}/approve/",
        {"plan_hash": pivot_command.plan_hash},
        format="json",
    )
    assert approved.status_code == 200
    assert approved.data["status"] == AgentCommand.Status.QUEUED

    eph_agent_client = APIClient()
    pivot_poll = eph_agent_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(eph_node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
    )
    assert pivot_poll.status_code == 200
    pivot_command_data = pivot_poll.data["command"]
    assert pivot_command_data["id"] == str(pivot_command.id)
    assert pivot_command_data["command_type"] == "cluster.capi.pivot.execute"

    pivot_result = {
        "pivot_executed": False,
        "pivot_simulated": True,
        "execution_mode": "preflight_verified_simulation",
        "simulation_status": "ready_for_target_agent_acknowledgement",
        "dry_run_passed": True,
        "source_namespace_scope_verified": True,
        "target_management_plane_verified": True,
        "cluster_name": "edge-lab",
        "namespace": "capi-system",
        "ownership_transferred": False,
        "target_agent_acknowledgement_required": True,
        "postflight_required": True,
        "side_effects": False,
    }
    pivot_submitted = eph_agent_client.post(
        f"/api/v1/agent-runtime/commands/{pivot_command_data['id']}/result/",
        {
            "node_id": str(eph_node.id),
            "outcome": "succeeded",
            "result": pivot_result,
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
        HTTP_X_AGENT_LEASE=pivot_command_data["lease_token"],
    )

    deployment.refresh_from_db()
    pivot_command.refresh_from_db()
    assert pivot_submitted.status_code == 200
    assert pivot_command.status == AgentCommand.Status.SUCCEEDED
    assert pivot_command.approved_by_id == approver.id
    assert deployment.error is None
    assert deployment.plan_result["dual_agent"]["phase"] == "pivot_simulation_ready"
    assert deployment.plan_result["dual_agent"]["pivot_simulated"] is True
    assert deployment.plan_result["dual_agent"]["pivot_completed"] is False
    assert deployment.plan_result["dual_agent"]["target_agent_owns_cluster"] is False
    assert deployment.plan_result["dual_agent"]["eph_reclaim_allowed"] is False
    assert not AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.capi.pivot.target.verify",
    ).exists()
    assert not AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.capi.worker.preflight",
    ).exists()


@pytest.mark.django_db
def test_source_capi_pivot_preflight_rejects_malformed_inventory_counts(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, eph_token, client = cluster_delivery_workspace
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="malformed-source-inventory-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(b"malformed-target-token").hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="malformed-source-inventory",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    AgentCommand.objects.filter(
        deployment=deployment,
        command_type="host.preflight",
    ).update(
        status=AgentCommand.Status.SUCCEEDED,
        finished_at=timezone.now(),
    )
    deployment.status = ClusterDeployment.Status.CONTROL_PLANE_READY
    deployment.save(update_fields=["status"])

    handoff = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-agent/handoff/",
        {"target_node_id": str(target_node.id)},
        format="json",
    )
    assert handoff.status_code == 202

    source_client = APIClient()
    polled = source_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(eph_node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
    )
    command = polled.data["command"]
    assert command["command_type"] == "cluster.capi.pivot.source.preflight"

    submitted = source_client.post(
        f"/api/v1/agent-runtime/commands/{command['id']}/result/",
        {
            "node_id": str(eph_node.id),
            "outcome": "succeeded",
            "result": {
                "cluster_name": "edge-lab",
                "provider_versions": None,
                "resource_counts": None,
                "inventory": [],
                "inventory_truncated": False,
                "missing_resources": [],
                "pagination_required": [],
                "controllers": [],
                "blockers": [],
                "side_effects": False,
                "source_management_plane_ready": True,
            },
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
        HTTP_X_AGENT_LEASE=command["lease_token"],
    )

    deployment.refresh_from_db()
    assert submitted.status_code == 200
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "INVALID_CAPI_SOURCE_PREFLIGHT_RESULT"


@pytest.mark.django_db
def test_capi_pivot_provider_version_mismatch_fails_closed(
    cluster_delivery_workspace,
):
    _, tenant, project, eph_node, _, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="provider-version-mismatch",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    source_versions = {
        "cluster.x-k8s.io": "v1beta1",
        "bootstrap.cluster.x-k8s.io": "v1beta1",
        "controlplane.cluster.x-k8s.io": "v1beta1",
        "infrastructure.cluster.x-k8s.io": "v1beta1",
        "metal3.io": "v1alpha1",
    }
    target_versions = {**source_versions, "metal3.io": "v1beta1"}
    deployment.plan_result = {
        "capi_pivot_source_preflight": {
            "preflight_validated": True,
            "provider_versions": source_versions,
        },
        "capi_pivot_preflight": {
            "preflight_validated": True,
            "provider_versions": target_versions,
        },
    }
    deployment.save(update_fields=["plan_result"])

    _update_capi_pivot_provider_compatibility(deployment)

    deployment.refresh_from_db()
    compatibility = deployment.plan_result["capi_pivot_provider_compatibility"]
    assert compatibility["checked"] is True
    assert compatibility["compatible"] is False
    assert compatibility["mismatches"] == [
        {
            "group": "metal3.io",
            "source": "v1alpha1",
            "target": "v1beta1",
        }
    ]
    assert compatibility["execution_allowed"] is False


@pytest.mark.django_db
def test_capi_pivot_api_version_mismatch_never_allows_execution(
    cluster_delivery_workspace,
):
    _, tenant, project, eph_node, _, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="provider-api-version-mismatch",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    groups = {
        "cluster.x-k8s.io",
        "bootstrap.cluster.x-k8s.io",
        "controlplane.cluster.x-k8s.io",
        "infrastructure.cluster.x-k8s.io",
        "metal3.io",
    }
    source_versions = {group: "v1beta1" for group in groups}
    target_versions = dict(source_versions)
    target_versions["metal3.io"] = "v1alpha1"
    deployment.plan_result = {
        "capi_pivot_source_preflight": {
            "preflight_validated": True,
            "provider_versions": source_versions,
        },
        "capi_pivot_preflight": {
            "preflight_validated": True,
            "provider_versions": target_versions,
        },
    }
    deployment.save(update_fields=["plan_result"])

    _update_capi_pivot_provider_compatibility(deployment)

    deployment.refresh_from_db()
    compatibility = deployment.plan_result["capi_pivot_provider_compatibility"]
    assert compatibility["checked"] is True
    assert compatibility["basis"] == "served_api_versions_only"
    assert compatibility["compatible"] is False
    assert compatibility["mismatches"] == [
        {
            "group": "metal3.io",
            "source": "v1beta1",
            "target": "v1alpha1",
        }
    ]
    assert compatibility["execution_allowed"] is False



@pytest.mark.django_db
@pytest.mark.parametrize("approval_valid", [True, False])
def test_dual_agent_pivot_simulation_requires_independent_approval_and_does_not_transfer_ownership(
    cluster_delivery_workspace,
    approval_valid,
):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="dual-agent-pivot-simulation",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    target_token = "dual-agent-target-token"
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="dual-agent-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(target_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    deployment.target_node = target_node
    deployment.spec = {**deployment.spec, "capi_namespace": "metal3"}
    deployment.plan_result = {
        "capi_pivot_source_preflight": {
            "preflight_validated": True,
            "source_cluster_identity": {
                "name": "edge-lab",
                "namespace": "metal3",
                "uid": "source-cluster-uid",
            },
        },
        "capi_pivot_preflight": {"preflight_validated": True},
        "capi_pivot_provider_compatibility": {
            "checked": True,
            "compatible": True,
            "execution_allowed": False,
        },
    }
    deployment.status = ClusterDeployment.Status.TARGET_AGENT_PENDING
    deployment.save(update_fields=["target_node", "spec", "plan_result", "status", "updated_at"])

    _maybe_queue_capi_pivot(deployment)
    pivot_command = AgentCommand.objects.get(
        deployment=deployment,
        command_type="cluster.capi.pivot.execute",
    )
    assert pivot_command.node_id == eph_node.id
    assert pivot_command.status == AgentCommand.Status.WAITING_APPROVAL

    if approval_valid:
        approver = get_user_model().objects.create_user(
            username="dual-agent-pivot-approver",
            password="test-password",
        )
        TenantMembership.objects.create(
            tenant=tenant,
            user=approver,
            role=TenantMembership.Role.ADMIN,
        )
        pivot_command.status = AgentCommand.Status.SUCCEEDED
        pivot_command.approved_by = approver
        pivot_command.approved_at = timezone.now()
        pivot_command.approved_plan_hash = pivot_command.plan_hash
        pivot_command.save(
            update_fields=[
                "status",
                "approved_by",
                "approved_at",
                "approved_plan_hash",
            ]
        )

    handle_deployment_command_result(
        pivot_command,
        "succeeded",
        {
            "pivot_executed": False,
            "pivot_simulated": True,
            "execution_mode": "preflight_verified_simulation",
            "simulation_status": "ready_for_target_agent_acknowledgement",
            "dry_run_passed": True,
            "source_namespace_scope_verified": True,
            "target_management_plane_verified": True,
            "cluster_name": "edge-lab",
            "namespace": "metal3",
            "ownership_transferred": False,
            "target_agent_acknowledgement_required": True,
            "postflight_required": True,
            "side_effects": False,
        },
        None,
    )

    deployment.refresh_from_db()
    if not approval_valid:
        assert deployment.status == ClusterDeployment.Status.FAILED
        assert deployment.error["code"] == "INVALID_CAPI_PIVOT_SIMULATION_RESULT"
        assert deployment.plan_result.get("dual_agent", {}).get(
            "target_agent_owns_cluster"
        ) is not True
        return

    dual_agent = deployment.plan_result["dual_agent"]
    assert dual_agent["phase"] == "pivot_simulation_ready"
    assert dual_agent["pivot_simulated"] is True
    assert dual_agent["pivot_completed"] is False
    assert dual_agent["target_agent_owns_cluster"] is False
    assert dual_agent["eph_reclaim_allowed"] is False
    assert deployment.error is None
    assert not AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.capi.pivot.target.verify",
    ).exists()
    assert not AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.capi.worker.preflight",
    ).exists()


@pytest.mark.django_db
def test_control_plane_rejects_pivot_simulation_that_claims_side_effects(
    cluster_delivery_workspace,
):
    _, _, _, eph_node, _, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        cluster_delivery_workspace[1],
        cluster_delivery_workspace[2],
        eph_node,
        key="unsafe-pivot-simulation-result",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    deployment.plan_result = {
        "capi_pivot_source_preflight": {
            "source_cluster_identity": {"uid": "source-cluster-uid"},
        },
    }
    deployment.save(update_fields=["plan_result", "updated_at"])
    command = AgentCommand.objects.create(
        tenant=deployment.tenant,
        project=deployment.project,
        deployment=deployment,
        node=eph_node,
        requested_by=deployment.requested_by,
        command_type="cluster.capi.pivot.execute",
        payload={},
        idempotency_key="unsafe-pivot-simulation-result",
        status=AgentCommand.Status.RUNNING,
    )

    handle_deployment_command_result(
        command,
        "succeeded",
        {
            "pivot_executed": False,
            "pivot_simulated": True,
            "dry_run_passed": True,
            "source_namespace_scope_verified": True,
            "target_management_plane_verified": True,
            "cluster_name": deployment.cluster_name,
            "namespace": "default",
            "ownership_transferred": False,
            "target_agent_acknowledgement_required": True,
            "postflight_required": True,
            "side_effects": True,
        },
        None,
    )

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "INVALID_CAPI_PIVOT_SIMULATION_RESULT"
    assert deployment.plan_result.get("dual_agent", {}).get("target_agent_owns_cluster") is not True

@pytest.mark.django_db
def test_control_plane_rejects_postflight_without_complete_capi_worker_inventory(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="incomplete-capi-worker-postflight",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="postflight-worker-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(b"postflight-worker-token").hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    deployment.target_node = target_node
    deployment.status = ClusterDeployment.Status.POSTFLIGHT
    deployment.save(update_fields=["target_node", "status", "updated_at"])
    command = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target_node,
        requested_by=user,
        deployment=deployment,
        command_type="cluster.postflight",
        payload={"expected_worker_replicas": 4},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key=f"incomplete-capi-worker-postflight:{deployment.id}",
    )

    handle_deployment_command_result(
        command,
        "succeeded",
        {
            "cluster_name": "edge-lab",
            "namespace": "default",
            "expected_worker_replicas": 4,
            "node_count": 5,
            "ready": True,
            "not_ready_nodes": [],
            "worker_machine_count": 4,
            "ready_worker_machine_count": 3,
            "not_ready_worker_machine_names": ["edge-lab-worker-04"],
            "worker_node_names": ["worker-01", "worker-02", "worker-03"],
            "capi_worker_inventory_complete": False,
            "side_effects": False,
        },
        None,
    )

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "TARGET_CLUSTER_POSTFLIGHT_FAILED"
    assert deployment.error["not_ready_worker_machine_names"] == [
        "edge-lab-worker-04"
    ]


@pytest.mark.django_db
def test_control_plane_accepts_postflight_only_when_all_capi_workers_are_ready(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="complete-capi-worker-postflight",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="complete-postflight-worker-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(b"complete-postflight-token").hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    deployment.target_node = target_node
    deployment.status = ClusterDeployment.Status.POSTFLIGHT
    deployment.save(update_fields=["target_node", "status", "updated_at"])
    command = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target_node,
        requested_by=user,
        deployment=deployment,
        command_type="cluster.postflight",
        payload={"expected_worker_replicas": 4},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key=f"complete-capi-worker-postflight:{deployment.id}",
    )

    handle_deployment_command_result(
        command,
        "succeeded",
        {
            "cluster_name": "edge-lab",
            "namespace": "default",
            "expected_worker_replicas": 4,
            "node_count": 5,
            "ready": True,
            "not_ready_nodes": [],
            "worker_machine_count": 4,
            "ready_worker_machine_count": 4,
            "not_ready_worker_machine_names": [],
            "worker_node_names": [
                "worker-01",
                "worker-02",
                "worker-03",
                "worker-04",
            ],
            "capi_worker_inventory_complete": True,
            "side_effects": False,
        },
        None,
    )

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.SUCCEEDED
    assert deployment.error is None
    assert (
        deployment.plan_result["postflight"]["capi_worker_inventory_complete"] is True
    )


@pytest.mark.django_db
def test_control_plane_rejects_partial_target_machine_readiness_claim(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="partial-target-readiness",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="partial-readiness-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(b"partial-readiness-token").hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    deployment.target_node = target_node
    deployment.spec = {**deployment.spec, "capi_namespace": "metal3"}
    deployment.plan_result = {
        "capi_pivot_source_preflight": {
            "source_cluster_identity": {"uid": "source-cluster-uid"}
        },
        "dual_agent": {
            "phase": "target_agent_verification_pending",
            "target_agent_owns_cluster": False,
            "eph_reclaim_allowed": False,
        },
    }
    deployment.save(update_fields=["target_node", "spec", "plan_result", "updated_at"])
    command = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target_node,
        requested_by=user,
        deployment=deployment,
        command_type="cluster.capi.pivot.target.verify",
        payload={},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key=f"partial-target-readiness:{deployment.id}",
    )

    handle_deployment_command_result(
        command,
        "succeeded",
        {
            "cluster_name": "edge-lab",
            "namespace": "metal3",
            "cluster_uid": "source-cluster-uid",
            "source_cluster_uid": "source-cluster-uid",
            "cluster_identity_preserved": True,
            "pivot_adoption_verified": True,
            "machine_inventory_ready": True,
            "controllers_ready": True,
            "machine_count": 3,
            "ready_machine_count": 1,
            "not_ready_machine_names": ["edge-lab-worker-1", "edge-lab-worker-2"],
            "side_effects": False,
        },
        None,
    )

    deployment.refresh_from_db()
    assert deployment.plan_result["dual_agent"]["target_agent_owns_cluster"] is False
    assert deployment.plan_result["dual_agent"]["eph_reclaim_allowed"] is False
    assert deployment.error["code"] == "TARGET_AGENT_CAPI_ADOPTION_VERIFICATION_FAILED"




@pytest.mark.django_db
def test_control_plane_rejects_target_adoption_without_source_cluster_uid(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="target-adoption-missing-source-uid",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="partial-readiness-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(b"partial-readiness-token").hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    deployment.target_node = target_node
    deployment.spec = {**deployment.spec, "capi_namespace": "metal3"}
    deployment.plan_result = {
        "capi_pivot_source_preflight": {
            "source_cluster_identity": {}
        },
        "dual_agent": {
            "phase": "target_agent_verification_pending",
            "target_agent_owns_cluster": False,
            "eph_reclaim_allowed": False,
        },
    }
    deployment.save(update_fields=["target_node", "spec", "plan_result", "updated_at"])
    command = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target_node,
        requested_by=user,
        deployment=deployment,
        command_type="cluster.capi.pivot.target.verify",
        payload={},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key=f"partial-target-readiness:{deployment.id}",
    )

    handle_deployment_command_result(
        command,
        "succeeded",
        {
            "cluster_name": "edge-lab",
            "namespace": "metal3",
            "cluster_uid": None,
            "source_cluster_uid": None,
            "cluster_identity_preserved": True,
            "pivot_adoption_verified": True,
            "machine_inventory_ready": True,
            "controllers_ready": True,
            "machine_count": 3,
            "ready_machine_count": 3,
            "not_ready_machine_names": [],
            "side_effects": False,
        },
        None,
    )

    deployment.refresh_from_db()
    assert deployment.plan_result["dual_agent"]["target_agent_owns_cluster"] is False
    assert deployment.plan_result["dual_agent"]["eph_reclaim_allowed"] is False
    assert deployment.error["code"] == "TARGET_AGENT_CAPI_ADOPTION_VERIFICATION_FAILED"


def _worker_preflight_fixture(cluster_delivery_workspace, key):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    created = create_deployment(client, tenant, project, eph_node, key=key)
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name=f"{key}-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(f"{key}-token".encode()).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    network_profile = {
        "version": "v1",
        "control_plane": {
            "interface": "eno1",
            "cidr": "192.0.2.10/24",
            "gateway": "192.0.2.1",
            "mtu": 1500,
        },
        "data_plane": {
            "interfaces": ["ens2f0"],
            "mtu": 9000,
            "hugepages": "1Gi",
            "numa_policy": "single-numa-node",
            "cpu_manager_policy": "static",
            "cni": {
                "primary": "calico",
                "meta": "multus",
                "attachments": [
                    {
                        "name": "sriov-data",
                        "type": "sriov",
                        "resource_name": "intel.com/sriov_netdevice",
                        "dpdk": True,
                    }
                ],
            },
        },
        "pod_traffic_default": "data_plane",
    }
    config = {
        "machine_deployment_name": "edge-workers",
        "infrastructure_template_name": "edge-worker-template",
        "bootstrap_template_name": "edge-worker-bootstrap",
        "baremetal_host_namespace": "metal3-system",
        "host_selector": {"pool": "workers"},
        "network_profile": network_profile,
    }
    deployment.target_node = target_node
    deployment.spec = {**deployment.spec, "worker_provisioning": config}
    deployment.plan_result = {
        "capi_pivot_source_preflight": {
            "source_cluster_identity": {"uid": "source-cluster-uid"}
        }
    }
    deployment.status = ClusterDeployment.Status.PROVISIONING
    deployment.save(update_fields=["target_node", "spec", "plan_result", "status"])
    command = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target_node,
        requested_by=user,
        deployment=deployment,
        command_type="cluster.capi.worker.preflight",
        payload={
            "cluster_name": "edge-lab",
            "namespace": "default",
            "expected_worker_replicas": 4,
            "worker_provisioning": config,
        },
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key=f"{key}:{deployment.id}",
    )
    profile_hash = hashlib.sha256(
        json.dumps(
            network_profile,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    result = {
        "ready": True,
        "cluster_name": "edge-lab",
        "cluster_uid": "source-cluster-uid",
        "namespace": "default",
        "expected_worker_replicas": 4,
        "machine_deployment_name": "edge-workers",
        "machine_deployment_exists": False,
        "infrastructure_template": {
            "name": "edge-worker-template",
            "uid": "infra-template-uid",
            "api_version": "infrastructure.cluster.x-k8s.io/v1beta1",
        },
        "bootstrap_template": {
            "name": "edge-worker-bootstrap",
            "uid": "bootstrap-template-uid",
            "api_version": "bootstrap.cluster.x-k8s.io/v1beta1",
        },
        "baremetal_host_namespace": "metal3-system",
        "host_selector": {"pool": "workers"},
        "template_host_selector": {"pool": "workers"},
        "template_host_selector_verified": True,
        "available_baremetal_host_count": 4,
        "selected_baremetal_hosts": [
            {"name": f"bmh-{index}", "uid": f"bmh-uid-{index}", "ready": True}
            for index in range(1, 5)
        ],
        "network_profile": {
            "valid": True,
            "profile_hash": profile_hash,
            "pod_traffic_default": "data_plane",
            "primary_cni": "calico",
            "meta_cni": "multus",
            "dpdk_requested": True,
            "attachment_count": 1,
        },
        "network_template_profile_hash": profile_hash,
        "network_template_profile_verified": True,
        "provider_versions": {
            "cluster.x-k8s.io": "v1beta1",
            "bootstrap.cluster.x-k8s.io": "v1beta1",
            "infrastructure.cluster.x-k8s.io": "v1beta1",
            "metal3.io": "v1alpha1",
        },
        "controllers": [
            {"namespace": "capi-system", "deployment": "capi-controller-manager", "ready": True},
            {
                "namespace": "capi-kubeadm-bootstrap-system",
                "deployment": "capi-kubeadm-bootstrap-controller-manager",
                "ready": True,
            },
            {
                "namespace": "capi-kubeadm-control-plane-system",
                "deployment": "capi-kubeadm-control-plane-controller-manager",
                "ready": True,
            },
            {"namespace": "capm3-system", "deployment": "capm3-controller-manager", "ready": True},
        ],
        "controllers_ready": True,
        "blockers": [],
        "side_effects": False,
        "worker_creation_performed": False,
        "execution_allowed": False,
    }
    return deployment, command, result


@pytest.mark.django_db
def test_control_plane_rejects_worker_preflight_without_source_cluster_uid(
    cluster_delivery_workspace,
):
    deployment, command, result = _worker_preflight_fixture(
        cluster_delivery_workspace,
        "worker-preflight-missing-source-uid",
    )
    deployment.plan_result = {
        **deployment.plan_result,
        "capi_pivot_source_preflight": {
            "source_cluster_identity": {"uid": None}
        },
        "dual_agent": {"target_agent_owns_cluster": True},
    }
    deployment.save(update_fields=["plan_result"])
    result["cluster_uid"] = None

    handle_deployment_command_result(command, "succeeded", result, None)

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "TARGET_WORKER_PROVISIONING_PREFLIGHT_FAILED"
    assert deployment.plan_result.get("dual_agent", {}).get("target_agent_owns_cluster") is True
    assert not AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.capi.worker.provision",
    ).exists()

@pytest.mark.django_db
def test_control_plane_accepts_worker_preflight_but_keeps_provisioning_approval_gate(
    cluster_delivery_workspace,
):
    deployment, command, result = _worker_preflight_fixture(
        cluster_delivery_workspace,
        "worker-preflight-approved-evidence",
    )

    handle_deployment_command_result(command, "succeeded", result, None)

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.WAITING_APPROVAL
    assert deployment.error is None
    assert deployment.plan_result["worker_provisioning_preflight"]["ready"] is True
    assert (
        deployment.plan_result["dual_agent"]["phase"]
        == "worker_preflight_passed_waiting_provisioning_approval"
    )
    assert deployment.plan_result["dual_agent"]["eph_reclaim_allowed"] is False
    checkpoint = ClusterDeploymentCheckpoint.objects.get(deployment=deployment)
    assert checkpoint.agent_node_id == deployment.target_node_id
    assert checkpoint.generation == 1
    assert checkpoint.sequence == 1
    assert checkpoint.phase == "worker_provisioning"
    assert checkpoint.data["pending_step"] == "worker_provisioning"
    assert "worker_provisioning_preflight" in checkpoint.data["completed_steps"]


@pytest.mark.django_db
def test_control_plane_rejects_worker_preflight_with_duplicate_host_identity(
    cluster_delivery_workspace,
):
    deployment, command, result = _worker_preflight_fixture(
        cluster_delivery_workspace,
        "worker-preflight-duplicate-host",
    )
    result["selected_baremetal_hosts"][1]["uid"] = (
        result["selected_baremetal_hosts"][0]["uid"]
    )

    handle_deployment_command_result(command, "succeeded", result, None)

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "TARGET_WORKER_PROVISIONING_PREFLIGHT_FAILED"

@pytest.mark.django_db
def test_control_plane_rejects_worker_preflight_without_healthy_provider_controllers(
    cluster_delivery_workspace,
):
    deployment, command, result = _worker_preflight_fixture(
        cluster_delivery_workspace,
        "worker-preflight-controller-unhealthy",
    )
    result["controllers"][2]["ready"] = False
    result["controllers_ready"] = False

    handle_deployment_command_result(command, "succeeded", result, None)

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "TARGET_WORKER_PROVISIONING_PREFLIGHT_FAILED"
    assert deployment.plan_result.get("dual_agent", {}).get("eph_reclaim_allowed") is not True

@pytest.mark.django_db
def test_approved_worker_provisioning_result_stays_simulation_only(
    cluster_delivery_workspace,
):
    deployment, preflight_command, preflight_result = _worker_preflight_fixture(
        cluster_delivery_workspace,
        "worker-provisioning-simulation",
    )
    handle_deployment_command_result(
        preflight_command,
        "succeeded",
        preflight_result,
        None,
    )
    deployment.refresh_from_db()
    provision_command = AgentCommand.objects.get(
        deployment=deployment,
        command_type="cluster.capi.worker.provision",
    )
    assert provision_command.status == AgentCommand.Status.WAITING_APPROVAL

    approver = get_user_model().objects.create_user(
        username="worker-provisioning-simulation-approver",
        password="test-password",
    )
    TenantMembership.objects.create(
        tenant=deployment.tenant,
        user=approver,
        role=TenantMembership.Role.ADMIN,
    )
    provision_command.status = AgentCommand.Status.SUCCEEDED
    provision_command.approved_by = approver
    provision_command.approved_at = timezone.now()
    provision_command.approved_plan_hash = provision_command.plan_hash
    provision_command.save(
        update_fields=[
            "status",
            "approved_by",
            "approved_at",
            "approved_plan_hash",
        ]
    )
    result = {
        "deployment_id": str(deployment.id),
        "cluster_name": deployment.cluster_name,
        "namespace": "default",
        "machine_deployment_name": "edge-workers",
        "machine_deployment_uid": None,
        "cluster_uid": provision_command.payload["expected_cluster_uid"],
        "provisioning_plan_hash": provision_command.payload[
            "provisioning_plan_hash"
        ],
        "machine_deployment_manifest_hash": "a" * 64,
        "execution_mode": "server_side_dry_run_simulation",
        "simulation_status": "ready_for_manual_execution",
        "network_profile_hash": preflight_result["network_profile"]["profile_hash"],
        "created": False,
        "idempotent_reconciliation": False,
        "dry_run_passed": True,
        "side_effects": False,
        "execution_allowed": False,
        "worker_creation_performed": False,
        "worker_readiness_verified": False,
        "worker_machine_count": 0,
        "ready_worker_machine_count": 0,
        "not_ready_worker_machine_names": [],
        "worker_node_names": [],
    }

    handle_deployment_command_result(provision_command, "succeeded", result, None)

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.PLAN_READY
    assert deployment.error is None
    assert (
        deployment.plan_result["dual_agent"]["phase"]
        == "worker_provisioning_simulated_manual_execution_required"
    )
    assert deployment.plan_result["dual_agent"]["worker_provisioning_completed"] is False
    assert deployment.plan_result["dual_agent"]["worker_provisioning_simulated"] is True
    assert deployment.plan_result["dual_agent"]["eph_reclaim_allowed"] is False
    assert deployment.plan_result["worker_provisioning_simulation"] == result
    checkpoint = ClusterDeploymentCheckpoint.objects.get(deployment=deployment)
    assert checkpoint.sequence == 1
    assert checkpoint.phase == "worker_provisioning"
    assert checkpoint.data["pending_step"] == "worker_provisioning"
    assert "worker_provisioning" not in checkpoint.data["completed_steps"]
    assert not AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.postflight",
    ).exists()

    handle_deployment_command_result(provision_command, "succeeded", result, None)

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.PLAN_READY
    assert not AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.postflight",
    ).exists()


@pytest.mark.django_db
def test_worker_provisioning_rejects_payload_mutation_after_approval(
    cluster_delivery_workspace,
):
    deployment, preflight_command, preflight_result = _worker_preflight_fixture(
        cluster_delivery_workspace,
        "worker-provisioning-payload-mutated-after-approval",
    )
    handle_deployment_command_result(
        preflight_command, "succeeded", preflight_result, None
    )
    provision_command = AgentCommand.objects.get(
        deployment=deployment,
        command_type="cluster.capi.worker.provision",
    )
    approver = get_user_model().objects.create_user(
        username="worker-payload-tamper-approver",
        password="test-password",
    )
    TenantMembership.objects.create(
        tenant=deployment.tenant,
        user=approver,
        role=TenantMembership.Role.ADMIN,
    )
    provision_command.status = AgentCommand.Status.SUCCEEDED
    provision_command.approved_by = approver
    provision_command.approved_at = timezone.now()
    provision_command.approved_plan_hash = provision_command.plan_hash
    provision_command.payload = {
        **provision_command.payload,
        "expected_cluster_uid": "tampered-cluster-uid",
    }
    provision_command.save(
        update_fields=[
            "status",
            "approved_by",
            "approved_at",
            "approved_plan_hash",
            "payload",
        ]
    )

    handle_deployment_command_result(provision_command, "succeeded", {}, None)

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "TARGET_WORKER_PROVISIONING_SIMULATION_FAILED"
    assert deployment.plan_result.get("dual_agent", {}).get("eph_reclaim_allowed") is False


@pytest.mark.django_db
def test_worker_provisioning_rejects_non_object_payload_without_crashing(
    cluster_delivery_workspace,
):
    deployment, preflight_command, preflight_result = _worker_preflight_fixture(
        cluster_delivery_workspace,
        "worker-provisioning-malformed-payload",
    )
    handle_deployment_command_result(
        preflight_command, "succeeded", preflight_result, None
    )
    provision_command = AgentCommand.objects.get(
        deployment=deployment,
        command_type="cluster.capi.worker.provision",
    )
    approver = get_user_model().objects.create_user(
        username="worker-malformed-payload-approver",
        password="test-password",
    )
    TenantMembership.objects.create(
        tenant=deployment.tenant,
        user=approver,
        role=TenantMembership.Role.ADMIN,
    )
    provision_command.status = AgentCommand.Status.SUCCEEDED
    provision_command.approved_by = approver
    provision_command.approved_at = timezone.now()
    provision_command.approved_plan_hash = provision_command.plan_hash
    provision_command.payload = ["malformed"]
    provision_command.save(
        update_fields=[
            "status",
            "approved_by",
            "approved_at",
            "approved_plan_hash",
            "payload",
        ]
    )

    handle_deployment_command_result(provision_command, "succeeded", {}, None)

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "TARGET_WORKER_PROVISIONING_SIMULATION_FAILED"
    assert deployment.plan_result.get("dual_agent", {}).get("eph_reclaim_allowed") is False


@pytest.mark.django_db
def test_worker_provisioning_rejects_non_object_result_without_crashing(
    cluster_delivery_workspace,
):
    deployment, preflight_command, preflight_result = _worker_preflight_fixture(
        cluster_delivery_workspace,
        "worker-provisioning-malformed-result",
    )
    handle_deployment_command_result(
        preflight_command,
        "succeeded",
        preflight_result,
        None,
    )
    provision_command = AgentCommand.objects.get(
        deployment=deployment,
        command_type="cluster.capi.worker.provision",
    )
    approver = get_user_model().objects.create_user(
        username="worker-malformed-result-approver",
        password="test-password",
    )
    TenantMembership.objects.create(
        tenant=deployment.tenant,
        user=approver,
        role=TenantMembership.Role.ADMIN,
    )
    provision_command.status = AgentCommand.Status.SUCCEEDED
    provision_command.approved_by = approver
    provision_command.approved_at = timezone.now()
    provision_command.approved_plan_hash = provision_command.plan_hash
    provision_command.save(
        update_fields=[
            "status",
            "approved_by",
            "approved_at",
            "approved_plan_hash",
        ]
    )

    handle_deployment_command_result(
        provision_command,
        "succeeded",
        ["malformed", "agent", "result"],
        None,
    )

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "TARGET_WORKER_PROVISIONING_SIMULATION_FAILED"
    assert deployment.plan_result["dual_agent"]["eph_reclaim_allowed"] is False


@pytest.mark.django_db
def test_worker_provisioning_result_without_independent_approval_is_rejected(
    cluster_delivery_workspace,
):
    deployment, preflight_command, preflight_result = _worker_preflight_fixture(
        cluster_delivery_workspace,
        "worker-provisioning-no-approval",
    )
    handle_deployment_command_result(
        preflight_command,
        "succeeded",
        preflight_result,
        None,
    )
    provision_command = AgentCommand.objects.get(
        deployment=deployment,
        command_type="cluster.capi.worker.provision",
    )
    provision_command.status = AgentCommand.Status.SUCCEEDED
    provision_command.save(update_fields=["status"])
    result = {
        "deployment_id": str(deployment.id),
        "cluster_name": deployment.cluster_name,
        "namespace": "default",
        "machine_deployment_name": "edge-workers",
        "machine_deployment_uid": "machine-deployment-uid",
        "cluster_uid": provision_command.payload["expected_cluster_uid"],
        "provisioning_plan_hash": provision_command.payload[
            "provisioning_plan_hash"
        ],
        "network_profile_hash": preflight_result["network_profile"]["profile_hash"],
        "created": True,
        "dry_run_passed": True,
        "side_effects": True,
        "execution_allowed": True,
        "worker_creation_performed": True,
        "worker_readiness_verified": True,
        "worker_machine_count": 4,
        "ready_worker_machine_count": 4,
        "not_ready_worker_machine_names": [],
    }

    handle_deployment_command_result(provision_command, "succeeded", result, None)

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "TARGET_WORKER_PROVISIONING_SIMULATION_FAILED"



@pytest.mark.django_db
def test_pivot_postflight_rejects_missing_direct_ironic_verification(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="pivot-postflight-ironic-required",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    target_token = "pivot-postflight-target-token"
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="pivot-postflight-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(target_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    deployment.target_node = target_node
    deployment.status = ClusterDeployment.Status.PROVISIONING
    deployment.plan_result = {
        "capi_pivot_source_preflight": {
            "source_cluster_identity": {"uid": "source-cluster-uid"}
        }
    }
    deployment.save(update_fields=["target_node", "status", "plan_result"])
    command = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=target_node,
        requested_by=user,
        deployment=deployment,
        command_type="cluster.capi.pivot.postflight",
        payload={"cluster_name": "edge-lab", "namespace": "default"},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="pivot-postflight-ironic-required-command",
    )

    handle_deployment_command_result(
        command,
        "succeeded",
        {
            "postflight_passed": True,
            "cluster_name": "edge-lab",
            "namespace": "default",
            "cluster_uid": "source-cluster-uid",
            "side_effects": False,
            "complete_hardware_verification": False,
            "verification_scope": "capi_metal3_without_direct_ironic_api",
            "ironic_verification": "not_configured",
            "metal3_observation": {
                "ready": True,
                "machine_count": 1,
                "resolved_machine_count": 1,
                "ironic": {
                    "status": "not_configured",
                    "applicable_node_count": 0,
                    "verified_node_count": 0,
                },
            },
        },
        None,
    )

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.FAILED
    assert deployment.error["code"] == "CAPI_PIVOT_METAL3_IRONIC_POSTFLIGHT_FAILED"
    assert deployment.plan_result["dual_agent"]["eph_reclaim_allowed"] is False
    assert not AgentCommand.objects.filter(
        deployment=deployment,
        command_type="cluster.capi.worker.preflight",
    ).exists()


@pytest.mark.django_db
def test_eph_agent_cannot_poll_deployment_commands_after_target_ownership(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, eph_token, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="eph-command-fencing-poll",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    command = AgentCommand.objects.filter(
        deployment=deployment,
        node=eph_node,
        status=AgentCommand.Status.QUEUED,
    ).order_by("created_at").first()
    assert command is not None
    deployment.plan_result = {
        **(deployment.plan_result or {}),
        "dual_agent": {"target_agent_owns_cluster": True},
    }
    deployment.save(update_fields=["plan_result"])

    response = APIClient().post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(eph_node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
    )

    assert response.status_code == 200
    assert response.data == {"command": None}
    command.refresh_from_db()
    assert command.status == AgentCommand.Status.FAILED
    assert command.error["code"] == "AGENT_CONTROL_OWNERSHIP_FENCED"
    assert command.lease_token is None


@pytest.mark.django_db
def test_eph_agent_cannot_submit_result_after_target_ownership_fence(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, eph_token, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="eph-command-fencing-result",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    lease_token = uuid.uuid4()
    command = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=eph_node,
        requested_by=user,
        deployment=deployment,
        command_type="cluster.capi.pivot.source.preflight",
        payload={},
        status=AgentCommand.Status.RUNNING,
        lease_token=lease_token,
        lease_expires_at=timezone.now() + datetime.timedelta(seconds=60),
        idempotency_key=f"eph-fenced-result:{deployment.id}",
    )
    deployment.plan_result = {
        **(deployment.plan_result or {}),
        "dual_agent": {"target_agent_owns_cluster": True},
    }
    deployment.save(update_fields=["plan_result"])

    response = APIClient().post(
        f"/api/v1/agent-runtime/commands/{command.id}/result/",
        {
            "node_id": str(eph_node.id),
            "outcome": "succeeded",
            "result": {"source_management_plane_ready": True},
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
        HTTP_X_AGENT_LEASE=str(lease_token),
    )

    assert response.status_code == 409
    assert response.data["code"] == "AGENT_CONTROL_OWNERSHIP_FENCED"
    command.refresh_from_db()
    assert command.status == AgentCommand.Status.FAILED
    assert command.lease_token is None
    assert command.result is None


@pytest.mark.django_db
def test_eph_agent_cannot_renew_command_lease_after_target_ownership_fence(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, eph_token, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="eph-command-fencing-renew",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    lease_token = uuid.uuid4()
    command = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=eph_node,
        requested_by=user,
        deployment=deployment,
        command_type="cluster.capi.pivot.source.preflight",
        payload={},
        status=AgentCommand.Status.RUNNING,
        lease_token=lease_token,
        lease_expires_at=timezone.now() + datetime.timedelta(seconds=60),
        idempotency_key=f"eph-fenced-renew:{deployment.id}",
    )
    deployment.plan_result = {
        **(deployment.plan_result or {}),
        "dual_agent": {"target_agent_owns_cluster": True},
    }
    deployment.save(update_fields=["plan_result"])

    response = APIClient().post(
        f"/api/v1/agent-runtime/commands/{command.id}/lease/renew/",
        {"node_id": str(eph_node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
        HTTP_X_AGENT_LEASE=str(lease_token),
    )

    assert response.status_code == 409
    assert response.data["code"] == "AGENT_CONTROL_OWNERSHIP_FENCED"
    command.refresh_from_db()
    assert command.status == AgentCommand.Status.FAILED
    assert command.lease_token is None
    assert command.lease_expires_at is None

@pytest.mark.django_db
@pytest.mark.parametrize("outcome", ["failed", "succeeded"])
def test_terminal_deployment_ignores_late_command_callbacks(
    cluster_delivery_workspace,
    outcome,
):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="terminal-late-callback-" + outcome,
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    finished_at = timezone.now()
    deployment.status = ClusterDeployment.Status.SUCCEEDED
    deployment.error = None
    deployment.finished_at = finished_at
    deployment.save(update_fields=["status", "error", "finished_at"])
    command = AgentCommand.objects.create(
        tenant=tenant,
        project=project,
        node=eph_node,
        requested_by=user,
        deployment=deployment,
        command_type="cluster.postflight",
        payload={},
        status=AgentCommand.Status.SUCCEEDED,
        idempotency_key="terminal-late-callback-command-" + outcome,
    )

    handle_deployment_command_result(
        command,
        outcome,
        {"ready": False},
        {"code": "LATE_CALLBACK"} if outcome == "failed" else None,
    )

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.SUCCEEDED
    assert deployment.error is None
    assert deployment.finished_at == finished_at
    assert (deployment.plan_result or {}).get("postflight") is None



@pytest.mark.django_db
def test_checkpoint_restore_fails_closed_after_target_agent_reassignment(
    cluster_delivery_workspace,
):
    user, tenant, project, eph_node, _, client = cluster_delivery_workspace
    old_token = "target-checkpoint-old-agent-token"
    old_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="checkpoint-old-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(old_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(
        client,
        tenant,
        project,
        eph_node,
        key="checkpoint-agent-reassignment",
    )
    deployment = ClusterDeployment.objects.get(pk=created.data["id"])
    deployment.status = ClusterDeployment.Status.CONTROL_PLANE_READY
    deployment.save(update_fields=["status"])
    handoff = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-agent/handoff/",
        {"target_node_id": str(old_node.id)},
        format="json",
    )
    assert handoff.status_code == 202

    old_agent_client = APIClient()
    checkpoint_url = (
        f"/api/v1/agent-runtime/deployments/{deployment.id}/checkpoint/"
    )
    old_headers = {"HTTP_AUTHORIZATION": f"Bearer {old_token}"}
    checkpoint = old_agent_client.post(
        checkpoint_url,
        {
            "node_id": str(old_node.id),
            "generation": 1,
            "sequence": 1,
            "phase": "target_agent_bootstrap",
            "data": {
                "cluster_name": "edge-lab",
                "completed_steps": [],
                "pending_step": "target_agent_bootstrap",
            },
        },
        format="json",
        **old_headers,
    )
    assert checkpoint.status_code == 201

    replacement_token = "target-checkpoint-replacement-agent-token"
    replacement_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="checkpoint-replacement-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(replacement_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    deployment.target_node = replacement_node
    deployment.save(update_fields=["target_node", "updated_at"])

    replacement_client = APIClient()
    restored = replacement_client.get(
        checkpoint_url,
        {"node_id": str(replacement_node.id)},
        HTTP_AUTHORIZATION=f"Bearer {replacement_token}",
    )

    assert restored.status_code == 409
    assert restored.data["code"] == "CHECKPOINT_AGENT_MISMATCH_MANUAL_RECOVERY_REQUIRED"
    assert "data" not in restored.data

    attempted_write = replacement_client.post(
        checkpoint_url,
        {
            "node_id": str(replacement_node.id),
            "generation": 1,
            "sequence": 2,
            "phase": "target_agent_bootstrap",
            "data": {
                "cluster_name": "edge-lab",
                "completed_steps": ["target_agent_bootstrap"],
                "pending_step": None,
            },
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {replacement_token}",
    )
    assert attempted_write.status_code == 409

    persisted = ClusterDeploymentCheckpoint.objects.get(deployment=deployment)
    assert persisted.agent_node_id == old_node.id
    assert persisted.sequence == 1
    assert persisted.phase == "target_agent_bootstrap"
    assert persisted.data["completed_steps"] == []
    assert persisted.data["pending_step"] == "target_agent_bootstrap"


@pytest.mark.django_db
def test_worker_provisioning_command_failure_requires_reconciliation_without_retry(
    cluster_delivery_workspace,
):
    deployment, preflight_command, preflight_result = _worker_preflight_fixture(
        cluster_delivery_workspace,
        "worker-provisioning-ambiguous-outcome",
    )
    handle_deployment_command_result(
        preflight_command,
        "succeeded",
        preflight_result,
        None,
    )
    provision_command = AgentCommand.objects.get(
        deployment=deployment,
        command_type="cluster.capi.worker.provision",
    )

    handle_deployment_command_result(
        provision_command,
        "failed",
        {},
        {"code": "AGENT_CONNECTION_LOST"},
    )

    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.RECONCILIATION_REQUIRED
    assert deployment.error["code"] == "WORKER_PROVISIONING_OUTCOME_UNKNOWN"
    assert deployment.error["retry_allowed"] is False
    reconciliation = deployment.plan_result["worker_provisioning_reconciliation"]
    assert reconciliation["state"] == "outcome_unknown"
    assert reconciliation["command_id"] == str(provision_command.id)
    assert reconciliation["side_effects"] == "unknown"
    assert reconciliation["retry_allowed"] is False
    assert reconciliation["eph_reclaim_allowed"] is False
    assert deployment.plan_result["dual_agent"]["eph_reclaim_allowed"] is False

    handle_deployment_command_result(
        provision_command,
        "succeeded",
        {"created": False, "side_effects": False},
        None,
    )
    deployment.refresh_from_db()
    assert deployment.status == ClusterDeployment.Status.RECONCILIATION_REQUIRED
    reconciliation = deployment.plan_result["worker_provisioning_reconciliation"]
    assert reconciliation["state"] == "outcome_unknown"
