"""Management-cluster readiness is judged from structured data, not grep.

A shell check that could not read a field used to compare two empty strings and
pass. These tests pin the opposite behaviour: missing evidence is a failure with
a reason, and an Ironic API that answers anonymous reads is never called ready.
"""

from __future__ import annotations

import json

from apps.eph_agent import metal3_readiness as readiness


def ok(document):
    return {"available": True, "returncode": 0, "stdout": json.dumps(document)}


def crd_document(names):
    return {"items": [{"metadata": {"name": name}} for name in names]}


def deployment(replicas=1, ready=1):
    return {"status": {"replicas": replicas, "readyReplicas": ready}}


def test_all_crds_present_passes():
    def runner(args, timeout=60, **_):
        return ok(crd_document(readiness.REQUIRED_CRDS))

    findings = readiness.check_crds(runner)
    assert findings and all(item.ok for item in findings)


def test_a_missing_crd_fails_with_a_reason():
    present = readiness.REQUIRED_CRDS[:-1]

    def runner(args, timeout=60, **_):
        return ok(crd_document(present))

    findings = readiness.check_crds(runner)
    failed = [item for item in findings if not item.ok]
    assert len(failed) == 1
    assert readiness.REQUIRED_CRDS[-1] in failed[0].name
    assert failed[0].detail == "not installed"


def test_unreadable_cluster_is_a_failure_not_a_pass():
    def runner(args, timeout=60, **_):
        return {"available": False, "error": "OSError"}

    findings = readiness.check_crds(runner)
    assert len(findings) == 1
    assert not findings[0].ok
    assert "no JSON" in findings[0].detail


def test_deployment_requires_ready_to_equal_desired():
    calls = []

    def runner(args, timeout=60, **_):
        calls.append(args)
        return ok(deployment(replicas=2, ready=1))

    findings = readiness.check_deployments(runner, kubeconfig="/tmp/kubeconfig")
    assert len(findings) == len(readiness.PROVIDER_DEPLOYMENTS)
    assert all(not item.ok for item in findings)
    assert "ready=1 desired=2" in findings[0].detail
    assert calls[0][:2] == ["--kubeconfig", "/tmp/kubeconfig"]


def test_missing_deployment_is_reported():
    def runner(args, timeout=60, **_):
        return {"available": True, "returncode": 1, "stdout": ""}

    findings = readiness.check_deployments(runner)
    assert all(not item.ok and item.detail == "not found" for item in findings)


def test_replica_set_count_and_readiness_are_both_checked():
    def runner(args, timeout=60, **_):
        return ok({"items": [deployment(), deployment()]})

    findings = readiness.check_replicasets(runner)
    counts = [item for item in findings if "replica set count" in item.name]
    assert len(counts) == len(readiness.PROVIDER_REPLICASETS)
    assert all(not item.ok and "found=2 expected=1" in item.detail for item in counts)
    replicas = [item for item in findings if "replica set count" not in item.name]
    assert replicas and all(item.ok for item in replicas)


def test_bridges_are_read_from_json_not_grep():
    payload = [{"ifname": "provisioning"}, {"ifname": "eth0"}]

    def host(args, timeout=30, **_):
        return {"available": True, "returncode": 0, "stdout": json.dumps(payload)}

    findings = readiness.check_bridges(host)
    by_name = {item.name: item for item in findings}
    assert by_name["bridge provisioning"].ok
    assert not by_name["bridge external"].ok


def test_all_eph_infrastructure_is_checked_as_kubernetes_deployments():
    assert not hasattr(readiness, "check_containers")
    required = readiness.provider_deployments()
    for name in ("registry", "httpd-infra"):
        assert ("ai-agent-system", name) in required
    # A physical fleet has real BMCs; the emulators are only required when the
    # deployment actually declares a virtual one.
    for name in ("vbmc", "sushy-tools"):
        assert ("ai-agent-system", name) not in required
        assert ("ai-agent-system", name) in readiness.provider_deployments(
            emulated_bmc=True
        )


def test_ironic_must_refuse_anonymous_reads():
    assert readiness.check_ironic_auth(lambda url: 401, "https://10.0.0.5:6385/v1/")[0].ok

    exposed = readiness.check_ironic_auth(lambda url: 200, "https://10.0.0.5:6385/v1/")[0]
    assert not exposed.ok
    assert "exposed" in exposed.detail

    unreachable = readiness.check_ironic_auth(lambda url: None, "https://10.0.0.5:6385/v1/")[0]
    assert not unreachable.ok
    assert "no response" in unreachable.detail

    odd = readiness.check_ironic_auth(lambda url: 500, "https://10.0.0.5:6385/v1/")[0]
    assert not odd.ok
    assert "expected 401" in odd.detail


def test_ironic_endpoint_is_built_without_double_slashes():
    seen = []

    def probe(url):
        seen.append(url)
        return 401

    readiness.check_ironic_auth(probe, "https://10.0.0.5:6385/v1/")
    assert seen == ["https://10.0.0.5:6385/v1/nodes"]


def test_verify_management_cluster_collects_every_layer():
    def runner(args, timeout=60, **_):
        if "crds" in args:
            return ok(crd_document(readiness.REQUIRED_CRDS))
        if "replicasets" in args:
            return ok({"items": [deployment()]})
        return ok(deployment())

    def host(args, timeout=30, **_):
        if args[0] == "ip":
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps([{"ifname": "provisioning"}, {"ifname": "external"}]),
            }
        return {"available": True, "returncode": 0, "stdout": "httpd-infra\nregistry\n"}

    findings = readiness.verify_management_cluster(
        runner,
        host_runner=host,
        http_probe=lambda url: 401,
        ironic_url="https://10.0.0.5:6385/v1",
    )
    assert findings and all(item.ok for item in findings)
    assert any(item.name.startswith("CRD ") for item in findings)
    assert any(item.name.startswith("deployment ") for item in findings)
    assert any(item.name.startswith("bridge ") for item in findings)
    assert any("Ironic" in item.name for item in findings)


def test_finding_render_marks_failures():
    assert readiness.Finding("bridge external", False, "missing").render() == (
        "FAIL bridge external: missing"
    )
    assert readiness.Finding("CRD x", True).render() == "OK   CRD x"
