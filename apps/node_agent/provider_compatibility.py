import re
from typing import Any


class ProviderCompatibilityError(ValueError):
    pass


UPSTREAM_SNAPSHOT_DATE = "2026-11-05"
UPSTREAM_SOURCES = {
    "version_support": "https://book.metal3.io/version_support",
    "dev_env_test_matrix": "https://github.com/metal3-io/metal3-dev-env",
    "capm3_pivot_tests": "https://github.com/metal3-io/cluster-api-provider-metal3/blob/main/test/e2e/pivoting.go",
    "bmo_e2e_tests": "https://github.com/metal3-io/baremetal-operator/blob/main/test/e2e/README.md",
}

COMPONENT_RELEASE_STATUS = {
    "capm3": {"1.14": "supported", "1.13": "supported", "1.12": "tested"},
    "bmo": {"0.14": "supported", "0.13": "supported", "0.12": "tested"},
    "ironic": {"39.0": "supported", "38.0": "supported", "37.0": "supported", "35.0": "tested", "33.0": "eol"},
    "irso": {"0.12": "supported", "0.11": "supported", "0.10": "tested", "0.9": "eol"},
}

IRSO_IRONIC_COMPATIBILITY = {
    "0.12": {"39.0", "38.0", "37.0"},
    "0.11": {"38.0", "37.0", "35.0"},
    "0.10": {"37.0", "35.0", "34.0"},
    "0.9": {"35.0", "34.0", "33.0"},
}

CAPM3_API_VERSIONS = {
    "1.14": "v1beta2",
    "1.13": "v1beta2",
    "1.12": "v1beta1",
}

CI_TESTED_PROFILES = {
    ("1.14", "0.14", "37.0"),
    ("1.13", "0.13", "35.0"),
    ("1.12", "0.12", "33.0"),
}


def _release_minor(value: Any, component: str, allow_build_suffix: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ProviderCompatibilityError("INVALID_" + component.upper() + "_VERSION")
    suffix = r"(?:[-+][0-9A-Za-z.-]+)?" if allow_build_suffix else ""
    match = re.fullmatch(
        r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.[0-9]+)?" + suffix,
        value,
    )
    if match is None:
        raise ProviderCompatibilityError("INVALID_" + component.upper() + "_VERSION")
    return match.group(1) + "." + match.group(2)


def evaluate_provider_compatibility(payload: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = {
        "provider_versions",
        "kubernetes_version",
        "capi_api_version",
    }
    if not isinstance(payload, dict) or set(payload) - allowed_fields:
        raise ProviderCompatibilityError("PROVIDER_COMPATIBILITY_PAYLOAD_INVALID")
    versions = payload.get("provider_versions")
    required = {"capm3", "bmo", "ironic", "irso"}
    if not isinstance(versions, dict) or set(versions) != required:
        raise ProviderCompatibilityError("PROVIDER_VERSIONS_INCOMPLETE")

    normalized = {
        name: _release_minor(versions[name], name)
        for name in sorted(required)
    }
    kubernetes_version = payload.get("kubernetes_version")
    if kubernetes_version is not None:
        _release_minor(kubernetes_version, "kubernetes", allow_build_suffix=True)
    capi_api_version = payload.get("capi_api_version")
    if capi_api_version is not None and capi_api_version not in {"v1beta1", "v1beta2"}:
        raise ProviderCompatibilityError("INVALID_CAPI_API_VERSION")

    component_results = {}
    blockers = []
    warnings = []
    for component, release in normalized.items():
        status = COMPONENT_RELEASE_STATUS[component].get(release, "unsupported")
        component_results[component] = {
            "release": release,
            "status": status,
        }
        if status == "unsupported":
            blockers.append("unsupported_" + component + "_release:" + release)
        elif status == "eol":
            blockers.append("component_release_eol:" + component + ":" + release)
        elif status == "tested":
            warnings.append("component_release_tested_not_currently_supported:" + component)

    ironic_compatible = normalized["ironic"] in IRSO_IRONIC_COMPATIBILITY.get(
        normalized["irso"], set()
    )
    if not ironic_compatible:
        blockers.append(
            "irso_ironic_version_pair_not_supported:"
            + normalized["irso"]
            + "+"
            + normalized["ironic"]
        )

    expected_capi_api = CAPM3_API_VERSIONS.get(normalized["capm3"])
    capi_api_compatible = None
    if expected_capi_api is not None and capi_api_version is not None:
        capi_api_compatible = capi_api_version == expected_capi_api
        if not capi_api_compatible:
            blockers.append(
                "capm3_capi_api_version_mismatch:"
                + expected_capi_api
                + "_expected_got_"
                + capi_api_version
            )
    elif expected_capi_api is None:
        warnings.append("capm3_capi_api_mapping_not_in_snapshot")
    else:
        warnings.append("capi_api_version_not_supplied")

    ci_profile = (
        normalized["capm3"],
        normalized["bmo"],
        normalized["ironic"],
    )
    ci_tested = ci_profile in CI_TESTED_PROFILES
    if not ci_tested:
        warnings.append("provider_combination_not_listed_in_upstream_periodic_ci_matrix")

    if blockers:
        overall_status = "unsupported"
    elif any(item["status"] == "tested" for item in component_results.values()):
        overall_status = "tested"
    else:
        overall_status = "supported"

    return {
        "compatibility_status": overall_status,
        "component_versions": component_results,
        "kubernetes_version": kubernetes_version,
        "capi_api_version": capi_api_version,
        "expected_capi_api_version": expected_capi_api,
        "capi_api_compatible": capi_api_compatible,
        "irso_ironic_compatible": ironic_compatible,
        "upstream_ci_profile_tested": ci_tested,
        "upstream_ci_profile": {
            "capm3": ci_profile[0],
            "bmo": ci_profile[1],
            "ironic": ci_profile[2],
        },
        "blockers": blockers,
        "warnings": warnings,
        "mutation_authorized": False,
        "provisioning_ready": False,
        "evidence": {
            "snapshot_date": UPSTREAM_SNAPSHOT_DATE,
            "sources": UPSTREAM_SOURCES,
            "limitations": [
                "Kubernetes version compatibility is not inferred from provider release numbers.",
                "An upstream periodic CI profile is not proof of this project's deployment.",
                "The CI matrix does not enumerate every supported IrSO and Ironic pairing.",
                "Live CRD schemas, controller image digests, Ironic health, IPA assets, BMC reachability and provisioning network checks remain separate gates.",
            ],
        },
    }
