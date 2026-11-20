# ruff: noqa: I001
import datetime
import re
import uuid

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from packages.agent_contracts.infra_networks import (
    InfraNetworkError,
    derive_network_spec,
    network_by_role,
    plan_infra_networks,
    render_ip_pools,
    render_role_network_data,
    render_sriov_node_policies,
)
from packages.agent_contracts.storage_addon import (
    StorageAddonError,
    plan_storage_addon as plan_storage_addon_contract,
)
from apps.agent_contracts.sriov_device_plugin import (
    SriovDevicePluginConfigError,
    validate_sriov_device_plugin_config,
)
from core.audit import record_audit_event
from core.agent_node_views import _tenant_and_project, _validate_payload
from core.cloud_control_bridge import (
    CloudControlBridgeError,
    CloudControlNotConfigured,
    FleetInventoryEmpty,
    FleetInventoryInsufficient,
    build_cloud_control_spec,
    control_identity,
    dispatch_deployment,
)
from core.models import (
    AgentNode,
    ClusterDeployment,
    InfrastructureNetworkProfile,
    ProjectMembership,
    TenantMembership,
)


def _deployment_data(deployment):
    plan_result = deployment.plan_result if isinstance(deployment.plan_result, dict) else {}
    return {
        "id": str(deployment.id),
        "tenant_id": str(deployment.tenant_id),
        "project_id": str(deployment.project_id),
        "cluster_name": deployment.cluster_name,
        "kubernetes_version": deployment.kubernetes_version,
        "eph_node_id": str(deployment.eph_node_id),
        "target_node_id": (
            str(deployment.target_node_id) if deployment.target_node_id else None
        ),
        "status": deployment.status,
        "spec": deployment.spec,
        "plan_result": deployment.plan_result,
        "cloud_control": plan_result.get("cloud_control"),
        "error": deployment.error,
        "created_at": deployment.created_at,
        "updated_at": deployment.updated_at,
        "started_at": deployment.started_at,
        "finished_at": deployment.finished_at,
    }


def _can_edit_project(user, tenant, project):
    tenant_admin = TenantMembership.objects.filter(
        tenant=tenant,
        user=user,
        role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
    ).exists()
    project_editor = ProjectMembership.objects.filter(
        project=project,
        user=user,
        role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
    ).exists()
    return tenant_admin or project_editor


def _validated_spec(request, *, infra_profile=None):
    spec = request.data.get("cluster_spec")
    if not isinstance(spec, dict):
        return None, "cluster_spec must be a JSON object."
    if spec.get("infra_network_profile") is None and infra_profile is not None:
        spec = {**spec, "infra_network_profile": infra_profile}
    payload_error = _validate_payload({"cluster_spec": spec})
    if payload_error:
        return None, payload_error
    cluster_name = spec.get("cluster_name")
    kubernetes_version = spec.get("kubernetes_version")
    control_plane_replicas = spec.get("control_plane_replicas")
    worker_replicas = spec.get("worker_replicas")
    if (
        not isinstance(cluster_name, str)
        or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", cluster_name)
        or len(cluster_name) > 63
    ):
        return None, "cluster_name must be a DNS label."
    if (
        not isinstance(kubernetes_version, str)
        or not re.fullmatch(r"v1\.\d+\.\d+", kubernetes_version)
    ):
        return None, "kubernetes_version must use v1.x.y format."
    if (
        not isinstance(control_plane_replicas, int)
        or isinstance(control_plane_replicas, bool)
        or control_plane_replicas not in (1, 3, 5, 7)
    ):
        return None, "control_plane_replicas must be one of 1, 3, 5, 7."
    if (
        not isinstance(worker_replicas, int)
        or isinstance(worker_replicas, bool)
        or worker_replicas < 0
        or worker_replicas > 1000
    ):
        return None, "worker_replicas must be an integer from 0 to 1000."
    worker_config = spec.get("worker_provisioning")
    if worker_config is not None:
        if not isinstance(worker_config, dict):
            return None, "worker_provisioning must be a JSON object."
        pools = worker_config.get("sriov_device_plugin")
        if pools is not None:
            try:
                normalized = validate_sriov_device_plugin_config(pools)
            except SriovDevicePluginConfigError as exc:
                return None, str(exc)
            spec["worker_provisioning"] = {
                **worker_config,
                "sriov_device_plugin": normalized,
            }
    infra_profile = spec.get("infra_network_profile")
    if infra_profile is not None:
        try:
            plan = plan_infra_networks(infra_profile)
            spec["infra_network_profile"] = {
                "networks": plan["networks"],
                "dns_servers": plan["dns_servers"],
            }
            spec["network_spec"] = derive_network_spec(plan)
            meta_data = {
                "objectNames": [
                    {"key": "machine_name", "object": "machine"},
                    {"key": "bmh_name", "object": "baremetalhost"},
                ]
            }
            role_data = {
                role: {
                    "meta_data": meta_data,
                    "network_data": render_role_network_data(plan, role),
                }
                for role in ("control_plane", "worker", "storage")
            }
            spec["data_template"] = role_data["control_plane"]
            spec["worker_data_template"] = role_data["worker"]
            spec["storage_data_template"] = role_data["storage"]
            spec["ip_pool_manifests"] = render_ip_pools(
                plan,
                cluster_name=cluster_name,
                namespace=spec.get("namespace", "metal3"),
            )
            spec["sriov_node_policy_manifests"] = render_sriov_node_policies(plan)
            storage_addon = spec.get("storage_addon")
            if isinstance(storage_addon, dict) and storage_addon.get("enabled") is True:
                storage_network = storage_addon.get("network")
                if not isinstance(storage_network, dict):
                    internal = network_by_role(plan, "storage-internal")
                    external = network_by_role(plan, "storage-external")
                    if internal is not None and external is not None:
                        storage_addon = {
                            **storage_addon,
                            "network": {
                                "internal": {
                                    "name": internal["name"],
                                    "interface": (
                                        internal["bond"]["name"]
                                        if internal["bond"]
                                        else internal["interface"]
                                    ),
                                    "cidr": internal["cidr"],
                                    "mtu": internal["mtu"],
                                    "vlan": internal["vlan"],
                                    "ipam": "whereabouts",
                                },
                                "external": {
                                    "name": external["name"],
                                    "interface": (
                                        external["bond"]["name"]
                                        if external["bond"]
                                        else external["interface"]
                                    ),
                                    "cidr": external["cidr"],
                                    "mtu": external["mtu"],
                                    "vlan": external["vlan"],
                                    "ipam": "whereabouts",
                                },
                            },
                        }
                        spec["storage_addon"] = storage_addon
        except InfraNetworkError as exc:
            return None, f"infra_network_profile: {exc}"
    storage_addon = spec.get("storage_addon")
    if storage_addon is not None:
        worker_contract = spec.get("worker_network_contract")
        if not isinstance(worker_contract, dict) and isinstance(
            spec.get("worker_provisioning"), dict
        ):
            worker_contract = spec["worker_provisioning"].get(
                "worker_network_contract"
            ) or spec["worker_provisioning"].get("network_contract")
        try:
            spec["storage_addon"] = plan_storage_addon_contract(
                storage_addon,
                worker_network_contract=worker_contract,
            )
        except StorageAddonError as exc:
            return None, f"storage_addon: {exc}"
    return spec, None


class ClusterDeploymentListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        deployments = ClusterDeployment.objects.filter(
            tenant=tenant,
            project=project,
        )[:50]
        return Response(
            {
                "count": len(deployments),
                "results": [_deployment_data(item) for item in deployments],
            }
        )

    def post(self, request, tenant_id, project_id):
        if not isinstance(request.data, dict):
            return Response({"detail": "Request body must be a JSON object."}, status=400)
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        if not _can_edit_project(request.user, tenant, project):
            return Response({"detail": "Project editor permission required."}, status=403)
        try:
            eph_node_id = uuid.UUID(str(request.data.get("eph_node_id")))
        except (TypeError, ValueError, AttributeError):
            return Response({"detail": "A valid eph_node_id is required."}, status=400)
        eph_node = get_object_or_404(
            AgentNode.objects.filter(
                tenant=tenant,
                project=project,
                node_type=AgentNode.NodeType.EPH_NODE,
            ),
            pk=eph_node_id,
        )
        if eph_node.status != AgentNode.Status.ONLINE or (
            eph_node.last_seen_at is None
            or eph_node.last_seen_at < timezone.now() - datetime.timedelta(seconds=60)
        ):
            return Response(
                {"detail": "The selected EPH agent must have a fresh heartbeat."},
                status=409,
            )
        raw_spec = request.data.get("cluster_spec")
        infra_profile = None
        if isinstance(raw_spec, dict) and raw_spec.get("infra_network_profile") is None:
            profile_record = InfrastructureNetworkProfile.objects.filter(
                tenant=tenant, project=project
            ).first()
            if profile_record is not None:
                # The saved project profile is the single source of truth;
                # callers do not have to paste the same JSON again.
                infra_profile = profile_record.profile
        spec, payload_error = _validated_spec(request, infra_profile=infra_profile)
        if payload_error:
            return Response({"detail": payload_error}, status=400)
        cluster_name = spec["cluster_name"]
        kubernetes_version = spec["kubernetes_version"]
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key or len(idempotency_key) > 128:
            return Response({"detail": "A valid Idempotency-Key header is required."}, status=400)

        deployment = ClusterDeployment.objects.filter(
            tenant=tenant,
            idempotency_key=idempotency_key,
        ).first()
        created = False
        if deployment is None:
            try:
                deployment = ClusterDeployment.objects.create(
                    tenant=tenant,
                    project=project,
                    eph_node=eph_node,
                    requested_by=request.user,
                    cluster_name=cluster_name,
                    kubernetes_version=kubernetes_version,
                    spec=spec,
                    status=ClusterDeployment.Status.PREFLIGHT_QUEUED,
                    idempotency_key=idempotency_key,
                )
                created = True
            except IntegrityError:
                deployment = ClusterDeployment.objects.get(
                    tenant=tenant,
                    idempotency_key=idempotency_key,
                )
        if (
            deployment.project_id != project.id
            or deployment.eph_node_id != eph_node.id
            or deployment.spec != spec
        ):
            return Response(
                {"detail": "Idempotency-Key was used for a different cluster plan."},
                status=409,
            )

        plan_result = deployment.plan_result if isinstance(deployment.plan_result, dict) else {}
        control = plan_result.get("cloud_control")
        if isinstance(control, dict) and isinstance(control.get("spec"), dict):
            installation_id = uuid.UUID(control["installation_id"])
            cluster_id = uuid.UUID(control["cluster_id"])
            control_spec = control["spec"]
        else:
            try:
                control_spec = build_cloud_control_spec(
                    spec,
                    project_id=project.id,
                    cluster_name=cluster_name,
                )
            except (FleetInventoryEmpty, FleetInventoryInsufficient) as exc:
                if created:
                    deployment.delete()
                code = exc.code
                return Response(
                    {
                        "detail": str(exc) or "No complete bare-metal fleet is available.",
                        "code": code,
                    },
                    status=409,
                )
            installation_id, cluster_id = control_identity(
                tenant.id,
                project.id,
                eph_node.id,
                cluster_name,
            )
            plan_result = {
                **plan_result,
                "cloud_control": {
                    "deployment_id": str(deployment.id),
                    "installation_id": str(installation_id),
                    "cluster_id": str(cluster_id),
                    "spec": control_spec,
                },
            }
            deployment.plan_result = plan_result
            deployment.save(update_fields=["plan_result", "updated_at"])

        try:
            result = dispatch_deployment(
                deployment_id=deployment.id,
                installation_id=installation_id,
                cluster_id=cluster_id,
                spec=control_spec,
            )
        except CloudControlNotConfigured:
            return Response(
                {
                    "detail": "Cloud Control API is not configured on this platform.",
                    "code": "CLOUD_CONTROL_NOT_CONFIGURED",
                },
                status=503,
            )
        except CloudControlBridgeError:
            deployment.error = {
                "code": "CLOUD_CONTROL_DISPATCH_FAILED",
                "retryable": True,
            }
            deployment.save(update_fields=["error", "updated_at"])
            return Response(
                {
                    "detail": "Cloud Control did not accept the deployment.",
                    "code": "CLOUD_CONTROL_DISPATCH_FAILED",
                },
                status=502,
            )

        with transaction.atomic():
            deployment = ClusterDeployment.objects.select_for_update().get(pk=deployment.pk)
            if deployment.status not in {
                ClusterDeployment.Status.SUCCEEDED,
                ClusterDeployment.Status.FAILED,
                ClusterDeployment.Status.CANCELLED,
            }:
                deployment.status = ClusterDeployment.Status.PROVISIONING
                deployment.started_at = deployment.started_at or timezone.now()
            deployment.error = None
            deployment.save(update_fields=["status", "error", "started_at", "updated_at"])

        if created:
            record_audit_event(
                request,
                tenant,
                "cluster_deployment.created",
                "cluster_deployment",
                deployment.id,
                project=project,
                metadata={
                    "cluster_name": deployment.cluster_name,
                    "kubernetes_version": deployment.kubernetes_version,
                    "eph_node_id": str(eph_node.id),
                },
            )
        return Response(
            {
                **_deployment_data(deployment),
                "cloud_control_result": result,
            },
            status=202 if created else 200,
        )


class ClusterDeploymentDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, deployment_id):
        deployment = get_object_or_404(
            ClusterDeployment.objects.filter(tenant__memberships__user=request.user),
            pk=deployment_id,
        )
        if not _can_edit_project(request.user, deployment.tenant, deployment.project):
            if not ProjectMembership.objects.filter(
                project=deployment.project,
                user=request.user,
            ).exists():
                return Response({"detail": "Project membership required."}, status=403)
        return Response(_deployment_data(deployment))
