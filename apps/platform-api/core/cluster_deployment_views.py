# ruff: noqa: I001
import datetime
import re
import uuid
from typing import Any

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
from packages.agent_contracts.host_network_verify import verify_host_networks
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
from packages.agent_contracts.eph_network import (
    EphNetworkError,
    eph_network_state,
    observed_sriov_pci_defaults,
)
from packages.agent_contracts.ironic_spec import (
    IronicSpecError,
    apply_provisioning_defaults,
    normalize_ironic_config,
    provisioning_defaults,
)
from core.cloud_control_client import CloudControlDispatchError
from core.cloud_control_bridge import (
    CloudControlBridgeError,
    CloudControlNotConfigured,
    FleetInventoryEmpty,
    FleetInventoryInsufficient,
    build_cloud_control_client,
    build_cloud_control_spec,
    control_identity,
    dispatch_deployment,
)
from core.models import (
    AgentMachine,
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
        "preflight_result": deployment.preflight_result,
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


NETWORK_READINESS_OBSERVATION_WINDOW = datetime.timedelta(hours=24)


def _network_readiness(project, spec):
    """Return the pre-install network readiness report for a deployment spec.

    The gate only applies when the deployment actually carries an infrastructure
    network profile; projects without one keep the historical behaviour.
    A machine is counted for a role only when its observed OS inventory is
    fresh and passes the role checks for the configured networks.
    """
    profile = spec.get("infra_network_profile")
    if profile is None:
        return None
    required_control = spec.get("control_plane_replicas", 0)
    required_worker = spec.get("worker_replicas", 0)
    fresh_after = timezone.now() - NETWORK_READINESS_OBSERVATION_WINDOW
    machines = AgentMachine.objects.filter(
        project=project,
        observed_at__gte=fresh_after,
    ).order_by("agent_node__name")
    control_ready = []
    worker_ready = []
    blocked = []
    with_findings = []
    evidence = []
    for machine in machines:
        inventory = machine.hardware_inventory
        evidence.append(
            {
                "machine_id": str(machine.id),
                "name": machine.agent_node.name,
                "inventory_revision": machine.inventory_revision,
                "observed_at": machine.observed_at.isoformat(),
            }
        )
        role_results = {}
        role_reports = {}
        for role in ("control_plane", "worker"):
            try:
                result = verify_host_networks(profile, role, inventory)
            except (InfraNetworkError, ValueError) as exc:
                role_results[role] = False
                role_reports[role] = {
                    "ok": False,
                    "checked_networks": [],
                    "findings": [
                        {
                            "code": "HOST_NETWORK_PROFILE_INVALID",
                            "status": "fail",
                            "network": None,
                            "interface": None,
                            "expected": None,
                            "observed": str(exc),
                        }
                    ],
                }
                continue
            role_results[role] = bool(result["ok"])
            role_reports[role] = {
                "ok": bool(result["ok"]),
                "checked_networks": result.get("checked_networks", []),
                "findings": result.get("findings", [])[:20],
            }
        if role_results.get("control_plane"):
            control_ready.append(str(machine.id))
        if role_results.get("worker"):
            worker_ready.append(str(machine.id))
        if not any(role_results.values()):
            blocked.append(
                {
                    "machine_id": str(machine.id),
                    "name": machine.agent_node.name,
                    "reason": "network_verification_failed",
                }
            )
        if not all(role_results.values()):
            with_findings.append(
                {
                    "machine_id": str(machine.id),
                    "name": machine.agent_node.name,
                    "roles": role_reports,
                }
            )
    policies = spec.get("sriov_node_policy_manifests") or []
    sriov_ready = []
    sriov_findings = []
    if policies:
        for machine in machines:
            inventory = machine.hardware_inventory
            nics = inventory.get("nics") if isinstance(inventory, dict) else None
            if not isinstance(nics, list):
                continue
            pci_index = {}
            for nic in nics:
                if not isinstance(nic, dict):
                    continue
                pci = nic.get("pci_address") or nic.get("pci")
                if isinstance(pci, str) and pci:
                    pci_index[pci.lower()] = nic
            machine_ok = True
            for policy in policies:
                if not isinstance(policy, dict):
                    continue
                policy_spec = policy.get("spec") if isinstance(policy.get("spec"), dict) else {}
                selector = (
                    policy_spec.get("nicSelector")
                    if isinstance(policy_spec.get("nicSelector"), dict)
                    else {}
                )
                expected_pci = (
                    selector.get("rootDevices")
                    or selector.get("pciAddresses")
                    or []
                )
                num_vfs = policy_spec.get("numVfs")
                policy_name = (
                    policy.get("metadata", {}).get("name")
                    if isinstance(policy.get("metadata"), dict)
                    else "sriov"
                )
                for pci in expected_pci:
                    normalized = str(pci).lower()
                    nic = pci_index.get(normalized)
                    if nic is None:
                        machine_ok = False
                        sriov_findings.append(
                            {
                                "machine_id": str(machine.id),
                                "name": machine.agent_node.name,
                                "policy": policy_name,
                                "code": "SRIOV_PCI_ADDRESS_MISSING",
                                "pci_address": normalized,
                            }
                        )
                        continue
                    total_vfs = nic.get("sriov_totalvfs")
                    if (
                        isinstance(num_vfs, int)
                        and isinstance(total_vfs, int)
                        and not isinstance(total_vfs, bool)
                        and total_vfs < num_vfs
                    ):
                        machine_ok = False
                        sriov_findings.append(
                            {
                                "machine_id": str(machine.id),
                                "name": machine.agent_node.name,
                                "policy": policy_name,
                                "code": "SRIOV_TOTALVFS_INSUFFICIENT",
                                "pci_address": normalized,
                                "expected": num_vfs,
                                "observed": total_vfs,
                            }
                        )
            if machine_ok:
                sriov_ready.append(str(machine.id))
    report = {
        "required_control_plane": required_control,
        "required_worker": required_worker,
        "control_plane_ready": len(control_ready),
        "worker_ready": len(worker_ready),
        "observed_machines": machines.count(),
        "blocked_machines": blocked,
        "machines_with_findings": with_findings,
        "required_sriov_workers": required_worker if policies else 0,
        "sriov_ready": len(sriov_ready) if policies else 0,
        "sriov_findings": sriov_findings,
        "evidence": evidence,
        "evaluated_at": timezone.now().isoformat(),
    }
    ready = len(control_ready) >= required_control and len(worker_ready) >= required_worker
    if policies:
        ready = ready and len(sriov_ready) >= required_worker
    report["ready"] = ready
    return report


def _network_evidence_drift(project, spec):
    """Compare the stored collection evidence with the current machine inventory."""
    evidence = spec.get("network_evidence")
    if not isinstance(evidence, list) or not evidence:
        return None
    drift = []
    for item in evidence:
        if not isinstance(item, dict):
            drift.append({"machine_id": None, "code": "NETWORK_EVIDENCE_INVALID"})
            continue
        machine_id = item.get("machine_id")
        machine = AgentMachine.objects.filter(
            project=project,
            id=machine_id,
        ).first()
        if machine is None:
            drift.append(
                {
                    "machine_id": machine_id,
                    "code": "NETWORK_EVIDENCE_MACHINE_MISSING",
                }
            )
            continue
        revision = item.get("inventory_revision")
        observed_at = item.get("observed_at")
        current_observed = (
            machine.observed_at.isoformat() if machine.observed_at is not None else None
        )
        if revision != machine.inventory_revision or observed_at != current_observed:
            drift.append(
                {
                    "machine_id": machine_id,
                    "code": "NETWORK_EVIDENCE_STALE",
                    "expected_revision": revision,
                    "current_revision": machine.inventory_revision,
                    "expected_observed_at": observed_at,
                    "current_observed_at": current_observed,
                }
            )
    return drift


def _validated_spec(request, *, infra_profile=None, project=None):
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
    network_defaults: dict[str, Any] = {"sriov_pci_sources": {}, "warnings": []}
    if infra_profile is not None and project is not None:
        inventories = list(
            AgentMachine.objects.filter(project=project)
            .exclude(observed_at=None)
            .values_list("hardware_inventory", flat=True)
        )
        try:
            infra_profile, sources, warnings = observed_sriov_pci_defaults(
                infra_profile, inventories
            )
        except EphNetworkError as exc:
            return None, f"infra_network_profile: {exc}"
        spec["infra_network_profile"] = infra_profile
        network_defaults["sriov_pci_sources"] = sources
        network_defaults["warnings"] = warnings
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
            addresses = spec.get("eph_network_addresses")
            if addresses is not None:
                spec["eph_network"] = eph_network_state(plan, addresses=addresses)
                network_defaults["eph_network_configured"] = True
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
        except EphNetworkError as exc:
            return None, f"eph_network: {exc}"
    spec["network_defaults"] = network_defaults
    ironic_config = spec.get("ironic")
    if ironic_config is not None:
        if not isinstance(ironic_config, dict):
            return None, "ironic: IRONIC_SPEC_INVALID"
        networks = (spec.get("infra_network_profile") or {}).get("networks")
        provisioning = next(
            (
                network
                for network in (networks or [])
                if isinstance(network, dict)
                and network.get("role") == "provisioning"
                and network.get("enabled", True)
            ),
            None,
        )
        if provisioning is not None:
            defaults = provisioning_defaults(provisioning)
            addresses = spec.get("eph_network_addresses")
            if isinstance(addresses, dict):
                owned = addresses.get("provisioning") or addresses.get("control")
                if isinstance(owned, str) and owned.strip():
                    # The EPH-owned address beats the network gateway guess.
                    defaults["ip_address"] = owned.split("/", 1)[0].strip()
            ironic_config, filled = apply_provisioning_defaults(
                ironic_config,
                defaults,
            )
            if filled:
                # Persist the resolved values so the dispatch payload and the
                # EPH never have to re-derive them from the profile.
                spec["ironic"] = ironic_config
                spec["ironic_defaults_sources"] = filled
        try:
            normalize_ironic_config(ironic_config)
        except IronicSpecError as exc:
            return None, f"ironic: {exc.code}"
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
        spec, payload_error = _validated_spec(
            request, infra_profile=infra_profile, project=project
        )
        if payload_error:
            return Response({"detail": payload_error}, status=400)
        readiness = _network_readiness(project, spec)
        if readiness is not None and not readiness["ready"]:
            return Response(
                {
                    "detail": (
                        "Network readiness gate failed: "
                        f"{readiness['control_plane_ready']}/"
                        f"{readiness['required_control_plane']} control-plane and "
                        f"{readiness['worker_ready']}/"
                        f"{readiness['required_worker']} worker nodes verified. "
                        "Run Collect all node networks, then retry."
                    ),
                    "network_readiness": readiness,
                },
                status=409,
            )
        if readiness is not None:
            # Bind the plan to the exact inventory revision it was validated against.
            spec["network_evidence"] = readiness["evidence"]
        ironic_section = spec.get("ironic")
        preflight = {
            "network_readiness": readiness,
            "ironic_workload": {
                "configured": isinstance(ironic_section, dict),
                "warnings": (
                    []
                    if isinstance(ironic_section, dict)
                    else ["IRONIC_WORKLOAD_CR_REQUIRED"]
                ),
                "defaults_sources": spec.get("ironic_defaults_sources") or [],
            },
            "network_defaults": spec.get("network_defaults")
            or {"sriov_pci_sources": {}, "warnings": []},
        }
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
                    preflight_result=preflight,
                    status=ClusterDeployment.Status.PREFLIGHT_QUEUED,
                    idempotency_key=idempotency_key,
                )
                created = True
            except IntegrityError:
                deployment = ClusterDeployment.objects.get(
                    tenant=tenant,
                    idempotency_key=idempotency_key,
                )
        def _spec_signature(value):
            if not isinstance(value, dict):
                return value
            return {key: item for key, item in value.items() if key != "network_evidence"}

        if (
            deployment.project_id != project.id
            or deployment.eph_node_id != eph_node.id
            or _spec_signature(deployment.spec) != _spec_signature(spec)
        ):
            return Response(
                {"detail": "Idempotency-Key was used for a different cluster plan."},
                status=409,
            )

        plan_result = deployment.plan_result if isinstance(deployment.plan_result, dict) else {}
        if deployment.preflight_result != preflight:
            deployment.preflight_result = preflight
            deployment.save(update_fields=["preflight_result", "updated_at"])
        drift = _network_evidence_drift(project, deployment.spec)
        if drift:
            return Response(
                {
                    "detail": (
                        "Node network inventory changed after the readiness check. "
                        "Collect all node networks again before installing."
                    ),
                    "code": "NETWORK_EVIDENCE_DRIFT",
                    "drift": drift,
                },
                status=409,
            )
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

        if isinstance(control_spec, dict):
            # The control plane re-verifies these bindings before the Target
            # handoff, and the EPH renders the workload CR from ``ironic``.
            # Both are injected explicitly because a cached plan result may
            # predate either field.
            forwarded = {
                key: spec[key]
                for key in ("network_evidence", "ironic")
                if spec.get(key)
            }
            if forwarded:
                control_spec = {**control_spec, **forwarded}
            cached = plan_result.get("cloud_control")
            if isinstance(cached, dict) and cached.get("spec") != control_spec:
                # Store the exact bytes handed to the control plane. A cached
                # plan that predates a field would otherwise keep dropping it
                # on every repeated submission.
                plan_result = {
                    **plan_result,
                    "cloud_control": {**cached, "spec": control_spec},
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
        data = _deployment_data(deployment)
        drift = _network_evidence_drift(deployment.project, deployment.spec)
        if drift is None:
            data["network_evidence_status"] = "missing"
        else:
            data["network_evidence_status"] = {
                "status": "drift" if drift else "current",
                "drift": drift,
            }
        return Response(data)


class ClusterDeploymentEphPreflightView(APIView):
    """Pull the EPH runtime preflight verdict back onto the platform record.

    Readiness gating stays on the platform; this endpoint only mirrors what the
    EPH reported so the detail page can show runtime warnings instead of
    inferring them from the spec. The stored snapshot is explicit and timestamped,
    so a stale sync is visible rather than silently trusted.
    """

    permission_classes = [IsAuthenticated]

    MAX_WARNINGS = 32
    MAX_WARNING_LENGTH = 160

    def post(self, request, deployment_id):
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
        client = None
        try:
            client = build_cloud_control_client()
            payload = client.get_deployment_preflight(deployment.id)
        except (CloudControlNotConfigured, CloudControlBridgeError) as exc:
            return Response(
                {"detail": str(exc), "code": "eph_preflight_unavailable"},
                status=503,
            )
        except CloudControlDispatchError:
            return Response(
                {
                    "detail": "Cloud Control request failed.",
                    "code": "eph_preflight_unavailable",
                },
                status=502,
            )
        finally:
            if client is not None:
                client.close()
        output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
        raw_warnings = output.get("warnings")
        warnings = []
        if isinstance(raw_warnings, list):
            for item in raw_warnings[: self.MAX_WARNINGS]:
                text = str(item)[: self.MAX_WARNING_LENGTH]
                if text and text not in warnings:
                    warnings.append(text)
        record = {
            "available": bool(payload.get("available")),
            "reason": payload.get("reason"),
            "status": payload.get("status"),
            "error_code": payload.get("error_code"),
            "eph_ready": output.get("eph_ready"),
            "warnings": warnings,
            "observed_at": output.get("observed_at"),
            "command_id": payload.get("command_id"),
            "synced_at": timezone.now().isoformat(),
        }
        preflight = (
            dict(deployment.preflight_result)
            if isinstance(deployment.preflight_result, dict)
            else {}
        )
        preflight["eph_preflight"] = record
        deployment.preflight_result = preflight
        deployment.save(update_fields=["preflight_result", "updated_at"])
        return Response(
            {"eph_preflight": record, "preflight_result": preflight}
        )


class ClusterDeploymentNetworkEvidenceView(APIView):
    """Pre-execution evidence check for Target and control-plane automation."""

    permission_classes = [IsAuthenticated]

    def post(self, request, deployment_id):
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
        drift = _network_evidence_drift(deployment.project, deployment.spec)
        if drift is None:
            return Response(
                {
                    "status": "missing",
                    "detail": "This deployment has no bound network evidence.",
                },
                status=409,
            )
        if drift:
            return Response(
                {
                    "status": "drift",
                    "detail": (
                        "Node network inventory changed after the readiness check. "
                        "Re-run network collection and create a new deployment."
                    ),
                    "drift": drift,
                },
                status=409,
            )
        return Response(
            {
                "status": "current",
                "checked_machines": len(deployment.spec.get("network_evidence") or []),
            },
            status=200,
        )
