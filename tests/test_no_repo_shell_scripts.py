"""Repository hygiene: project automation lives in Python, not shell scripts.

The vendored Metal3 lab tree is an untouched upstream copy and is excluded;
everything the repository itself owns must be importable Python so it can be
linted, unit-tested and reasoned about like the rest of the codebase.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDORED = ROOT / "deploy" / "metal3-lab" / "vendor"


def test_repository_owns_no_shell_scripts():
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*.sh")
        if VENDORED not in path.parents
        and ".git" not in path.parts
        and "node_modules" not in path.parts
        and ".venv" not in path.parts
    ]
    assert offenders == []


def test_python_entry_points_replace_the_former_scripts():
    assert (ROOT / "deploy/agents/build_agents.py").is_file()
    assert (ROOT / "deploy/agents/deploy_agents.py").is_file()
    assert (ROOT / "deploy/cloud-control-plane/deploy_cloud_control.py").is_file()
    assert (ROOT / "tests/integration/kind_metal3_crds.py").is_file()
    assert (ROOT / "tests/integration/test_agent_mtls_proxy.py").is_file()


def test_ci_runs_the_python_entry_points():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "bash -n" not in workflow
    assert "python3 tests/integration/test_agent_mtls_proxy.py" in workflow
    assert "python3 tests/integration/kind_metal3_crds.py" in workflow
    assert "python -m compileall" in workflow
