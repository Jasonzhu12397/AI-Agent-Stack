import pytest

from apps.cloud_control_agent.workspace import (
    WorkspaceError,
    normalize_plugin,
    normalize_project,
    normalize_skill,
    normalize_task,
    normalize_work,
    normalize_work_result,
    normalize_workflow_definition,
    normalize_workflow_run,
    normalize_workspace_path,
)


def test_project_normalization_accepts_dns_label_and_defaults():
    project = normalize_project(name="edge-lab", description="Lab project")
    assert project["name"] == "edge-lab"
    assert project["status"] == "active"
    assert project["metadata"] == {}


@pytest.mark.parametrize(
    "name",
    ["", "-edge", "edge lab", "edge/lab", "a" * 64],
)
def test_project_normalization_rejects_invalid_names(name):
    with pytest.raises(WorkspaceError):
        normalize_project(name=name)


def test_project_normalization_rejects_unknown_status():
    with pytest.raises(WorkspaceError):
        normalize_project(name="edge-lab", status="deleted")


def test_skill_requires_entrypoint_and_unique_dotted_actions():
    skill = normalize_skill(
        name="metal3-prep",
        version="1.2.0",
        manifest={"entrypoint": "skills.metal3.prepare", "actions": ["bmh.apply", "bmh.wait"]},
    )
    assert skill["manifest"]["entrypoint"] == "skills.metal3.prepare"

    with pytest.raises(WorkspaceError):
        normalize_skill(name="metal3-prep", version="1.0.0", manifest={})

    with pytest.raises(WorkspaceError):
        normalize_skill(
            name="metal3-prep",
            version="1.0.0",
            manifest={"entrypoint": "run", "actions": ["apply", "apply"]},
        )


def test_skill_manifest_is_size_limited():
    manifest = {"entrypoint": "run", "blob": "x" * (70 * 1024)}
    with pytest.raises(WorkspaceError):
        normalize_skill(name="big", version="1.0.0", manifest=manifest)


def test_plugin_requires_indirect_credential_reference():
    plugin = normalize_plugin(
        name="vault",
        version="2.0.0",
        kind="connector",
        config_ref="secret://tenants/edge/vault",
    )
    assert plugin["config_ref"].startswith("secret://")

    with pytest.raises(WorkspaceError):
        normalize_plugin(
            name="vault",
            version="2.0.0",
            kind="connector",
            config_ref="https://user:pass@vault.example.com",
        )

    with pytest.raises(WorkspaceError):
        normalize_plugin(name="vault", version="2.0.0", kind="driver")


@pytest.mark.parametrize("kind", ["deployment", "maintenance", "inspection", "diagnostic"])
def test_work_accepts_known_kinds(kind):
    work = normalize_work(
        kind=kind,
        payload={"cluster": "edge-01"},
        required_role="eph",
        priority=5,
        idempotency_key="work-001",
    )
    assert work["required_role"] == "eph"
    assert work["priority"] == 5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "unknown"),
        ("required_role", "cloud"),
        ("priority", 10),
        ("priority", True),
        ("idempotency_key", ""),
    ],
)
def test_work_rejects_invalid_contract_fields(field, value):
    document = {
        "kind": "deployment",
        "payload": {},
        "required_role": None,
        "priority": 0,
        "idempotency_key": "work-001",
    }
    document[field] = value
    with pytest.raises(WorkspaceError):
        normalize_work(**document)


def test_work_result_only_accepts_terminal_statuses():
    assert normalize_work_result(status="succeeded", result={"ok": True})["status"] == "succeeded"
    with pytest.raises(WorkspaceError):
        normalize_work_result(status="queued", result={})


def test_workspace_path_splits_project_collections():
    project_id, collection = normalize_workspace_path("/api/v1/control/projects/abc")
    assert project_id == "abc"
    assert collection is None

    project_id, collection = normalize_workspace_path("/api/v1/control/projects/abc/skills/")
    assert project_id == "abc"
    assert collection == "skills"

    with pytest.raises(WorkspaceError):
        normalize_workspace_path("/api/v1/control/projects/abc/secrets")


def test_workflow_definition_accepts_a_dag():
    definition = normalize_workflow_definition({
        "description": "Prepare then inspect",
        "steps": [
            {"id": "prepare", "name": "Prepare hosts", "required_role": "eph"},
            {
                "id": "inspect",
                "name": "Inspect hosts",
                "depends_on": ["prepare"],
                "payload": {"selector": "all"},
            },
        ],
    })
    assert [step["id"] for step in definition["steps"]] == ["prepare", "inspect"]
    assert definition["steps"][1]["depends_on"] == ["prepare"]


def test_workflow_definition_rejects_cycles_and_unknown_dependencies():
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition({
            "steps": [
                {"id": "a", "name": "A", "depends_on": ["b"]},
                {"id": "b", "name": "B", "depends_on": ["a"]},
            ],
        })
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition({
            "steps": [{"id": "a", "name": "A", "depends_on": ["missing"]}],
        })
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition({
            "steps": [{"id": "a", "name": "A", "depends_on": ["a"]}],
        })


def test_workflow_definition_rejects_duplicate_ids_and_bad_roles():
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition({
            "steps": [
                {"id": "a", "name": "A"},
                {"id": "a", "name": "Again"},
            ],
        })
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition({
            "steps": [{"id": "a", "name": "A", "required_role": "cloud"}],
        })


def test_task_normalization_and_workflow_run_input():
    task = normalize_task(
        name="Check NTP",
        payload={"hosts": ["10.0.0.1"]},
        required_role="target",
        priority=3,
        idempotency_key="task-ntp-001",
    )
    assert task["required_role"] == "target"
    assert task["priority"] == 3

    run = normalize_workflow_run(input={"cluster": "edge-01"}, idempotency_key="run-001")
    assert run["input"] == {"cluster": "edge-01"}


def test_workspace_path_accepts_tasks_and_workflows():
    _, collection = normalize_workspace_path("/api/v1/control/projects/abc/tasks")
    assert collection == "tasks"
    _, collection = normalize_workspace_path("/api/v1/control/projects/abc/workflows")
    assert collection == "workflows"
