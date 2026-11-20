from apps.eph_agent import metal3_readiness


def test_ipam_readiness_uses_capm3_system_namespace():
    assert ("capm3-system", "ipam-manager") in metal3_readiness.PROVIDER_DEPLOYMENTS


def test_ipam_replicaset_provider_is_scoped_to_capm3_system():
    entry = next(
        item for item in metal3_readiness.PROVIDER_REPLICASETS if item[1] == "ipam-metal3"
    )
    assert entry[2] == "capm3-system"
