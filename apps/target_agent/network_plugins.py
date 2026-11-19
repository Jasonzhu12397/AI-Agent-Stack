"""Target cluster network-stack install planning.

After the Target Agent takes over the target control plane, the cluster needs
its networking layering installed and verified:

* ``calico``: cluster CNI and network policies (base, always enabled);
* ``multus``: multi-attach CNI for additional host/network interfaces;
* ``ovs``: virtual switching for the data plane;
* ``sriov``: SR-IOV CNI plus the SR-IOV device plugin for VF allocation;
* ``dpdk``: userspace high-performance data-plane datapath.

The traffic split is fixed by design: control-plane and management traffic use
the ordinary NIC, while Pod data traffic must use the SR-IOV/DPDK data plane
through a VF. The planner only declares which plugins belong to that stack and
validates the supplied configuration; it does not apply manifests or touch any
host NIC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_VERSION_PATTERN = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?$")


class NetworkStackError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


#: Supported plugins and their networking role.
_PLUGIN_ROLES: dict[str, str] = {
    "calico": "cni",
    "multus": "multus",
    "ovs": "switch",
    "sriov": "vf",
    "dpdk": "dataplane",
}

#: Plugins that are part of the high-performance data plane rather than the
#: control/management plane.
DATA_PLANE_PLUGINS: frozenset[str] = frozenset({"ovs", "sriov", "dpdk"})


@dataclass(frozen=True)
class NetworkPlugin:
    name: str
    role: str
    enabled: bool
    version: str | None
    config: dict[str, Any] | None


def _valid_version(value: Any) -> str:
    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        raise NetworkStackError("NETWORK_STACK_VERSION_INVALID")
    return value


def plan_network_stack(spec: Any) -> list[NetworkPlugin]:
    """Return the target network plugins, enabled according to the spec.

    ``calico`` is always enabled as the base CNI. Every other plugin is
    disabled unless explicitly listed with ``enabled: true``. Unknown plugin
    names, non-boolean flags and malformed versions are rejected fail-closed.
    """

    if not isinstance(spec, dict):
        raise NetworkStackError("NETWORK_STACK_SPEC_INVALID")
    plugins = spec.get("plugins", {})
    if plugins is None:
        plugins = {}
    if not isinstance(plugins, dict):
        raise NetworkStackError("NETWORK_STACK_PLUGINS_INVALID")

    unknown = set(plugins) - set(_PLUGIN_ROLES)
    if unknown:
        raise NetworkStackError("NETWORK_STACK_PLUGINS_INVALID")

    planned: list[NetworkPlugin] = []
    for name, role in _PLUGIN_ROLES.items():
        entry = plugins.get(name)
        if entry is None:
            enabled = name == "calico"
            version = None
            config = (
                {"dataplane": "eBPF", "kube_proxy_replacement": True}
                if name == "calico"
                else None
            )
        else:
            if not isinstance(entry, dict):
                raise NetworkStackError("NETWORK_STACK_PLUGIN_ENTRY_INVALID")
            enabled = entry.get("enabled", name == "calico")
            if not isinstance(enabled, bool):
                raise NetworkStackError("NETWORK_STACK_ENABLED_INVALID")
            version = entry.get("version")
            normalized_version = _valid_version(version) if version is not None else None
            config = entry.get("config")
            if config is not None and not isinstance(config, dict):
                raise NetworkStackError("NETWORK_STACK_CONFIG_INVALID")
            version = normalized_version
            if name == "calico":
                if not enabled:
                    raise NetworkStackError("NETWORK_STACK_CALICO_REQUIRED")
                normalized_config = dict(config or {})
                if normalized_config.get("dataplane", "eBPF") != "eBPF":
                    raise NetworkStackError("NETWORK_STACK_CALICO_EBPF_REQUIRED")
                if normalized_config.get("kube_proxy_replacement", True) is not True:
                    raise NetworkStackError("NETWORK_STACK_KUBE_PROXY_REPLACEMENT_REQUIRED")
                config = {
                    **normalized_config,
                    "dataplane": "eBPF",
                    "kube_proxy_replacement": True,
                }
        planned.append(
            NetworkPlugin(
                name=name,
                role=role,
                enabled=enabled,
                version=version,
                config=config,
            )
        )
    return planned


def data_plane_plugins() -> frozenset[str]:
    """Return the plugin names that belong to the high-performance data plane."""

    return DATA_PLANE_PLUGINS
