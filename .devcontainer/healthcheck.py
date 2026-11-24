"""Report, from inside the Codespace, whether the console actually works.

The Codespace is reachable only through a browser session, so a deployment
cannot be confirmed from the outside. This script runs the checks locally and
publishes the result to a dedicated branch, which makes the deployment
verifiable without shell access.
"""

from __future__ import annotations

import base64
import http.cookiejar
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ".devcontainer/status/codespace.json"
BRANCH = "codespace/status"
REPO_SLUG = "Jasonzhu12397/AI-Agent-Stack"

API_HEALTH = "http://127.0.0.1:8000/health/ready"
WEB_ROOT = "http://127.0.0.1:8000/"
LOGIN_URL = "http://127.0.0.1:8000/api/v1/auth/login/"
CSRF_URL = "http://127.0.0.1:8000/api/v1/auth/csrf/"


def fetch(
    url: str,
    *,
    payload: dict | None = None,
    timeout: float = 10.0,
    accept: str = "application/json",
    opener: urllib.request.OpenerDirector | None = None,
    extra_headers: dict | None = None,
    limit: int = 400,
) -> tuple[int, str]:
    data = None
    headers = {"Accept": accept, **(extra_headers or {})}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        client = opener or urllib.request.build_opener()
        with client.open(request, timeout=timeout) as response:
            return response.status, response.read(limit).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(limit).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - the report must never crash
        return 0, f"{type(exc).__name__}: {exc}"


def wait_for(url: str, attempts: int = 40, delay: float = 3.0) -> tuple[int, str]:
    for _ in range(attempts):
        status, body = fetch(url, accept="text/html")
        if status:
            return status, body
        time.sleep(delay)
    return fetch(url)


def tail(path: Path, lines: int = 12) -> list[str]:
    if not path.exists():
        return [f"missing {path}"]
    return path.read_text(errors="replace").splitlines()[-lines:]


def sign_in() -> tuple[int, str]:
    """Sign in the demo user exactly like the browser does: cookie first."""

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    _, csrf_body = fetch(CSRF_URL, opener=opener)
    token = json.loads(csrf_body).get("csrf_token", "") if csrf_body else ""
    return fetch(
        LOGIN_URL,
        payload={"username": "demo", "password": "demo1234"},
        timeout=20.0,
        opener=opener,
        extra_headers={"X-CSRFToken": token, "Referer": "http://127.0.0.1:8000/"},
    )


def workflow_smoke() -> dict:
    """Queue a real run and wait for it to finish.

    A console whose runs stay "queued" looks healthy from every HTTP probe and
    is still unusable, so the deployment check drives the whole path the user
    does: create a draft, publish it, run it, and require a terminal status.
    """

    origin = "http://127.0.0.1:8000"
    cookie = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    _, csrf_body = fetch(CSRF_URL, opener=cookie)
    login = fetch(
        LOGIN_URL,
        payload={"username": "demo", "password": "demo1234"},
        opener=cookie,
        extra_headers={"X-CSRFToken": json.loads(csrf_body).get("csrf_token", "")},
    )
    if login[0] not in (200, 201):
        return {"status": "login-failed", "detail": login[1]}
    _, csrf_body = fetch(CSRF_URL, opener=cookie)
    token = json.loads(csrf_body).get("csrf_token", "")
    headers = {"X-CSRFToken": token, "Referer": origin + "/", "Origin": origin}
    tenants_status, tenants_body = fetch(
        f"{origin}/api/v1/tenants/", opener=cookie, extra_headers=headers, limit=4000
    )
    if tenants_status != 200:
        return {"status": "tenants-failed", "detail": tenants_body}
    tenant_id = json.loads(tenants_body)["results"][0]["id"]
    projects_status, projects_body = fetch(
        f"{origin}/api/v1/tenants/{tenant_id}/projects/",
        opener=cookie,
        extra_headers=headers,
        limit=4000,
    )
    if projects_status != 200:
        return {"status": "projects-failed", "detail": projects_body}
    project_id = json.loads(projects_body)["results"][0]["id"]
    definition = {
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [{"source": "start", "target": "end"}],
    }
    created_status, created_body = fetch(
        f"{origin}/api/v1/tenants/{tenant_id}/projects/{project_id}/workflows/",
        payload={
            "name": "Deployment smoke",
            "slug": "deployment-smoke-" + time.strftime("%Y%m%d%H%M%S", time.gmtime()),
            "description": "Deployment self-check",
            "draft_definition": definition,
        },
        opener=cookie,
        extra_headers=headers,
        limit=4000,
    )
    if created_status != 201:
        return {"status": "create-failed", "detail": created_body}
    workflow_id = json.loads(created_body)["id"]
    publish_status, publish_body = fetch(
        f"{origin}/api/v1/workflows/{workflow_id}/publish/",
        payload={},
        opener=cookie,
        extra_headers=headers,
        limit=4000,
    )
    if publish_status != 201:
        return {"status": "publish-failed", "detail": publish_body}
    run_status, run_body = fetch(
        f"{origin}/api/v1/workflows/{workflow_id}/runs/",
        payload={"input": {"message": "deployment smoke"}},
        opener=cookie,
        extra_headers={**headers, "Idempotency-Key": "deployment-smoke-"
                       + time.strftime("%Y%m%d%H%M%S", time.gmtime())},
        limit=4000,
    )
    if run_status not in (200, 202):
        return {"status": "run-rejected", "detail": run_body}
    run_id = json.loads(run_body)["id"]
    terminal = {"succeeded", "failed", "cancelled"}
    last = "queued"
    for _ in range(20):
        time.sleep(1.0)
        _, runs_body = fetch(
            f"{origin}/api/v1/workflows/{workflow_id}/runs/",
            opener=cookie,
            extra_headers=headers,
            limit=8000,
        )
        runs = json.loads(runs_body) if runs_body else []
        runs = runs if isinstance(runs, list) else runs.get("results", [])
        matching = [item for item in runs if item.get("id") == run_id]
        if matching:
            last = matching[0].get("task_status") or last
            if last in terminal:
                return {
                    "status": last,
                    "run_id": run_id[:8],
                    "error": matching[0].get("task_error"),
                }
    return {"status": "never-ran", "run_id": run_id[:8], "last_status": last}


def collect() -> dict:
    data_dir = REPO_ROOT / ".devdata"
    api_status, api_body = wait_for(API_HEALTH, attempts=5, delay=2.0)
    web_status, _ = wait_for(WEB_ROOT)
    login_status, login_body = sign_in()
    workflow = workflow_smoke()
    return {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "api": {"status": api_status, "body": api_body},
        "web": {"status": web_status},
        "login": {"status": login_status, "body": login_body},
        "workflow": workflow,
        "ok": (
            api_status == 200
            and web_status == 200
            and login_status in (200, 201)
            and workflow.get("status") == "succeeded"
        ),
        "api_log": tail(data_dir / "api.log"),
        "web_log": tail(data_dir / "web.log"),
        "worker_log": tail(data_dir / "worker.log"),
    }


def gh(*args: str, stdin: dict | None = None) -> str:
    result = subprocess.run(
        ["gh", *args],
        input=json.dumps(stdin) if stdin else None,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[-500:])
    return result.stdout


def ensure_branch() -> None:
    try:
        gh("api", f"repos/{REPO_SLUG}/git/ref/heads/{BRANCH}")
        return
    except RuntimeError:
        pass
    default_branch = gh("api", f"repos/{REPO_SLUG}", "--jq", ".default_branch").strip()
    head = gh(
        "api", f"repos/{REPO_SLUG}/git/ref/heads/{default_branch}", "--jq", ".object.sha"
    ).strip()
    gh(
        "api",
        "-X",
        "POST",
        f"repos/{REPO_SLUG}/git/refs",
        "--input",
        "-",
        stdin={"ref": f"refs/heads/{BRANCH}", "sha": head},
    )


def publish(status: dict) -> None:
    payload = json.dumps(status, indent=2) + "\n"
    target = REPO_ROOT / STATUS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload)
    body = {
        "message": f"codespace status {status['checked_at']}",
        "content": base64.b64encode(payload.encode()).decode(),
        "branch": BRANCH,
    }
    try:
        ensure_branch()
        try:
            sha = gh(
                "api",
                f"repos/{REPO_SLUG}/contents/{STATUS_PATH}?ref={BRANCH}",
                "--jq",
                ".sha",
            ).strip()
        except RuntimeError:
            sha = ""
        if sha:
            body["sha"] = sha
        gh(
            "api",
            "-X",
            "PUT",
            f"repos/{REPO_SLUG}/contents/{STATUS_PATH}",
            "--input",
            "-",
            stdin=body,
        )
        print("status published")
    except RuntimeError as exc:
        print(f"gh publish failed: {exc}")
        publish_via_git(payload)


def publish_via_git(payload: str) -> None:
    """Fallback channel: git works in codespaces even without gh credentials."""

    worktree = Path("/tmp/codespace-status")
    try:
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=str(REPO_ROOT), check=True)
        subprocess.run(
            ["git", "worktree", "add", "--force", "--detach", str(worktree), f"origin/{BRANCH}"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=True,
            text=True,
        )
        target = worktree / STATUS_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload)
        subprocess.run(["git", "add", STATUS_PATH], cwd=str(worktree), check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=codespace",
                "-c",
                "user.email=codespace@users.noreply.github.com",
                "commit",
                "-q",
                "-m",
                "codespace status",
            ],
            cwd=str(worktree),
            check=True,
        )
        subprocess.run(
            ["git", "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}"],
            cwd=str(worktree),
            check=True,
        )
        print("status published via git")
    except subprocess.CalledProcessError as exc:
        print(f"git publish failed: {(exc.stderr or '').strip()[-500:]}")
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=str(REPO_ROOT),
            capture_output=True,
        )


def main() -> None:
    status = collect()
    print(json.dumps(status, indent=2))
    publish(status)
    sys.exit(0 if status["ok"] else 1)


if __name__ == "__main__":
    main()
