# ruff: noqa: I001
import datetime
import hashlib

import pytest
from core.models import (
    AgentCommand,
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
                "plan_hash": "a" * 64,
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
    assert deployment.plan_hash == "a" * 64
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
def test_target_agent_runs_postflight_and_completes_deployment(cluster_delivery_workspace):
    user, tenant, project, eph_node, eph_token, client = cluster_delivery_workspace
    target_token = "target-agent-test-token"
    target_node = AgentNode.objects.create(
        tenant=tenant,
        project=project,
        created_by=user,
        name="delivery-target",
        node_type=AgentNode.NodeType.TARGET_CLUSTER,
        cluster_name="edge-lab",
        credential_hash=hashlib.sha256(target_token.encode("utf-8")).hexdigest(),
        status=AgentNode.Status.ONLINE,
        last_seen_at=timezone.now(),
    )
    created = create_deployment(client, tenant, project, eph_node, key="target-postflight")
    deployment_id = created.data["id"]
    eph_client = APIClient()

    preflight = eph_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(eph_node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
    )
    preflight_command = preflight.data["command"]
    eph_client.post(
        f"/api/v1/agent-runtime/commands/{preflight_command['id']}/result/",
        {
            "node_id": str(eph_node.id),
            "outcome": "succeeded",
            "result": {"cpu_count": 32},
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
        HTTP_X_AGENT_LEASE=preflight_command["lease_token"],
    )
    plan = eph_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(eph_node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
    )
    plan_command = plan.data["command"]
    eph_client.post(
        f"/api/v1/agent-runtime/commands/{plan_command['id']}/result/",
        {
            "node_id": str(eph_node.id),
            "outcome": "succeeded",
            "result": {
                "plan_status": "validated_dry_run",
                "plan_hash": "b" * 64,
                "provisioning_ready": True,
                "missing_requirements": [],
                "adapter_enabled": False,
                "side_effects": False,
            },
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {eph_token}",
        HTTP_X_AGENT_LEASE=plan_command["lease_token"],
    )

    postflight = client.post(
        f"/api/v1/cluster-deployments/{deployment_id}/target-postflight/",
        {
            "target_node_id": str(target_node.id),
            "confirmation": "cluster_provisioned",
        },
        format="json",
    )
    assert postflight.status_code == 202
    command = AgentCommand.objects.get(pk=postflight.data["command_id"])
    assert command.node_id == target_node.id
    assert command.command_type == "cluster.postflight"
    assert command.payload["cluster_name"] == "edge-lab"
    assert "cluster_spec" not in command.payload

    target_client = APIClient()
    polled = target_client.post(
        "/api/v1/agent-runtime/commands/poll/",
        {"node_id": str(target_node.id)},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {target_token}",
    )
    target_command = polled.data["command"]
    result = target_client.post(
        f"/api/v1/agent-runtime/commands/{target_command['id']}/result/",
        {
            "node_id": str(target_node.id),
            "outcome": "succeeded",
            "result": {
                "node_count": 7,
                "ready": True,
                "not_ready_nodes": [],
            },
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {target_token}",
        HTTP_X_AGENT_LEASE=target_command["lease_token"],
    )

    deployment = ClusterDeployment.objects.get(pk=deployment_id)
    assert result.status_code == 200
    assert deployment.status == ClusterDeployment.Status.SUCCEEDED
    assert deployment.plan_result["postflight"]["ready"] is True



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
    created = create_deployment(client, tenant, project, eph_node, key="target-postflight-incomplete")
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
