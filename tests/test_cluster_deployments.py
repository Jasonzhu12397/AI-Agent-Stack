# ruff: noqa: I001
import datetime
import hashlib
import json

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
        "phase": "capi_pivot_preflight",
        "data": {
            "cluster_name": "edge-lab",
            "completed_steps": ["target_agent_bootstrap"],
            "pending_step": "capi_pivot_preflight",
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
    checkpoint_payload["phase"] = "capi_pivot_verified"
    checkpoint_payload["data"]["completed_steps"].append("capi_pivot_preflight")
    advanced = agent_client.post(url, checkpoint_payload, format="json", **headers)
    assert advanced.status_code == 201
    checkpoint = ClusterDeploymentCheckpoint.objects.get(deployment=deployment)
    assert checkpoint.sequence == 2
    assert checkpoint.phase == "capi_pivot_verified"


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

    replay = dict(payload)
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
    assert checkpoint.sequence == 1
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
    deployment.status = ClusterDeployment.Status.CONTROL_PLANE_READY
    deployment.save(update_fields=["status"])

    handoff = client.post(
        f"/api/v1/cluster-deployments/{deployment.id}/target-agent/handoff/",
        {"target_node_id": str(target_node.id)},
        format="json",
    )

    assert handoff.status_code == 202
    command = AgentCommand.objects.get(
        deployment=deployment,
        command_type="cluster.capi.pivot.target.preflight",
    )
    assert command.node_id == target_node.id
    assert command.payload == {
        "deployment_id": str(deployment.id),
        "cluster_name": "edge-lab",
        "namespace": "default",
    }
    assert handoff.data["preflight_command_id"] == str(command.id)
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
                "cluster_name": "edge-lab",
            }
        ],
        "inventory_truncated": False,
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
        "namespace": "default",
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
    assert deployment.error is None
