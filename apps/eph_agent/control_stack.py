"""Bootstrap cluster control-stack install planning.

Before the EPH Agent can register BareMetalHosts and render CAPI/Metal3
resources, the single-node bootstrap cluster must carry the provider control
plane: cert-manager, the CAPI core controllers, the BareMetal Operator,
Ironic and the CAPM3 infrastructure provider.

Community Metal3 bring-up normally installs these through a sequence of
``clusterctl init`` calls and hand-written manifests. This module turns that
sequence into a declared, ordered, validated plan so the EPH Agent can apply
it idempotently and observe each controller becoming ready.

The planner is pure: it validates the supplied spec and returns typed
component descriptors plus readiness probes. It does not run ``kubectl``,
``clusterctl`` or any installer; execution stays in the injected runner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .metal3_skill import Metal3SkillError

_NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_VERSION_PATTERN = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?$")


@dataclass(frozen=True)
class StackComponent:
    name: str
    namespace: str
    controller: str
    install_order: int
    version: str | None
    custom_resource: dict[str, str] | None = None

    def readiness_probe(self) -> dict[str, str]:
        return {
            "kind": "Deployment",
            "name": self.controller,
            "namespace": self.namespace,
        }

    def custom_resource_probe(self) -> dict[str, str] | None:
        """Return the workload CR whose Ready condition gates this component.

        A ready operator Deployment does not prove the workload it manages is
        serving. Ironic is reconciled by the Ironic Standalone Operator, so the
        EPH must observe the ``Ironic`` resource itself before it registers
        BareMetalHosts against that endpoint.
        """

        return dict(self.custom_resource) if self.custom_resource else None


#: Canonical install order on the bootstrap cluster. cert-manager must lead;
#: the CAPI core must exist before the infrastructure provider is initialized.
#:
#: Controller names are the Deployments each installer actually creates, not
#: component labels: cert-manager ships ``cert-manager`` (its webhook and
#: cainjector are separate Deployments), Metal3 IPAM ships
#: ``ipam-controller-manager``, and the lab installs Ironic through the
#: Ironic Standalone Operator. A mismatch here is not cosmetic: the adapter
#: observes these names via ``kubectl get deployment`` and would wait forever
#: on a Deployment that does not exist.
_COMPONENTS: tuple[tuple[str, str, str, dict[str, str] | None], ...] = (
    ("cert-manager", "cert-manager", "cert-manager", None),
    ("ipam", "metal3-ipam-system", "ipam-controller-manager", None),
    ("capi-core", "capi-system", "capi-controller-manager", None),
    (
        "capi-bootstrap",
        "capi-kubeadm-bootstrap-system",
        "capi-kubeadm-bootstrap-controller-manager",
        None,
    ),
    (
        "capi-control-plane",
        "capi-kubeadm-control-plane-system",
        "capi-kubeadm-control-plane-controller-manager",
        None,
    ),
    (
        "baremetal-operator",
        "baremetal-operator-system",
        "baremetal-operator-controller-manager",
        None,
    ),
    (
        "ironic",
        "ironic-standalone-operator-system",
        "ironic-standalone-operator-controller-manager",
        {
            "api_version": "ironic.metal3.io/v1alpha1",
            "kind": "Ironic",
            "name": "ironic",
            "namespace": "baremetal-operator-system",
        },
    ),
    ("capm3", "capm3-system", "capm3-controller-manager", None),
)


def _valid_name(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _NAME_PATTERN.fullmatch(value) or len(value) > 63:
        raise Metal3SkillError(code)
    return value


def _valid_version(value: Any) -> str:
    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        raise Metal3SkillError("CONTROL_STACK_VERSION_INVALID")
    return value


def plan_control_stack(spec: Any) -> list[StackComponent]:
    """Return the ordered control-stack components for the bootstrap cluster.

    ``spec.versions`` may pin a semver version per component name; an omitted
    version leaves the component unpinned rather than filling a hard-coded
    default. Unknown component keys are rejected fail-closed.
    """

    if not isinstance(spec, dict):
        raise Metal3SkillError("CONTROL_STACK_SPEC_INVALID")
    versions = spec.get("versions", {})
    if versions is None:
        versions = {}
    if not isinstance(versions, dict):
        raise Metal3SkillError("CONTROL_STACK_VERSIONS_INVALID")

    known_names = {entry[0] for entry in _COMPONENTS}
    for component_name in versions:
        if component_name not in known_names:
            raise Metal3SkillError("CONTROL_STACK_UNKNOWN_COMPONENT")

    planned: list[StackComponent] = []
    for index, (name, namespace, controller, custom_resource) in enumerate(_COMPONENTS):
        _valid_name(namespace, "CONTROL_STACK_NAMESPACE_INVALID")
        version = versions.get(name)
        normalized_version = _valid_version(version) if version is not None else None
        planned.append(
            StackComponent(
                name=name,
                namespace=namespace,
                controller=controller,
                install_order=index + 1,
                version=normalized_version,
                custom_resource=dict(custom_resource) if custom_resource else None,
            )
        )
    return planned


def install_order() -> list[str]:
    """Return the canonical component install order as names."""

    return [entry[0] for entry in _COMPONENTS]
