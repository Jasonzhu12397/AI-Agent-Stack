# ruff: noqa: I001
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from packages.agent_contracts.infra_networks import (
    InfraNetworkError,
    plan_infra_networks,
    port_inventory_findings,
    render_ip_pools,
    render_role_network_data,
)
from core.audit import record_audit_event
from core.agent_node_views import _tenant_and_project
from core.models import (
    AgentMachine,
    InfrastructureNetworkProfile,
    ProjectMembership,
    TenantMembership,
)


def _can_edit(user, tenant, project):
    return (
        TenantMembership.objects.filter(
            tenant=tenant,
            user=user,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        ).exists()
        or ProjectMembership.objects.filter(
            project=project,
            user=user,
            role__in=[ProjectMembership.Role.ADMIN, ProjectMembership.Role.EDITOR],
        ).exists()
    )


def _collected_hosts(tenant, project) -> list[dict[str, object]]:
    """Return every host inventory the project's machines have reported.

    The ports a profile may bind are hardware facts. The machines that were
    discovered by PXE report their NICs here, so the profile can be checked
    against what exists instead of what an operator remembers.
    """

    hosts: list[dict[str, object]] = []
    for machine in AgentMachine.objects.filter(tenant=tenant, project=project).select_related(
        "agent_node"
    ):
        raw = machine.hardware_inventory
        inventory = raw if isinstance(raw, dict) else {}
        hosts.append(
            {
                "bmh_name": machine.bmh_name
                or (machine.agent_node.name if machine.agent_node else ""),
                "nics": inventory.get("nics") or [],
                "boot_mac": inventory.get("boot_mac") or "",
            }
        )
    return [host for host in hosts if host["bmh_name"]]


def _network_plan_response(profile, cluster_name="network-preview"):
    plan = plan_infra_networks(profile)
    try:
        role_network_data = {
            role: render_role_network_data(plan, role)
            for role in ("control_plane", "worker", "storage")
        }
        ip_pools = render_ip_pools(plan, cluster_name=cluster_name)
    except InfraNetworkError as exc:
        return None, str(exc)
    return {
        "networks": plan["networks"],
        "dns_servers": plan["dns_servers"],
        "role_network_data": role_network_data,
        "ip_pools": ip_pools,
    }, None


class InfrastructureNetworkProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, tenant_id, project_id):
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        record = InfrastructureNetworkProfile.objects.filter(tenant=tenant, project=project).first()
        profile = record.profile if record and isinstance(record.profile, dict) else {}
        if not profile.get("networks"):
            return Response({"profile": {}, "generated": None})
        generated, error = _network_plan_response(profile)
        if error:
            return Response({"profile": profile, "generated": None, "detail": error}, status=409)
        plan = plan_infra_networks(profile)
        port_inventory = port_inventory_findings(plan, _collected_hosts(tenant, project))
        return Response(
            {"profile": profile, "generated": generated, "port_inventory": port_inventory}
        )

    def put(self, request, tenant_id, project_id):
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        if not _can_edit(request.user, tenant, project):
            return Response({"detail": "Project editor permission required."}, status=403)
        profile = request.data.get("profile", request.data)
        if not isinstance(profile, dict):
            return Response({"detail": "profile must be a JSON object."}, status=400)
        try:
            plan = plan_infra_networks(profile)
            generated, error = _network_plan_response(profile)
        except InfraNetworkError as exc:
            return Response({"detail": str(exc)}, status=400)
        if error:
            return Response({"detail": error}, status=400)
        stored = {"networks": plan["networks"], "dns_servers": plan["dns_servers"]}
        record, _ = InfrastructureNetworkProfile.objects.update_or_create(
            tenant=tenant,
            project=project,
            defaults={"profile": stored, "updated_by": request.user},
        )
        record_audit_event(
            request,
            tenant,
            "infrastructure_network_profile.updated",
            "infrastructure_network_profile",
            resource_id=str(record.id),
            project=project,
            metadata={"network_count": len(stored["networks"])},
        )
        port_inventory = port_inventory_findings(plan, _collected_hosts(tenant, project))
        return Response(
            {
                "profile": stored,
                "generated": generated,
                "port_inventory": port_inventory,
                "warnings": port_inventory["warnings"],
            }
        )

    def delete(self, request, tenant_id, project_id):
        tenant, project, _ = _tenant_and_project(request, tenant_id, project_id)
        if not _can_edit(request.user, tenant, project):
            return Response({"detail": "Project editor permission required."}, status=403)
        InfrastructureNetworkProfile.objects.filter(tenant=tenant, project=project).delete()
        record_audit_event(
            request,
            tenant,
            "infrastructure_network_profile.deleted",
            "infrastructure_network_profile",
            project=project,
        )
        return Response(status=204)
