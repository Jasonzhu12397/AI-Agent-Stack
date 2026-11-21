from apps.eph_agent import metal3_readiness


def test_ipam_readiness_uses_its_own_namespace():
    assert ("metal3-ipam-system", "ipam-controller-manager") in (
        metal3_readiness.PROVIDER_DEPLOYMENTS
    )


def test_ipam_replicaset_provider_is_scoped_to_its_own_namespace():
    entry = next(
        item for item in metal3_readiness.PROVIDER_REPLICASETS if item[1] == "ipam-metal3"
    )
    assert entry[2] == "metal3-ipam-system"
