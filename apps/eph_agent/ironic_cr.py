"""Render the Ironic workload CR from the shared platform contract.

The contract lives in :mod:`packages.agent_contracts.ironic_spec` because the
platform validates the same deployment spec before dispatch. This module only
wraps the normalised spec into the Kubernetes resource the EPH applies, so the
two applications cannot drift apart.

Nothing here talks to a cluster; the caller applies the returned document.
"""

from __future__ import annotations

from typing import Any

from packages.agent_contracts.ironic_spec import (
    API_VERSION,
    CR_NAME,
    CR_NAMESPACE,
    CRD_NAME,
    IronicSpecError,
    normalize_ironic_config,
)

IronicRenderError = IronicSpecError
IRONIC_CRD_NAME = CRD_NAME


def render_ironic_cr(spec: Any) -> dict[str, Any] | None:
    """Return the ``Ironic`` CR implied by ``spec.ironic``, or ``None``.

    ``None`` means the caller did not configure the workload here and must
    supply the CR through its own manifest source.
    """

    if not isinstance(spec, dict):
        raise IronicRenderError("IRONIC_SPEC_INVALID")
    ironic = spec.get("ironic")
    if ironic is None:
        return None
    return {
        "apiVersion": API_VERSION,
        "kind": "Ironic",
        "metadata": {"name": CR_NAME, "namespace": CR_NAMESPACE},
        "spec": normalize_ironic_config(ironic),
    }


def contains_ironic_cr(documents: Any) -> bool:
    """True when a manifest source already carries an ``Ironic`` resource."""

    if not isinstance(documents, list):
        return False
    return any(
        isinstance(document, dict)
        and document.get("kind") == "Ironic"
        and document.get("apiVersion") == API_VERSION
        for document in documents
    )
