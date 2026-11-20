"""Execute software work items claimed from the Cloud Control plane.

The control plane owns scheduling and leases. This module owns execution: it
validates the payload, journals a stable task, runs a read/analysis action and
returns a bounded result. It deliberately has no action that writes to the
repository or pushes code.
"""

from __future__ import annotations

import hashlib
import json
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .agent import SoftwareAgent
from .checkpoint import TaskStore
from .patching import TextEdit
from .testing import detect_test_plan

ACTIONS = frozenset(
    {
        "capabilities",
        "analyse",
        "review",
        "performance",
        "profile",
        "tests",
        "test_plan",
        "security",
        "docs",
        "plan_comment",
        "apply_patch",
        "bugfix",
        "git_status",
        "commit_plan",
        "plan",
    }
)
OPTIONS = frozenset({"action", "paths", "top", "output", "goal", "token", "edits", "approval_id"})
MAX_RESULT_BYTES = 240 * 1024
PHASE_FOR_ACTION = {
    "capabilities": "analyse",
    "analyse": "analyse",
    "review": "review",
    "performance": "review",
    "profile": "review",
    "tests": "test",
    "test_plan": "test",
    "security": "review",
    "docs": "deliver",
    "plan_comment": "change",
    "apply_patch": "change",
    "bugfix": "change",
    "git_status": "commit",
    "commit_plan": "commit",
    "plan": "analyse",
}


def canonical_digest(edits: list[dict]) -> str:
    payload = json.dumps(edits, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def granted_actions(skills: list[dict] | None) -> set[str]:
    """Intersect requested actions with the project's enabled skill manifests.

    No skills means the project has not restricted the agent, so every built-in
    action is available. A non-empty catalog is authoritative: only actions
    listed as ``software.<action>`` are granted.
    """
    if not skills:
        return set(ACTIONS)
    granted: set[str] = set()
    for skill in skills:
        manifest = skill.get("manifest") if isinstance(skill, dict) else None
        actions = manifest.get("actions") if isinstance(manifest, dict) else None
        if not isinstance(actions, list):
            continue
        for item in actions:
            if isinstance(item, str) and item.startswith("software."):
                granted.add(item.split(".", 1)[1])
    return granted & set(ACTIONS)


class WorkPayloadError(ValueError):
    """The work payload does not match the software contract."""


@dataclass
class WorkExecution:
    task_id: str
    status: str
    result: dict

    def as_dict(self) -> dict:
        return {"task_id": self.task_id, "status": self.status, "result": self.result}


class WorkClient(Protocol):
    def claim_work(self, kind: str | None = None) -> dict[str, Any] | None: ...

    def report_work_result(
        self, work: dict[str, Any], status: str, result: dict[str, Any]
    ) -> dict[str, Any]: ...


def _relative(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkPayloadError(f"{field} must be a non-empty string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise WorkPayloadError(f"{field} must stay inside the workspace")
    return value


def validate_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise WorkPayloadError("software work payload must be an object")
    unknown = set(payload) - OPTIONS
    if unknown:
        raise WorkPayloadError(f"unsupported payload keys: {sorted(unknown)}")
    action = payload.get("action")
    if action not in ACTIONS:
        raise WorkPayloadError(f"action must be one of {sorted(ACTIONS)}")
    paths = payload.get("paths", [])
    if not isinstance(paths, list) or len(paths) > 32:
        raise WorkPayloadError("paths must be a list of at most 32 entries")
    normalized_paths = [_relative(path, "paths") for path in paths]
    top = payload.get("top", 15)
    if not isinstance(top, int) or isinstance(top, bool) or not 1 <= top <= 100:
        raise WorkPayloadError("top must be an integer from 1 to 100")
    output = payload.get("output", "docs/generated")
    normalized_output = _relative(output, "output")
    goal = payload.get("goal", "")
    if not isinstance(goal, str) or len(goal) > 4000:
        raise WorkPayloadError("goal must be a string of at most 4000 characters")
    token = payload.get("token", "")
    if not isinstance(token, str) or len(token) > 128:
        raise WorkPayloadError("token must be a string of at most 128 characters")
    approval_id = payload.get("approval_id", "")
    if not isinstance(approval_id, str) or len(approval_id) > 64:
        raise WorkPayloadError("approval_id must be a string of at most 64 characters")
    edits = payload.get("edits", [])
    if not isinstance(edits, list) or len(edits) > 20:
        raise WorkPayloadError("edits must be a list of at most 20 entries")
    normalized_edits = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict) or set(edit) != {"path", "old", "new"}:
            raise WorkPayloadError(f"edits[{index}] must contain path, old and new")
        if not all(isinstance(edit[key], str) and edit[key] for key in ("path", "old", "new")):
            raise WorkPayloadError(f"edits[{index}] values must be non-empty strings")
        normalized_edits.append(
            {
                "path": _relative(edit["path"], f"edits[{index}].path"),
                "old": edit["old"],
                "new": edit["new"],
            }
        )
    if action == "apply_patch" and (not normalized_edits or not (token or approval_id)):
        raise WorkPayloadError(
            "apply_patch requires edits plus an approved token or control approval_id"
        )
    if action == "bugfix" and not normalized_edits:
        raise WorkPayloadError("bugfix requires edits")
    return {
        "action": action,
        "paths": normalized_paths,
        "top": top,
        "output": normalized_output,
        "goal": goal,
        "token": token,
        "approval_id": approval_id,
        "edits": normalized_edits,
    }


def _bound(result: Any) -> dict:
    serialized = json.dumps(result, default=str, ensure_ascii=False)
    if len(serialized.encode()) <= MAX_RESULT_BYTES:
        return result if isinstance(result, dict) else {"value": result}
    return {
        "truncated": True,
        "bytes": len(serialized.encode()),
        "preview": serialized[:200000],
    }


class SoftwareWorkExecutor:
    def __init__(
        self,
        root: str | Path,
        agent: SoftwareAgent | None = None,
        autonomous: bool = False,
        actor: str = "operator",
        approval_client: Any | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.agent = agent or SoftwareAgent(self.root, autonomous=autonomous, actor=actor)
        self.store = TaskStore(self.root)
        self.approval_client = approval_client

    def _redeem(self, request: dict, kind: str) -> None:
        if not request["approval_id"]:
            return
        if self.approval_client is None:
            raise WorkPayloadError("approval_id requires a control-plane client")
        self.approval_client.redeem_approval(
            work_id=request["work_id"],
            approval_id=request["approval_id"],
            action=request["action"],
            payload_digest=canonical_digest(request["edits"]),
            kind=kind,
        )

    def _run(self, request: dict) -> Any:
        action = request["action"]
        if action == "capabilities":
            return self.agent.capabilities()
        if action == "analyse":
            return self.agent.analyse()
        if action == "review":
            return self.agent.review(request["paths"] or None)
        if action == "performance":
            return self.agent.performance(request["paths"] or None)
        if action == "profile":
            return self.agent.profile(request["top"]).as_dict()
        if action == "tests":
            return self.agent.tests().as_dict()
        if action == "test_plan":
            plan = detect_test_plan(self.root)
            return {"command": plan.command, "framework": plan.framework} if plan else None
        if action == "security":
            return self.agent.security()
        if action == "docs":
            return {"written": self.agent.documents(request["output"])}
        if action == "plan_comment":
            plan = self.agent.comment_plan(request["top"])
            return {
                "diff": plan.diff,
                "token": plan.token,
                "status": "pending",
                "paths": plan.paths,
            }
        if action == "apply_patch":
            edits = [TextEdit(**edit) for edit in request["edits"]]
            self._redeem(request, "apply-patch")
            token = request["token"]
            if not token:
                token = self.agent.plan_change(edits).token
                self.agent.approvals.approve(token, "control-plane")
            return {"applied": self.agent.apply_change(edits, token)}
        if action == "bugfix":
            edits = [TextEdit(**edit) for edit in request["edits"]]
            self._redeem(request, "bugfix")
            if request["token"]:
                token = request["token"]
            elif request["approval_id"]:
                token = self.agent.plan_change(edits).token
                self.agent.approvals.approve(token, "control-plane")
            elif self.agent.autonomous:
                token = self.agent.plan_change(edits).token
            else:
                raise WorkPayloadError(
                    "bugfix needs an approved token unless the agent runs autonomously"
                )
            applied = self.agent.apply_change(edits, token)
            tests = self.agent.tests()
            return {
                "applied": applied,
                "tests": {"passed": tests.passed, "failed": tests.failed, "ok": tests.ok},
                "approval": "autonomous"
                if self.agent.autonomous and not request["token"]
                else "token",
            }
        if action == "git_status":
            return self.agent.git_status()
        if action == "commit_plan":
            return self.agent.commit_plan()
        if action == "plan":
            if not request["goal"]:
                raise WorkPayloadError("plan requires a goal")
            return self.agent.plan(request["goal"])
        raise WorkPayloadError(f"unhandled action: {action}")

    def execute(
        self,
        work_id: str,
        payload: Any,
        kind: str = "software",
        skills: list[dict] | None = None,
        plugins: list[dict] | None = None,
    ) -> WorkExecution:
        if kind != "software":
            raise WorkPayloadError(f"unsupported work kind: {kind}")
        if not isinstance(work_id, str) or not work_id.strip():
            raise WorkPayloadError("work_id must be a non-empty string")
        task_id = f"work-{work_id}"
        try:
            request = validate_payload(payload)
            granted = granted_actions(skills)
            if request["action"] not in granted:
                raise WorkPayloadError(
                    f"action {request['action']} is not granted by the project skills"
                )
            request["work_id"] = work_id
        except WorkPayloadError as error:
            self.store.ensure(task_id, "invalid control-plane payload")
            self.store.update(task_id, status="failed", checkpoint={"error": str(error)})
            return WorkExecution(task_id, "failed", {"error": str(error)})
        goal = request["goal"] or f"{request['action']} requested by the control plane"
        self.store.ensure(task_id, goal)
        self.store.update(
            task_id,
            phase=PHASE_FOR_ACTION[request["action"]],
            status="running",
            checkpoint={
                "action": request["action"],
                "skills": [skill.get("name") for skill in (skills or [])],
                "plugins": [plugin.get("name") for plugin in (plugins or [])],
            },
        )
        try:
            result = _bound(self._run(request))
        except Exception as error:  # noqa: BLE001 - a remote request must fail cleanly
            message = "".join(traceback.format_exception_only(type(error), error)).strip()
            self.store.update(
                task_id,
                status="failed",
                checkpoint={"error": message, "action": request["action"]},
            )
            return WorkExecution(task_id, "failed", {"error": message, "action": request["action"]})
        self.store.update(
            task_id,
            status="succeeded",
            checkpoint={"action": request["action"], "completed": True},
        )
        return WorkExecution(task_id, "succeeded", result)

    def execute_work_item(
        self,
        work: dict,
        skills: list[dict] | None = None,
        plugins: list[dict] | None = None,
    ) -> WorkExecution:
        if not isinstance(work, dict):
            raise WorkPayloadError("work item must be an object")
        work_id = work.get("work_id")
        return self.execute(
            str(work_id),
            work.get("payload"),
            work.get("kind", "software"),
            skills,
            plugins,
        )


def serve(
    client: WorkClient,
    root: str | Path,
    max_items: int = 1,
    kind: str = "software",
    autonomous: bool = True,
    actor: str = "software-agent",
) -> list[dict]:
    """Claim up to max_items software work items, execute them and report."""
    executor = SoftwareWorkExecutor(
        root,
        autonomous=autonomous,
        actor=actor,
        approval_client=client if hasattr(client, "redeem_approval") else None,
    )
    handled: list[dict] = []
    for _ in range(max_items):
        claimed = client.claim_work(kind=kind)
        if not claimed:
            break
        work = claimed["work"]
        if not isinstance(work, dict) or work.get("kind") != kind:
            break
        execution = executor.execute_work_item(
            work, claimed.get("skills", []), claimed.get("plugins", [])
        )
        client.report_work_result(work, execution.status, execution.result)
        handled.append({"work_id": work.get("work_id"), **execution.as_dict()})
    return handled
