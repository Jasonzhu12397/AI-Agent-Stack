from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "metal3-lab" / "run.sh"
README = ROOT / "deploy" / "metal3-lab" / "README.md"


def test_metal3_runner_pins_immutable_upstream_revision():
    source = RUNNER.read_text(encoding="utf-8")
    assert 'readonly UPSTREAM_REPO="https://github.com/metal3-io/metal3-dev-env.git"' in source
    assert 'readonly UPSTREAM_COMMIT="7076f07624cdfba990f83bea24f749b37e3baad7"' in source
    assert 'git -C "${UPSTREAM_DIR}" checkout --detach "${UPSTREAM_COMMIT}"' in source
    assert 'git rev-parse HEAD' in source


def test_host_mutating_actions_require_explicit_consent():
    source = RUNNER.read_text(encoding="utf-8")
    assert 'require_host_mutation_consent' in source
    assert 'METAL3_LAB_ALLOW_HOST_MUTATION:-' in source
    assert 'prepare)' in source
    assert 'lifecycle)' in source
    assert 'clean)' in source


def test_runner_exposes_upstream_full_lifecycle_and_cleanup():
    source = RUNNER.read_text(encoding="utf-8")
    assert 'make test' in source
    assert 'make clean' in source
    assert '01_prepare_host.sh' in source
    assert '02_configure_host.sh' in source
    assert '03_launch_mgmt_cluster.sh' in source
    assert '04_verify.sh' in source


def test_docs_distinguish_upstream_provider_e2e_from_agent_e2e():
    docs = README.read_text(encoding="utf-8")
    assert "not an AI Agent Stack three-agent end-to-end test" in docs
    assert "LIBVIRT_IRONIC" in docs
    assert "Cloud Control Agent" in docs
