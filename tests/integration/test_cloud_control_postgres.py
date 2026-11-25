"""End-to-end Cloud Control workspace tests against real PostgreSQL and mTLS.

These tests are skipped unless ``CLOUD_CONTROL_TEST_DATABASE_URL`` points at a
disposable PostgreSQL database. They create the production schema through the
same ``initialize_store`` function the service uses, start the real HTTP handler
behind a real TLS listener with client-certificate verification, and drive the
project, task and workflow APIs through httpx.
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import ssl
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from apps.cloud_control_agent.api import create_handler
from apps.cloud_control_agent.main import initialize_store

DATABASE_ENV = "CLOUD_CONTROL_TEST_DATABASE_URL"


def _database_url() -> str:
    value = os.environ.get(DATABASE_ENV, "").strip()
    if not value:
        pytest.skip(f"{DATABASE_ENV} is not configured")
    return value


def _openssl(*arguments: str) -> None:
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    subprocess.run(
        ["openssl", *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _issue_certificate(
    directory: Path,
    name: str,
    common_name: str,
    ca_key: Path,
    ca_crt: Path,
) -> tuple[Path, Path]:
    key = directory / f"{name}.key"
    csr = directory / f"{name}.csr"
    crt = directory / f"{name}.crt"
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(csr),
        "-subj",
        f"/CN={common_name}",
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        str(csr),
        "-CA",
        str(ca_crt),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(crt),
        "-days",
        "1",
    )
    return crt, key


@pytest.fixture(scope="module")
def tls_material(tmp_path_factory):
    directory = tmp_path_factory.mktemp("cloud-control-tls")
    ca_key = directory / "ca.key"
    ca_crt = directory / "ca.crt"
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca_crt),
        "-subj",
        "/CN=Cloud Control Test CA",
        "-days",
        "1",
    )
    server_key = directory / "server.key"
    server_csr = directory / "server.csr"
    server_crt = directory / "server.crt"
    extensions = directory / "server.ext"
    extensions.write_text("subjectAltName=IP:127.0.0.1,DNS:localhost\n", encoding="utf-8")
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(server_key),
        "-out",
        str(server_csr),
        "-subj",
        "/CN=localhost",
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        str(server_csr),
        "-CA",
        str(ca_crt),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(server_crt),
        "-days",
        "1",
        "-extfile",
        str(extensions),
    )
    platform = _issue_certificate(directory, "platform", "platform-api", ca_key, ca_crt)
    return {
        "directory": directory,
        "ca": ca_crt,
        "server": (server_crt, server_key),
        "platform": platform,
        "issue_agent": lambda agent_id: _issue_certificate(
            directory, f"agent-{agent_id}", str(agent_id), ca_key, ca_crt
        ),
    }


@pytest.fixture(scope="module")
def control_service():
    database_url = _database_url()
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
        initialize_store(connection)
    return database_url


@pytest.fixture(scope="module")
def control_server(control_service, tls_material):
    handler = create_handler(control_service)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(tls_material["server"][0]), str(tls_material["server"][1]))
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(str(tls_material["ca"]))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _identity(agent_id: UUID, installation_id: UUID, role: str = "eph") -> dict:
    return {
        "agent_id": str(agent_id),
        "role": role,
        "installation_id": str(installation_id),
        "cluster_id": None,
        "instance_id": f"integration-{str(agent_id)[:8]}",
    }


def _tls_client(base_url: str, tls, certificate) -> httpx.Client:
    context = ssl.create_default_context(cafile=str(tls["ca"]))
    context.load_cert_chain(str(certificate[0]), str(certificate[1]))
    return httpx.Client(
        base_url=base_url,
        verify=context,
        timeout=10.0,
    )


def _post(client: httpx.Client, path: str, payload: dict) -> dict:
    response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    document = response.json()
    assert isinstance(document, dict)
    return document


def _get(client: httpx.Client, path: str) -> dict:
    response = client.get(path)
    assert response.status_code == 200, response.text
    document = response.json()
    assert isinstance(document, dict)
    return document


def _register_agent(
    platform_client,
    agent_id: UUID,
    installation_id: UUID,
    role: str = "eph",
) -> None:
    _post(
        platform_client,
        "/api/v1/control/agents/register",
        {
            "agent_id": str(agent_id),
            "installation_id": str(installation_id),
            "role": role,
            "cluster_id": str(uuid4()) if role == "target" else None,
        },
    )


def _create_project(platform_client, installation_id: UUID) -> UUID:
    project_id = uuid4()
    _post(
        platform_client,
        "/api/v1/control/projects",
        {
            "project_id": str(project_id),
            "installation_id": str(installation_id),
            "name": f"lab-{project_id.hex[:8]}",
            "description": "integration project",
        },
    )
    return project_id


def _create_skill(platform_client, project_id: UUID) -> None:
    _post(
        platform_client,
        f"/api/v1/control/projects/{project_id}/skills",
        {
            "skill_id": str(uuid4()),
            "name": "metal3-prep",
            "version": "1.0.0",
            "manifest": {
                "entrypoint": "skills.metal3.prepare",
                "actions": ["bmh.apply", "bmh.wait_ready"],
            },
        },
    )


def _create_plugin(platform_client, project_id: UUID) -> None:
    _post(
        platform_client,
        f"/api/v1/control/projects/{project_id}/plugins",
        {
            "plugin_id": str(uuid4()),
            "name": "vault",
            "version": "2.0.0",
            "kind": "connector",
            "config_ref": "secret://tenants/lab/vault",
        },
    )


def _create_workflow(platform_client, project_id: UUID) -> UUID:
    workflow_id = uuid4()
    _post(
        platform_client,
        f"/api/v1/control/projects/{project_id}/workflows",
        {
            "workflow_id": str(workflow_id),
            "name": "provision-lab",
            "definition": {
                "description": "Prepare then verify",
                "steps": [
                    {"id": "prepare", "name": "Prepare hosts", "required_role": "eph"},
                    {
                        "id": "verify",
                        "name": "Verify hosts",
                        "depends_on": ["prepare"],
                        "payload": {"checks": ["bmh", "k8s"]},
                    },
                ],
            },
        },
    )
    return workflow_id


def _claim(client: httpx.Client, identity: dict) -> dict:
    document = _post(client, "/api/v1/agent-control/tasks/claim", identity)
    task = document.get("task")
    assert isinstance(task, dict), document
    return document


def test_project_workspace_and_workflow_run_against_postgres(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        _create_skill(platform_client, project_id)
        _create_plugin(platform_client, project_id)

        project = _get(platform_client, f"/api/v1/control/projects/{project_id}")
        assert project["skill_count"] == 1
        assert project["plugin_count"] == 1

        workflow_id = _create_workflow(platform_client, project_id)
        run_id = uuid4()
        started = _post(
            platform_client,
            f"/api/v1/control/workflows/{workflow_id}/runs",
            {
                "run_id": str(run_id),
                "input": {"cluster": "edge-01"},
                "idempotency_key": f"run-{run_id}",
            },
        )
        assert started["run"]["status"] == "running"
        by_step = {task["step_id"]: task for task in started["tasks"]}
        assert by_step["prepare"]["status"] == "queued"
        assert by_step["verify"]["status"] == "blocked"

        agent_id = uuid4()
        _register_agent(platform_client, agent_id, installation_id)
        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        try:
            identity = _identity(agent_id, installation_id)
            first = _claim(agent_client, identity)
            assert first["task"]["step_id"] == "prepare"
            assert first["skills"][0]["name"] == "metal3-prep"
            assert first["plugins"][0]["config_ref"] == "secret://tenants/lab/vault"

            result = _post(
                agent_client,
                "/api/v1/agent-control/tasks/result",
                {
                    **identity,
                    "task_id": first["task"]["task_id"],
                    "lease_token": first["lease_token"],
                    "status": "succeeded",
                    "result": {"hosts": 3},
                },
            )
            assert result["workflow_run_status"] == "running"

            second = _claim(agent_client, identity)
            assert second["task"]["step_id"] == "verify"
            _post(
                agent_client,
                "/api/v1/agent-control/tasks/result",
                {
                    **identity,
                    "task_id": second["task"]["task_id"],
                    "lease_token": second["lease_token"],
                    "status": "succeeded",
                    "result": {"ready": True},
                },
            )
        finally:
            agent_client.close()

        run = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
        assert run["run"]["status"] == "succeeded"
        assert run["run"]["output"] == {
            "prepare": {"hosts": 3},
            "verify": {"ready": True},
        }
    finally:
        platform_client.close()


def test_workflow_approval_step_pauses_and_resumes_the_run(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        workflow_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/workflows",
            {
                "workflow_id": str(workflow_id),
                "name": "approval-workflow",
                "definition": {
                    "steps": [
                        {"id": "prepare", "name": "Prepare", "required_role": "eph"},
                        {
                            "id": "review",
                            "name": "Review plan",
                            "type": "approval",
                            "depends_on": ["prepare"],
                        },
                        {
                            "id": "apply",
                            "name": "Apply",
                            "depends_on": ["review"],
                        },
                    ]
                },
            },
        )
        run_id = uuid4()
        started = _post(
            platform_client,
            f"/api/v1/control/workflows/{workflow_id}/runs",
            {
                "run_id": str(run_id),
                "input": {"cluster": "edge-02"},
                "idempotency_key": f"approval-run-{run_id}",
            },
        )
        by_step = {task["step_id"]: task for task in started["tasks"]}
        assert by_step["prepare"]["status"] == "queued"
        assert by_step["review"]["status"] == "blocked"
        assert by_step["apply"]["status"] == "blocked"

        agent_id = uuid4()
        _register_agent(platform_client, agent_id, installation_id)
        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        try:
            identity = _identity(agent_id, installation_id)
            first = _claim(agent_client, identity)
            assert first["task"]["step_id"] == "prepare"
            _post(
                agent_client,
                "/api/v1/agent-control/tasks/result",
                {
                    **identity,
                    "task_id": first["task"]["task_id"],
                    "lease_token": first["lease_token"],
                    "status": "succeeded",
                    "result": {"hosts": 2},
                },
            )
            paused = _post(agent_client, "/api/v1/agent-control/tasks/claim", identity)
            assert paused["task"] is None
        finally:
            agent_client.close()

        paused_run = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
        by_step = {task["step_id"]: task for task in paused_run["tasks"]}
        assert by_step["review"]["status"] == "awaiting_approval"
        assert by_step["review"]["step_type"] == "approval"
        assert by_step["apply"]["status"] == "blocked"
        assert paused_run["run"]["status"] == "running"

        approved = _post(
            platform_client,
            f"/api/v1/control/workflow-runs/{run_id}/steps/review/approve",
            {"decided_by": "bob"},
        )
        assert approved["workflow_run_status"] == "running"
        assert approved["task"]["status"] == "succeeded"
        assert approved["task"]["result"]["decision"] == "approved"

        resumed_run = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
        by_step = {task["step_id"]: task for task in resumed_run["tasks"]}
        assert by_step["apply"]["status"] == "queued"

        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        try:
            identity = _identity(agent_id, installation_id)
            second = _claim(agent_client, identity)
            assert second["task"]["step_id"] == "apply"
            _post(
                agent_client,
                "/api/v1/agent-control/tasks/result",
                {
                    **identity,
                    "task_id": second["task"]["task_id"],
                    "lease_token": second["lease_token"],
                    "status": "succeeded",
                    "result": {"applied": True},
                },
            )
        finally:
            agent_client.close()

        finished = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
        assert finished["run"]["status"] == "succeeded"
        assert finished["run"]["output"]["review"]["decision"] == "approved"
        assert finished["run"]["output"]["apply"] == {"applied": True}
    finally:
        platform_client.close()


def test_workflow_run_deadline_is_reconciled_into_a_failed_run(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        workflow_id = _create_workflow(platform_client, project_id)
        run_id = uuid4()
        started = _post(
            platform_client,
            f"/api/v1/control/workflows/{workflow_id}/runs",
            {
                "run_id": str(run_id),
                "input": {"cluster": "edge-deadline"},
                "idempotency_key": f"deadline-run-{run_id}",
                "deadline_seconds": 60,
            },
        )
        assert started["run"]["deadline_at"] is not None
        assert started["run"]["status"] == "running"

        database_url = os.environ["CLOUD_CONTROL_TEST_DATABASE_URL"]
        with psycopg.connect(database_url) as connection:
            connection.execute(
                """
                UPDATE agent_control_workflow_runs
                SET deadline_at = NOW() - INTERVAL '1 second'
                WHERE run_id = %s
                """,
                (run_id,),
            )
        reconciled = _post(platform_client, "/api/v1/control/workflows/reconcile", {"limit": 10})
        assert reconciled["status"] == "ok"
        assert reconciled["expired"] >= 1

        finished = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
        assert finished["run"]["status"] == "failed"
        assert finished["run"]["output"]["error"] == "WORKFLOW_DEADLINE_EXCEEDED"
        assert all(task["status"] == "cancelled" for task in finished["tasks"])
    finally:
        platform_client.close()


def test_workflow_condition_routes_only_the_matching_branch(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        workflow_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/workflows",
            {
                "workflow_id": str(workflow_id),
                "name": "conditional-workflow",
                "definition": {
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
                },
            },
        )
        run_id = uuid4()
        started = _post(
            platform_client,
            f"/api/v1/control/workflows/{workflow_id}/runs",
            {
                "run_id": str(run_id),
                "input": {},
                "idempotency_key": f"condition-run-{run_id}",
            },
        )
        by_step = {task["step_id"]: task for task in started["tasks"]}
        assert by_step["inspect"]["status"] == "queued"
        assert by_step["check"]["status"] == "blocked"
        assert by_step["apply"]["status"] == "blocked"
        assert by_step["skip"]["status"] == "blocked"

        agent_id = uuid4()
        _register_agent(platform_client, agent_id, installation_id)
        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        try:
            identity = _identity(agent_id, installation_id)
            first = _claim(agent_client, identity)
            assert first["task"]["step_id"] == "inspect"
            _post(
                agent_client,
                "/api/v1/agent-control/tasks/result",
                {
                    **identity,
                    "task_id": first["task"]["task_id"],
                    "lease_token": first["lease_token"],
                    "status": "succeeded",
                    "result": {"readiness": {"ready": False}},
                },
            )
            routed = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
            by_step = {task["step_id"]: task for task in routed["tasks"]}
            assert by_step["check"]["status"] == "succeeded"
            assert by_step["check"]["result"]["matched"] is False
            assert by_step["apply"]["status"] == "cancelled"
            assert by_step["skip"]["status"] == "queued"

            second = _claim(agent_client, identity)
            assert second["task"]["step_id"] == "skip"
            _post(
                agent_client,
                "/api/v1/agent-control/tasks/result",
                {
                    **identity,
                    "task_id": second["task"]["task_id"],
                    "lease_token": second["lease_token"],
                    "status": "succeeded",
                    "result": {"skipped": True},
                },
            )
        finally:
            agent_client.close()

        finished = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
        assert finished["run"]["status"] == "succeeded"
        assert finished["run"]["output"]["check"]["matched"] is False
        assert finished["run"]["output"]["skip"] == {"skipped": True}
        assert "apply" not in finished["run"]["output"]
    finally:
        platform_client.close()


def test_workflow_loop_step_requeues_until_its_iteration_budget(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        workflow_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/workflows",
            {
                "workflow_id": str(workflow_id),
                "name": "loop-workflow",
                "definition": {
                    "steps": [
                        {
                            "id": "wait",
                            "name": "Wait for BMH",
                            "type": "loop",
                            "max_iterations": 2,
                            "required_role": "eph",
                        }
                    ]
                },
            },
        )
        run_id = uuid4()
        started = _post(
            platform_client,
            f"/api/v1/control/workflows/{workflow_id}/runs",
            {
                "run_id": str(run_id),
                "input": {"host": "bmh-01"},
                "idempotency_key": f"loop-run-{run_id}",
            },
        )
        task = started["tasks"][0]
        assert task["step_type"] == "loop"
        assert task["iteration"] == 1
        assert task["loop_max_iterations"] == 2

        agent_id = uuid4()
        _register_agent(platform_client, agent_id, installation_id)
        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        try:
            identity = _identity(agent_id, installation_id)
            first = _claim(agent_client, identity)
            assert first["task"]["step_id"] == "wait"
            assert first["task"]["iteration"] == 1
            first_result = _post(
                agent_client,
                "/api/v1/agent-control/tasks/result",
                {
                    **identity,
                    "task_id": first["task"]["task_id"],
                    "lease_token": first["lease_token"],
                    "status": "succeeded",
                    "result": {"continue": True, "ready": False},
                },
            )
            assert first_result["looping"] is True
            assert first_result["iteration"] == 2
            assert first_result["workflow_run_status"] == "running"

            midway = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
            task = midway["tasks"][0]
            assert task["status"] == "queued"
            assert task["iteration"] == 2
            assert midway["run"]["status"] == "running"

            second = _claim(agent_client, identity)
            assert second["task"]["iteration"] == 2
            second_result = _post(
                agent_client,
                "/api/v1/agent-control/tasks/result",
                {
                    **identity,
                    "task_id": second["task"]["task_id"],
                    "lease_token": second["lease_token"],
                    "status": "succeeded",
                    "result": {"continue": True, "ready": True},
                },
            )
            assert second_result["looping"] is False
            assert second_result["status"] == "succeeded"
            assert second_result["workflow_run_status"] == "succeeded"
        finally:
            agent_client.close()

        finished = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
        assert finished["run"]["status"] == "succeeded"
        assert finished["run"]["output"]["wait"]["continue"] is True
        assert finished["tasks"][0]["iteration"] == 2
    finally:
        platform_client.close()


def test_workflow_any_join_releases_downstream_before_all_branches_finish(
    control_server, tls_material
):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        workflow_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/workflows",
            {
                "workflow_id": str(workflow_id),
                "name": "parallel-any-join",
                "definition": {
                    "steps": [
                        {"id": "left", "name": "Left", "required_role": "eph"},
                        {"id": "right", "name": "Right", "required_role": "eph"},
                        {
                            "id": "merge",
                            "name": "Merge",
                            "depends_on": ["left", "right"],
                            "join": "any",
                        },
                    ]
                },
            },
        )
        run_id = uuid4()
        started = _post(
            platform_client,
            f"/api/v1/control/workflows/{workflow_id}/runs",
            {
                "run_id": str(run_id),
                "input": {},
                "idempotency_key": f"any-join-{run_id}",
            },
        )
        by_step = {task["step_id"]: task for task in started["tasks"]}
        assert by_step["left"]["status"] == "queued"
        assert by_step["right"]["status"] == "queued"
        assert by_step["merge"]["status"] == "blocked"
        assert by_step["merge"]["join_policy"] == "any"

        agent_id = uuid4()
        _register_agent(platform_client, agent_id, installation_id)
        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        executed = []
        try:
            identity = _identity(agent_id, installation_id)
            first = _claim(agent_client, identity)
            assert first["task"]["step_id"] == "left"
            _post(
                agent_client,
                "/api/v1/agent-control/tasks/result",
                {
                    **identity,
                    "task_id": first["task"]["task_id"],
                    "lease_token": first["lease_token"],
                    "status": "succeeded",
                    "result": {"branch": "left"},
                },
            )
            executed.append("left")
            partial = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
            by_step = {task["step_id"]: task for task in partial["tasks"]}
            assert by_step["merge"]["status"] == "queued"
            assert by_step["right"]["status"] == "queued"
            assert partial["run"]["status"] == "running"

            for _ in range(3):
                claimed = _post(agent_client, "/api/v1/agent-control/tasks/claim", identity)
                task = claimed.get("task")
                if task is None:
                    break
                _post(
                    agent_client,
                    "/api/v1/agent-control/tasks/result",
                    {
                        **identity,
                        "task_id": task["task_id"],
                        "lease_token": claimed["lease_token"],
                        "status": "succeeded",
                        "result": {"branch": task["step_id"]},
                    },
                )
                executed.append(task["step_id"])
        finally:
            agent_client.close()
        assert set(executed) == {"left", "right", "merge"}
        finished = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
        assert finished["run"]["status"] == "succeeded"
        assert finished["run"]["output"]["merge"] == {"branch": "merge"}
    finally:
        platform_client.close()


def test_workflow_approval_rejection_fails_the_run(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        workflow_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/workflows",
            {
                "workflow_id": str(workflow_id),
                "name": "approval-reject",
                "definition": {
                    "steps": [
                        {"id": "review", "name": "Review", "type": "approval"},
                        {"id": "apply", "name": "Apply", "depends_on": ["review"]},
                    ]
                },
            },
        )
        run_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/workflows/{workflow_id}/runs",
            {
                "run_id": str(run_id),
                "input": {},
                "idempotency_key": f"reject-run-{run_id}",
            },
        )
        rejected = _post(
            platform_client,
            f"/api/v1/control/workflow-runs/{run_id}/steps/review/reject",
            {"decided_by": "bob", "reason": "host inventory is incomplete"},
        )
        assert rejected["workflow_run_status"] == "failed"
        assert rejected["task"]["status"] == "failed"
        assert rejected["task"]["result"]["reason"] == "host inventory is incomplete"
        run = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
        assert run["run"]["status"] == "failed"
        assert run["run"]["output"]["failed_step"] == "review"
        by_step = {task["step_id"]: task for task in run["tasks"]}
        assert by_step["apply"]["status"] == "cancelled"
    finally:
        platform_client.close()


def test_concurrent_task_claims_do_not_overlap(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        workflow_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/workflows",
            {
                "workflow_id": str(workflow_id),
                "name": "parallel-inspection",
                "definition": {
                    "steps": [
                        {"id": "node-a", "name": "Inspect A"},
                        {"id": "node-b", "name": "Inspect B"},
                    ],
                },
            },
        )
        run_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/workflows/{workflow_id}/runs",
            {
                "run_id": str(run_id),
                "input": {},
                "idempotency_key": f"run-{run_id}",
            },
        )

        agents = [uuid4(), uuid4()]
        for agent_id in agents:
            _register_agent(platform_client, agent_id, installation_id)
        clients = [
            _tls_client(control_server, tls_material, tls_material["issue_agent"](agent_id))
            for agent_id in agents
        ]
        identities = [_identity(agent_id, installation_id) for agent_id in agents]
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                claims = list(executor.map(_claim, clients, identities))
            task_ids = {claim["task"]["task_id"] for claim in claims}
            assert len(task_ids) == 2
            steps = {claim["task"]["step_id"] for claim in claims}
            assert steps == {"node-a", "node-b"}
        finally:
            for client in clients:
                client.close()
    finally:
        platform_client.close()


def test_task_lease_is_bound_to_the_claiming_agent(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        task_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/tasks",
            {
                "task_id": str(task_id),
                "name": "NTP check",
                "payload": {"hosts": ["10.0.0.1"]},
                "required_role": "eph",
                "priority": 4,
                "idempotency_key": f"task-{task_id}",
            },
        )

        first_agent, second_agent = uuid4(), uuid4()
        _register_agent(platform_client, first_agent, installation_id)
        first_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](first_agent)
        )
        second_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](second_agent)
        )
        try:
            identity = _identity(first_agent, installation_id)
            claimed = _claim(first_client, identity)
            assert claimed["task"]["task_id"] == str(task_id)
            response = second_client.post(
                "/api/v1/agent-control/tasks/result",
                json={
                    **_identity(second_agent, installation_id),
                    "task_id": str(task_id),
                    "lease_token": claimed["lease_token"],
                    "status": "succeeded",
                    "result": {},
                },
            )
            assert response.status_code == 400
            assert response.json()["error"] == "AGENT_NOT_REGISTERED"

            renewed = _post(
                first_client,
                "/api/v1/agent-control/tasks/lease/renew",
                {
                    **identity,
                    "task_id": str(task_id),
                    "lease_token": claimed["lease_token"],
                },
            )
            assert renewed["renewed"] is True
        finally:
            first_client.close()
            second_client.close()
    finally:
        platform_client.close()


def test_work_item_claim_and_result_round_trip(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        work_id = uuid4()
        created = _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/work",
            {
                "work_id": str(work_id),
                "kind": "inspection",
                "priority": 4,
                "required_role": "eph",
                "payload": {"cluster": "edge-02"},
                "idempotency_key": f"work-{work_id}",
            },
        )
        assert created["status"] == "queued"

        agent_id = uuid4()
        _register_agent(platform_client, agent_id, installation_id)
        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        try:
            identity = _identity(agent_id, installation_id)
            claimed = _post(agent_client, "/api/v1/agent-control/work/claim", identity)
            assert claimed["work"]["work_id"] == str(work_id)
            assert claimed["work"]["kind"] == "inspection"
            assert claimed["lease_seconds"] == 90

            rejected = agent_client.post(
                "/api/v1/agent-control/work/result",
                json={
                    **identity,
                    "work_id": str(work_id),
                    "lease_token": "stale-lease-token",
                    "status": "succeeded",
                    "result": {"findings": 2},
                },
            )
            assert rejected.status_code == 400, rejected.text
            assert "WORK_RESULT_NOT_ACCEPTED" in rejected.text

            _post(
                agent_client,
                "/api/v1/agent-control/work/result",
                {
                    **identity,
                    "work_id": str(work_id),
                    "lease_token": claimed["lease_token"],
                    "status": "succeeded",
                    "result": {"findings": 2},
                },
            )
        finally:
            agent_client.close()

        work = _get(platform_client, f"/api/v1/control/work/{work_id}")
        assert work["status"] == "succeeded"
        assert work["result"] == {"findings": 2}
        assert work["assigned_agent_id"] == str(agent_id)
    finally:
        platform_client.close()


def test_work_claim_filters_by_kind_for_software_tasks(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        inspection_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/work",
            {
                "work_id": str(inspection_id),
                "kind": "inspection",
                "priority": 9,
                "required_role": "eph",
                "payload": {"cluster": "edge-01"},
                "idempotency_key": f"inspection-{inspection_id}",
            },
        )
        software_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/work",
            {
                "work_id": str(software_id),
                "kind": "software",
                "priority": 1,
                "required_role": "eph",
                "payload": {"action": "review", "paths": ["apps/dev_agent/work.py"]},
                "idempotency_key": f"software-{software_id}",
            },
        )

        agent_id = uuid4()
        _register_agent(platform_client, agent_id, installation_id)
        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        try:
            identity = _identity(agent_id, installation_id)
            claimed = _post(
                agent_client,
                "/api/v1/agent-control/work/claim",
                {**identity, "kind": "software"},
            )
            assert claimed["work"]["work_id"] == str(software_id)
            assert claimed["work"]["kind"] == "software"
            _post(
                agent_client,
                "/api/v1/agent-control/work/result",
                {
                    **identity,
                    "work_id": str(software_id),
                    "lease_token": claimed["lease_token"],
                    "status": "succeeded",
                    "result": {"count": 0},
                },
            )
            remaining = _post(agent_client, "/api/v1/agent-control/work/claim", identity)
            assert remaining["work"]["work_id"] == str(inspection_id)
        finally:
            agent_client.close()
    finally:
        platform_client.close()


def test_control_plane_approval_is_single_approver_and_redeemed_once(control_server, tls_material):
    import hashlib
    import json

    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        work_id = uuid4()
        edits = [{"path": "pkg/cli.py", "old": "a", "new": "b"}]
        digest = hashlib.sha256(
            json.dumps(edits, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/work",
            {
                "work_id": str(work_id),
                "kind": "software",
                "priority": 2,
                "required_role": "eph",
                "payload": {"action": "apply_patch", "edits": edits},
                "idempotency_key": f"approval-work-{work_id}",
            },
        )
        approval_id = uuid4()
        created = _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/approvals",
            {
                "approval_id": str(approval_id),
                "work_id": str(work_id),
                "action": "apply-patch",
                "payload_digest": digest,
                "requested_by": "alice",
                "ttl_seconds": 600,
            },
        )
        assert created["status"] == "pending"
        approved = _post(
            platform_client,
            f"/api/v1/control/approvals/{approval_id}/approve",
            {"approved_by": "bob"},
        )
        assert approved["status"] == "approved"
        assert approved["approved_by"] == "bob"

        agent_id = uuid4()
        _register_agent(platform_client, agent_id, installation_id)
        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        try:
            identity = _identity(agent_id, installation_id)
            claimed = _post(
                agent_client,
                "/api/v1/agent-control/work/claim",
                {**identity, "kind": "software"},
            )
            assert claimed["work"]["work_id"] == str(work_id)
            redeem_body = {
                **identity,
                "work_id": str(work_id),
                "approval_id": str(approval_id),
                "action": "apply-patch",
                "payload_digest": digest,
            }
            redeemed = _post(agent_client, "/api/v1/agent-control/approvals/redeem", redeem_body)
            assert redeemed["redeemed"] is True
            again = agent_client.post("/api/v1/agent-control/approvals/redeem", json=redeem_body)
            assert again.status_code == 400
        finally:
            agent_client.close()
        approval = _get(platform_client, f"/api/v1/control/approvals/{approval_id}")
        assert approval["status"] == "used"
    finally:
        platform_client.close()


def test_failed_step_retries_until_its_attempt_budget_is_exhausted(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        workflow_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/workflows",
            {
                "workflow_id": str(workflow_id),
                "name": "retry-workflow",
                "definition": {
                    "steps": [
                        {
                            "id": "prepare",
                            "name": "Prepare hosts",
                            "max_attempts": 2,
                            "payload": {"cluster": "edge-03"},
                        }
                    ],
                },
                "idempotency_key": f"workflow-{workflow_id}",
            },
        )
        run_id = uuid4()
        started = _post(
            platform_client,
            f"/api/v1/control/workflows/{workflow_id}/runs",
            {"run_id": str(run_id), "input": {}, "idempotency_key": f"run-{run_id}"},
        )
        assert started["tasks"][0]["status"] == "queued"

        agent_id = uuid4()
        _register_agent(platform_client, agent_id, installation_id)
        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        try:
            identity = _identity(agent_id, installation_id)
            first = _claim(agent_client, identity)
            failed = _post(
                agent_client,
                "/api/v1/agent-control/tasks/result",
                {
                    **identity,
                    "task_id": first["task"]["task_id"],
                    "lease_token": first["lease_token"],
                    "status": "failed",
                    "result": {"error": "transient"},
                },
            )
            assert failed["retrying"] is True
            assert failed["status"] == "queued"
            assert failed["workflow_run_status"] == "running"

            second = _claim(agent_client, identity)
            assert second["task"]["task_id"] == first["task"]["task_id"]
            done = _post(
                agent_client,
                "/api/v1/agent-control/tasks/result",
                {
                    **identity,
                    "task_id": second["task"]["task_id"],
                    "lease_token": second["lease_token"],
                    "status": "succeeded",
                    "result": {"prepared": True},
                },
            )
            assert done["workflow_run_status"] == "succeeded"
            assert done["retrying"] is False
        finally:
            agent_client.close()

        run = _get(platform_client, f"/api/v1/control/workflow-runs/{run_id}")
        assert run["run"]["status"] == "succeeded"
    finally:
        platform_client.close()


def test_deployment_action_result_returns_the_latest_eph_preflight(control_server, tls_material):
    platform_client = _tls_client(
        control_server,
        tls_material,
        tls_material["platform"],
    )
    installation_id = uuid4()
    agent_id = uuid4()
    deployment_id = uuid4()
    try:
        _register_agent(platform_client, agent_id, installation_id)
        _post(
            platform_client,
            "/api/v1/control/deployments",
            {
                "deployment_id": str(deployment_id),
                "installation_id": str(installation_id),
                "cluster_id": str(uuid4()),
                "spec": {"cluster_name": "preflight-lab"},
            },
        )
        missing = _get(
            platform_client,
            f"/api/v1/control/deployments/{deployment_id}/actions/eph.preflight/result",
        )
        assert missing["available"] is False
        # The unified action channel names the gap generically.
        assert missing["reason"] == "DEPLOYMENT_ACTION_NOT_RUN"
        with psycopg.connect(_database_url()) as connection:
            connection.execute(
                """
                INSERT INTO agent_control_commands (
                    command_id, deployment_id, target_agent_id, required_role,
                    action, idempotency_key, payload, status, result
                )
                VALUES (%s, %s, %s, 'eph', 'eph.preflight', %s, '{}'::jsonb,
                        'succeeded', %s)
                """,
                (
                    uuid4(),
                    deployment_id,
                    agent_id,
                    f"preflight-{uuid4()}",
                    Jsonb(
                        {
                            "output": {
                                "eph_ready": False,
                                "warnings": ["IRONIC_IMAGE_REQUIRED"],
                                "observed_at": "2026-11-20T01:02:03+00:00",
                            }
                        }
                    ),
                ),
            )
            connection.commit()
        synced = _get(
            platform_client,
            f"/api/v1/control/deployments/{deployment_id}/actions/eph.preflight/result",
        )
        assert synced["available"] is True
        assert synced["status"] == "succeeded"
        assert synced["output"]["warnings"] == ["IRONIC_IMAGE_REQUIRED"]
        assert synced["output"]["eph_ready"] is False
    finally:
        platform_client.close()


@pytest.mark.integration
def test_deployment_action_result_returns_latest_inspection(control_server, tls_material):
    platform_client = _tls_client(
        control_server,
        tls_material,
        tls_material["platform"],
    )
    installation_id = uuid4()
    agent_id = uuid4()
    deployment_id = uuid4()
    try:
        _register_agent(platform_client, agent_id, installation_id)
        _post(
            platform_client,
            "/api/v1/control/deployments",
            {
                "deployment_id": str(deployment_id),
                "installation_id": str(installation_id),
                "cluster_id": str(uuid4()),
                "spec": {"cluster_name": "bmh-sync-lab"},
            },
        )
        missing = _get(
            platform_client,
            f"/api/v1/control/deployments/{deployment_id}/actions/metal3.host.inspect/result",
        )
        assert missing["available"] is False
        assert missing["reason"] == "DEPLOYMENT_ACTION_NOT_RUN"
        with psycopg.connect(_database_url()) as connection:
            connection.execute(
                """
                INSERT INTO agent_control_commands (
                    command_id, deployment_id, target_agent_id, required_role,
                    action, idempotency_key, payload, status, result
                )
                VALUES (%s, %s, %s, 'eph', 'metal3.host.inspect', %s, '{}'::jsonb,
                        'succeeded', %s)
                """,
                (
                    uuid4(),
                    deployment_id,
                    agent_id,
                    f"inspect-{uuid4()}",
                    Jsonb(
                        {
                            "output": {
                                "host_count": 1,
                                "hosts": [
                                    {
                                        "bmh_name": "bmh-1",
                                        "bmc_host": "10.0.0.11",
                                        "pci_addresses": [],
                                    }
                                ],
                                "warnings": ["BMH_PCI_ADDRESS_UNAVAILABLE"],
                                "observed_at": "2026-11-21T01:02:03+00:00",
                            }
                        }
                    ),
                ),
            )
            connection.commit()
        synced = _get(
            platform_client,
            f"/api/v1/control/deployments/{deployment_id}/actions/metal3.host.inspect/result",
        )
        assert synced["available"] is True
        assert synced["status"] == "succeeded"
        assert synced["output"]["host_count"] == 1
        assert synced["output"]["warnings"] == ["BMH_PCI_ADDRESS_UNAVAILABLE"]
    finally:
        platform_client.close()


def test_exhausted_expired_work_lease_is_failed_instead_of_reclaimed(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        work_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/work",
            {
                "work_id": str(work_id),
                "kind": "inspection",
                "priority": 1,
                "payload": {"cluster": "edge-03"},
                "max_attempts": 1,
                "idempotency_key": f"work-{work_id}",
            },
        )
        agent_id = uuid4()
        _register_agent(platform_client, agent_id, installation_id)
        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        try:
            identity = _identity(agent_id, installation_id)
            claimed = _post(agent_client, "/api/v1/agent-control/work/claim", identity)
            assert claimed["work"]["work_id"] == str(work_id)
            assert claimed["work"]["max_attempts"] == 1

            with psycopg.connect(_database_url()) as connection:
                connection.execute(
                    """
                    UPDATE agent_control_work_items
                    SET lease_expires_at = NOW() - INTERVAL '1 second'
                    WHERE work_id = %s
                    """,
                    (work_id,),
                )
            next_claim = _post(agent_client, "/api/v1/agent-control/work/claim", identity)
            assert next_claim["work"] is None
        finally:
            agent_client.close()

        work = _get(platform_client, f"/api/v1/control/work/{work_id}")
        assert work["status"] == "failed"
        assert work["result"] == {"error": "WORK_ATTEMPTS_EXHAUSTED"}
        assert work["attempts"] == 1
        assert work["max_attempts"] == 1
    finally:
        platform_client.close()


def test_exhausted_expired_task_lease_fails_without_hanging(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        task_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/tasks",
            {
                "task_id": str(task_id),
                "name": "Retry budget",
                "payload": {"max_attempts": 1, "hosts": ["10.0.0.9"]},
                "required_role": "eph",
                "idempotency_key": f"task-{task_id}",
            },
        )
        agent_id = uuid4()
        _register_agent(platform_client, agent_id, installation_id)
        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        try:
            identity = _identity(agent_id, installation_id)
            claimed = _claim(agent_client, identity)
            assert claimed["task"]["task_id"] == str(task_id)
            assert claimed["task"]["max_attempts"] == 1

            with psycopg.connect(_database_url()) as connection:
                connection.execute(
                    """
                    UPDATE agent_control_tasks
                    SET lease_expires_at = NOW() - INTERVAL '1 second'
                    WHERE task_id = %s
                    """,
                    (task_id,),
                )
            next_claim = _post(agent_client, "/api/v1/agent-control/tasks/claim", identity)
            assert next_claim["task"] is None
        finally:
            agent_client.close()

        task = _get(platform_client, f"/api/v1/control/tasks/{task_id}")
        assert task["status"] == "failed"
        assert task["result"] == {"error": "TASK_ATTEMPTS_EXHAUSTED"}
        assert task["attempts"] == 1
        assert task["max_attempts"] == 1
    finally:
        platform_client.close()


def test_standalone_task_retries_a_failed_result_within_budget(control_server, tls_material):
    installation_id = uuid4()
    platform_client = _tls_client(control_server, tls_material, tls_material["platform"])
    try:
        project_id = _create_project(platform_client, installation_id)
        task_id = uuid4()
        _post(
            platform_client,
            f"/api/v1/control/projects/{project_id}/tasks",
            {
                "task_id": str(task_id),
                "name": "Retry standalone",
                "payload": {"max_attempts": 2, "hosts": ["10.0.0.10"]},
                "required_role": "eph",
                "idempotency_key": f"task-{task_id}",
            },
        )
        agent_id = uuid4()
        _register_agent(platform_client, agent_id, installation_id)
        agent_client = _tls_client(
            control_server, tls_material, tls_material["issue_agent"](agent_id)
        )
        try:
            identity = _identity(agent_id, installation_id)
            first = _claim(agent_client, identity)
            failed = _post(
                agent_client,
                "/api/v1/agent-control/tasks/result",
                {
                    **identity,
                    "task_id": str(task_id),
                    "lease_token": first["lease_token"],
                    "status": "failed",
                    "result": {"error": "transient"},
                },
            )
            assert failed["retrying"] is True
            assert failed["status"] == "queued"
            second = _claim(agent_client, identity)
            assert second["task"]["task_id"] == str(task_id)
            assert second["task"]["max_attempts"] == 2
        finally:
            agent_client.close()
    finally:
        platform_client.close()
