import pytest

from apps.cloud_control_agent.workspace import (
    WorkspaceError,
    normalize_approval,
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


@pytest.mark.parametrize(
    "kind", ["deployment", "maintenance", "inspection", "diagnostic", "software"]
)
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
    definition = normalize_workflow_definition(
        {
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
        }
    )
    assert [step["id"] for step in definition["steps"]] == ["prepare", "inspect"]
    assert definition["steps"][1]["depends_on"] == ["prepare"]


def test_workflow_definition_rejects_cycles_and_unknown_dependencies():
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [
                    {"id": "a", "name": "A", "depends_on": ["b"]},
                    {"id": "b", "name": "B", "depends_on": ["a"]},
                ],
            }
        )
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [{"id": "a", "name": "A", "depends_on": ["missing"]}],
            }
        )
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [{"id": "a", "name": "A", "depends_on": ["a"]}],
            }
        )


def test_workflow_definition_rejects_duplicate_ids_and_bad_roles():
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [
                    {"id": "a", "name": "A"},
                    {"id": "a", "name": "Again"},
                ],
            }
        )
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [{"id": "a", "name": "A", "required_role": "cloud"}],
            }
        )


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


def test_workflow_definition_defaults_and_validates_step_retries():
    definition = normalize_workflow_definition(
        {
            "steps": [{"id": "prepare", "name": "Prepare hosts"}],
        }
    )
    assert definition["steps"][0]["max_attempts"] == 1

    definition = normalize_workflow_definition(
        {
            "steps": [
                {"id": "prepare", "name": "Prepare hosts", "max_attempts": 3},
            ],
        }
    )
    assert definition["steps"][0]["max_attempts"] == 3

    for value in (0, 6, True, "3", 2.5):
        with pytest.raises(WorkspaceError, match="max_attempts"):
            normalize_workflow_definition(
                {
                    "steps": [
                        {"id": "prepare", "name": "Prepare hosts", "max_attempts": value},
                    ],
                }
            )


def test_approval_contract_requires_sha256_digest_and_bounded_ttl():
    approval = normalize_approval(
        work_id="00000000-0000-0000-0000-000000000001",
        action="apply-patch",
        payload_digest="a" * 64,
        requested_by="alice",
        ttl_seconds=3600,
    )
    assert approval["action"] == "apply-patch"
    with pytest.raises(WorkspaceError):
        normalize_approval(
            work_id="00000000-0000-0000-0000-000000000001",
            action="apply-patch",
            payload_digest="not-a-digest",
            requested_by="alice",
        )
    with pytest.raises(WorkspaceError):
        normalize_approval(
            work_id="00000000-0000-0000-0000-000000000001",
            action="apply-patch",
            payload_digest="a" * 64,
            requested_by="alice",
            ttl_seconds=10,
        )


def test_workflow_definition_accepts_human_approval_steps():
    definition = normalize_workflow_definition(
        {
            "steps": [
                {"id": "prepare", "name": "Prepare hosts", "required_role": "eph"},
                {
                    "id": "review",
                    "name": "Review plan",
                    "type": "approval",
                    "depends_on": ["prepare"],
                    "payload": {"instructions": "Verify the host inventory"},
                },
                {
                    "id": "apply",
                    "name": "Apply changes",
                    "depends_on": ["review"],
                },
            ],
        }
    )
    steps = {step["id"]: step for step in definition["steps"]}
    assert steps["review"]["type"] == "approval"
    assert steps["review"]["required_role"] is None
    assert steps["prepare"]["type"] == "task"


def test_workflow_definition_rejects_invalid_approval_steps():
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [
                    {
                        "id": "review",
                        "name": "Review",
                        "type": "approval",
                        "required_role": "eph",
                    }
                ],
            }
        )
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [
                    {
                        "id": "review",
                        "name": "Review",
                        "type": "approval",
                        "max_attempts": 2,
                    }
                ],
            }
        )
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {"steps": [{"id": "review", "name": "Review", "type": "manual"}]}
        )


def test_workflow_definition_supports_any_join_policies():
    definition = normalize_workflow_definition(
        {
            "steps": [
                {"id": "left", "name": "Left", "required_role": "eph"},
                {"id": "right", "name": "Right", "required_role": "target"},
                {
                    "id": "merge",
                    "name": "Merge",
                    "depends_on": ["left", "right"],
                    "join": "any",
                },
            ]
        }
    )
    steps = {step["id"]: step for step in definition["steps"]}
    assert steps["merge"]["join"] == "any"
    assert steps["left"]["join"] == "all"


def test_workflow_definition_rejects_invalid_join_policies():
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [
                    {"id": "a", "name": "A"},
                    {"id": "b", "name": "B", "depends_on": ["a"], "join": "race"},
                ],
            }
        )


def test_workflow_loop_steps_require_a_bounded_iteration_budget():
    definition = normalize_workflow_definition(
        {
            "steps": [
                {
                    "id": "wait",
                    "name": "Wait for BMH",
                    "type": "loop",
                    "max_iterations": 5,
                    "required_role": "eph",
                }
            ]
        }
    )
    assert definition["steps"][0]["max_iterations"] == 5

    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [{"id": "wait", "name": "Wait", "type": "loop", "max_iterations": 1}],
            }
        )
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [{"id": "wait", "name": "Wait", "type": "loop", "max_iterations": 99}],
            }
        )
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {"steps": [{"id": "task", "name": "Task", "max_iterations": 2}]}
        )


def test_workflow_condition_steps_and_branch_when_guards():
    definition = normalize_workflow_definition(
        {
            "steps": [
                {"id": "inspect", "name": "Inspect", "required_role": "eph"},
                {
                    "id": "check",
                    "name": "Check readiness",
                    "type": "condition",
                    "depends_on": ["inspect"],
                    "condition": {
                        "source": "inspect",
                        "field": "readiness.ready",
                        "equals": True,
                    },
                },
                {
                    "id": "apply",
                    "name": "Apply",
                    "depends_on": ["check"],
                    "when": {"check": True},
                },
                {
                    "id": "skip",
                    "name": "Skip apply",
                    "depends_on": ["check"],
                    "when": {"check": False},
                },
            ]
        }
    )
    by_id = {step["id"]: step for step in definition["steps"]}
    assert by_id["check"]["condition"] == {
        "source": "inspect",
        "field": "readiness.ready",
        "equals": True,
    }
    assert by_id["apply"]["when"] == {"check": True}
    assert by_id["check"]["required_role"] is None


def test_workflow_condition_contract_rejects_invalid_branches():
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [
                    {"id": "a", "name": "A"},
                    {
                        "id": "b",
                        "name": "B",
                        "depends_on": ["a"],
                        "when": {"a": True},
                    },
                ],
            }
        )
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [
                    {
                        "id": "check",
                        "name": "Check",
                        "type": "condition",
                        "condition": {"field": "ready"},
                    }
                ],
            }
        )
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [
                    {"id": "a", "name": "A"},
                    {
                        "id": "check",
                        "name": "Check",
                        "type": "condition",
                        "condition": {
                            "source": "a",
                            "field": "ready",
                            "equals": True,
                        },
                    },
                ],
            }
        )
    with pytest.raises(WorkspaceError):
        normalize_workflow_definition(
            {
                "steps": [
                    {
                        "id": "check",
                        "name": "Check",
                        "type": "condition",
                        "condition": {"field": "ready", "equals": {"nested": 1}},
                    }
                ],
            }
        )


def test_workflow_run_deadline_defaults_and_bounds():
    run = normalize_workflow_run(input={}, idempotency_key="run-default")
    assert run["deadline_seconds"] == 86400
    for value in (59, 604801, True, "3600"):
        with pytest.raises(WorkspaceError):
            normalize_workflow_run(
                input={},
                idempotency_key=f"run-{value}",
                deadline_seconds=value,
            )


def test_work_retry_budget_is_bounded():
    work = normalize_work(
        kind="inspection",
        payload={},
        idempotency_key="work-budget",
        max_attempts=2,
    )
    assert work["max_attempts"] == 2

    for invalid in (0, 11, True, "3"):
        with pytest.raises(WorkspaceError, match="max_attempts"):
            normalize_work(
                kind="inspection",
                payload={},
                idempotency_key="work-budget-invalid",
                max_attempts=invalid,
            )
