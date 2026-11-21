"""Expand the kustomize variable placeholders upstream release YAML carries.

Several upstream release artifacts (Cluster API, CAPM3, Metal3 IPAM) are
published as ``kustomize`` output *before* variable substitution, so their
controller args contain text such as ``${CAPI_DIAGNOSTICS_ADDRESS:=:8443}`` or
``--feature-gates=MachinePool=${EXP_MACHINE_POOL:=true}``. Kubernetes does not
expand ``${VAR:=default}``: a plain ``kubectl apply`` hands the literal text to
the container and the controller fails to parse its own flags. Vendoring the
files unmodified keeps their published checksums intact, so the expansion has
to happen when the manifest is installed.

Supported syntax is exactly what the upstream files use:

* ``${NAME:=default}`` — the default when no override is supplied;
* ``${NAME}`` — only valid with an override; anything else is refused rather
  than applied as literal text.
"""

from __future__ import annotations

import re

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:=([^}]*))?\}")


class KustomizeVarError(ValueError):
    """Raised when a placeholder has no default and no override."""

    def __init__(self, names: list[str]) -> None:
        super().__init__("unexpanded kustomize variables: " + ", ".join(names))
        self.code = "KUSTOMIZE_VAR_UNEXPANDED"
        self.names = tuple(names)


def expand_kustomize_vars(text: str, overrides: dict[str, str] | None = None) -> str:
    """Replace every ``${...}`` placeholder, or fail closed."""

    overrides = dict(overrides or {})
    unresolved: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in overrides:
            return overrides[name]
        if match.group(2) is not None:
            return match.group(3) or ""
        unresolved.add(name)
        return match.group(0)

    expanded = _PLACEHOLDER.sub(replace, text)
    if unresolved:
        raise KustomizeVarError(sorted(unresolved))
    return expanded


__all__ = ["KustomizeVarError", "expand_kustomize_vars"]
