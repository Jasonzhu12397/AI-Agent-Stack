"""Undo a control-plane upgrade by restoring the pre-upgrade etcd snapshot.

Scope is deliberately narrow: one control plane, one verified snapshot, and a
swap that keeps the old data directory instead of deleting it. A cluster with
several control planes needs every member restored together, which this agent
does not coordinate, so it refuses instead of half-restoring a quorum. Package
downgrades are out of scope: kubeadm has no supported downgrade path, so the
binaries stay where they are and only the control-plane state is rolled back.
"""

from __future__ import annotations

import json
import time
from typing import Any

from packages.agent_contracts.kubernetes_rollback import (
    DEFAULT_ETCD_DATA_DIR,
    planned_steps,
)

from .kubernetes_upgrade_exec import UpgradeExecutionError, UpgradeExecutor, _output

STEP_NAMES: tuple[str, ...] = (
    "preflight",
    "etcd_restore",
    "data_swap",
    "kubelet_restart",
    "postflight",
)
STEP_TIMEOUT_SECONDS = 1800
API_WAIT_ATTEMPTS = 60
API_WAIT_INTERVAL_SECONDS = 5.0


class RollbackExecutionError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def restore_dir_for(request: dict[str, str]) -> str:
    return f"{DEFAULT_ETCD_DATA_DIR}/rollback-{_stamp()}"


def member_backup_for(restore_dir: str) -> str:
    return f"{DEFAULT_ETCD_DATA_DIR}/member.pre-rollback-{restore_dir.rsplit('-', 1)[-1]}"


class RollbackExecutor:
    """Restore the snapshot, swap the data directory, restart the kubelet."""

    def __init__(
        self,
        runner,
        *,
        api_wait_attempts: int = API_WAIT_ATTEMPTS,
        api_wait_interval: float = API_WAIT_INTERVAL_SECONDS,
    ) -> None:
        self._runner = runner
        self._api_wait_attempts = api_wait_attempts
        self._api_wait_interval = api_wait_interval
        self._upgrade = UpgradeExecutor(runner)
        self._restore_dir = ""
        self._member_backup = ""

    # -- reads -----------------------------------------------------------
    def _nodes(self) -> list[dict[str, Any]]:
        result = self._runner(["kubectl", "get", "nodes", "-o", "json"], timeout=60)
        if not result.get("available", True) or result.get("returncode") != 0:
            raise RollbackExecutionError("ROLLBACK_NODES_UNREADABLE", _output(result))
        try:
            payload = json.loads(result.get("stdout") or "")
        except ValueError as exc:
            raise RollbackExecutionError("ROLLBACK_NODES_JSON_INVALID", str(exc)) from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise RollbackExecutionError("ROLLBACK_NODES_JSON_INVALID")
        return [item for item in items if isinstance(item, dict)]

    def _check(self, name: str, ok: bool, detail: str) -> dict[str, Any]:
        return {"name": name, "ok": ok, "detail": detail}

    def _etcd_pod_on(self, node_name: str) -> tuple[str, list[dict[str, Any]]]:
        try:
            pods = self._upgrade._etcd_pods()
        except UpgradeExecutionError as error:
            raise RollbackExecutionError(error.code, error.detail) from error
        if not pods:
            raise RollbackExecutionError(
                "ROLLBACK_ETCD_UNREADABLE", "this cluster has no stacked etcd pod"
            )
        for item in pods:
            if str((item.get("spec") or {}).get("nodeName")) == node_name:
                name = str((item.get("metadata") or {}).get("name") or "")
                if name:
                    return name, pods
        name = str((pods[0].get("metadata") or {}).get("name") or "")
        if not name:
            raise RollbackExecutionError(
                "ROLLBACK_ETCD_UNREADABLE", "no etcd pod name to run etcdctl in"
            )
        return name, pods

    def _run(self, argv: list[str], timeout: int = STEP_TIMEOUT_SECONDS) -> dict[str, Any]:
        return self._runner(argv, timeout=timeout)

    @staticmethod
    def _ok(result: dict[str, Any]) -> bool:
        return result.get("available", True) and result.get("returncode") == 0

    # -- preflight -------------------------------------------------------
    def assess(self, request: dict[str, str]) -> dict[str, Any]:
        """Read-only: what a rollback of this cluster would be allowed to do.

        The lab runner has a kubeconfig and no rights to stop a kubelet, so this
        is the same evidence preflight with none of the mutations.
        """

        nodes = self._nodes()
        control_planes = [
            node for node in nodes if self._upgrade._is_control_plane(node)
        ]
        count = len(control_planes)
        assessed: dict[str, Any] = {
            "control_planes": count,
            "control_plane": "",
            "etcd_pod": "",
            "checks": [
                self._check(
                    "cluster.control_plane.count",
                    count == 1,
                    f"{count} control plane(s); a rollback restores exactly one",
                )
            ],
            "blocked": count != 1,
            "refusal": (
                ""
                if count == 1
                else "ROLLBACK_MULTI_CONTROL_PLANE_UNSUPPORTED"
                if count > 1
                else "ROLLBACK_CONTROL_PLANE_NOT_FOUND"
            ),
        }
        if count != 1:
            return assessed
        control_plane = control_planes[0]
        node_name = self._upgrade._node_name(control_plane)
        assessed["control_plane"] = node_name
        etcd_pod, _pods = self._etcd_pod_on(node_name)
        assessed["etcd_pod"] = etcd_pod
        preflight = self._preflight(request, control_plane, etcd_pod)
        assessed["checks"] = [*assessed["checks"], *preflight["checks"]]
        assessed["blocked"] = preflight["blocked"]
        return assessed

    def _preflight(
        self,
        request: dict[str, str],
        control_plane: dict[str, Any],
        etcd_pod: str,
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        name = self._upgrade._node_name(control_plane)
        conditions = (control_plane.get("status") or {}).get("conditions") or []
        ready = any(
            isinstance(item, dict)
            and item.get("type") == "Ready"
            and item.get("status") == "True"
            for item in conditions
        )
        checks.append(
            self._check(
                f"node.ready/{name}",
                ready,
                "Ready=True" if ready else "node is not Ready",
            )
        )
        status_argv = [
            *self._upgrade._etcdctl_argv(etcd_pod),
            "snapshot",
            "status",
            request["snapshot_path"],
            "--write-out=json",
        ]
        verified = self._run(status_argv, timeout=60)
        body = (verified.get("stdout") or "").strip()
        readable = self._ok(verified)
        hashed = readable and body.startswith("{") and "hash" in body
        # One probe inside the etcd pod answers both questions: whether the file
        # is visible where the restore will read it, and whether it verifies.
        checks.append(
            self._check(
                "etcd.snapshot.readable",
                readable,
                f"{request['snapshot_path']} is readable from the etcd pod"
                if readable
                else f"{request['snapshot_path']} is missing or unreadable: {_output(verified)}",
            )
        )
        checks.append(
            self._check(
                "etcd.snapshot.verified",
                hashed,
                "snapshot status returned its hash"
                if hashed
                else "etcdctl snapshot status did not verify the file",
            )
        )
        healthy, detail = self._upgrade._etcd_quorum_healthy(etcd_pod)
        checks.append(
            self._check(
                "etcd.quorum",
                healthy,
                detail if healthy else f"etcd is not healthy before the rollback: {detail}",
            )
        )
        if request["execution_mode"] == "execute":
            confirmed = request.get("confirm_cluster_name") == request["cluster_name"]
            checks.append(
                self._check(
                    "rollback.confirmation",
                    confirmed,
                    f"confirmed {request['cluster_name']}" if confirmed
                    else "confirm_cluster_name does not match",
                )
            )
        return {"checks": checks, "blocked": any(not item["ok"] for item in checks)}

    # -- steps -----------------------------------------------------------
    def _step(self, sequence: int, name: str) -> dict[str, Any]:
        return {
            "sequence": sequence,
            "name": name,
            "status": "not_started",
            "commands": [],
            "exit_code": None,
            "detail": "",
        }

    def _execute(self, step: dict[str, Any], argv: list[str], timeout: int) -> bool:
        step["commands"].append(" ".join(argv))
        result = self._run(argv, timeout=timeout)
        step["exit_code"] = result.get("returncode")
        ok = self._ok(result)
        step["status"] = "succeeded" if ok else "failed"
        if not ok:
            step["detail"] = _output(result)
        return ok

    def _wait_for_api(self) -> tuple[bool, str]:
        detail = ""
        for attempt in range(1, self._api_wait_attempts + 1):
            result = self._run(["kubectl", "get", "nodes", "-o", "json"], timeout=60)
            if self._ok(result):
                return True, f"apiserver answered after {attempt} probe(s)"
            detail = _output(result)
            if attempt < self._api_wait_attempts:
                time.sleep(self._api_wait_interval)
        return False, detail

    def run(self, request: dict[str, str]) -> dict[str, Any]:
        assessment = self.assess(request)
        if assessment["refusal"]:
            # Restoring one member of a stacked etcd would break the quorum.
            detail = (
                f"{assessment['control_planes']} control planes; every member "
                "must be restored together"
                if assessment["control_planes"] > 1
                else "no control-plane node reported itself"
            )
            raise RollbackExecutionError(assessment["refusal"], detail)
        etcd_pod = assessment["etcd_pod"]

        self._restore_dir = restore_dir_for(request)
        self._member_backup = member_backup_for(self._restore_dir)
        steps = planned_steps(request, restore_dir=self._restore_dir)
        performed: list[str] = []

        preflight = {"checks": assessment["checks"], "blocked": assessment["blocked"]}
        steps[0]["status"] = "succeeded" if not preflight["blocked"] else "failed"
        steps[0]["detail"] = (
            f"{sum(1 for item in preflight['checks'] if item['ok'])}/"
            f"{len(preflight['checks'])} checks passed"
        )
        steps[0]["exit_code"] = 0 if not preflight["blocked"] else 1
        if preflight["blocked"]:
            return self._result(
                request, steps, preflight, performed, "ROLLBACK_PREFLIGHT_BLOCKED"
            )

        restore_argv = [
            *self._upgrade._etcdctl_argv(etcd_pod),
            "snapshot",
            "restore",
            request["snapshot_path"],
            f"--data-dir={self._restore_dir}",
        ]
        if not self._execute(steps[1], restore_argv, STEP_TIMEOUT_SECONDS):
            return self._result(
                request, steps, preflight, performed, "ROLLBACK_RESTORE_FAILED"
            )
        restored_dir = self._run(
            ["sudo", "test", "-d", f"{self._restore_dir}/member"], timeout=60
        )
        steps[1]["commands"].append(f"sudo test -d {self._restore_dir}/member")
        if not self._ok(restored_dir):
            steps[1]["status"] = "failed"
            steps[1]["detail"] = f"{self._restore_dir}/member was not written"
            return self._result(
                request, steps, preflight, performed, "ROLLBACK_RESTORE_FAILED"
            )
        steps[1]["status"] = "succeeded"
        performed.append("etcd_restore")

        # The old data directory is moved aside, never deleted: if the swap fails
        # the operator still has the pre-rollback member on disk.
        stop_kubelet = ["sudo", "systemctl", "stop", "kubelet"]
        if not self._execute(steps[2], stop_kubelet, STEP_TIMEOUT_SECONDS):
            return self._result(
                request, steps, preflight, performed, "ROLLBACK_KUBELET_STOP_FAILED"
            )
        old_member = f"{DEFAULT_ETCD_DATA_DIR}/member"
        move_old = ["sudo", "mv", old_member, self._member_backup]
        if not self._execute(steps[2], move_old, STEP_TIMEOUT_SECONDS):
            return self._result(
                request, steps, preflight, performed, "ROLLBACK_SWAP_FAILED"
            )
        if not self._execute(
            steps[2],
            ["sudo", "mv", f"{self._restore_dir}/member", old_member],
            STEP_TIMEOUT_SECONDS,
        ):
            # Put the old data back so a failed rollback does not leave a cluster
            # with no data directory at all.
            self._run(
                ["sudo", "mv", self._member_backup, old_member], timeout=STEP_TIMEOUT_SECONDS
            )
            steps[2]["detail"] = (
                f"restored member could not be moved in; old data kept at "
                f"{self._member_backup}"
            )
            return self._result(request, steps, preflight, performed, "ROLLBACK_SWAP_FAILED")
        steps[2]["status"] = "succeeded"
        performed.append("data_swap")

        start_kubelet = ["sudo", "systemctl", "start", "kubelet"]
        if not self._execute(steps[3], start_kubelet, STEP_TIMEOUT_SECONDS):
            return self._result(
                request, steps, preflight, performed, "ROLLBACK_KUBELET_START_FAILED"
            )
        steps[3]["commands"] = ["sudo systemctl start kubelet"]
        api_ok, api_detail = self._wait_for_api()
        steps[3]["detail"] = api_detail
        if not api_ok:
            steps[3]["status"] = "failed"
            return self._result(
                request, steps, preflight, performed, "ROLLBACK_API_UNAVAILABLE"
            )
        steps[3]["status"] = "succeeded"
        performed.append("kubelet_restart")

        steps[4]["commands"] = []
        try:
            nodes_after = self._nodes()
        except RollbackExecutionError as error:
            steps[4]["status"] = "failed"
            steps[4]["detail"] = error.detail
            return self._result(request, steps, preflight, performed, "ROLLBACK_POSTFLIGHT_FAILED")
        steps[4]["commands"].append("kubectl get nodes -o json")
        healthy, health_detail = self._upgrade._etcd_quorum_healthy(etcd_pod)
        steps[4]["commands"].append(" ".join(self._upgrade._etcdctl_argv(etcd_pod)))
        steps[4]["detail"] = health_detail
        if not healthy:
            steps[4]["status"] = "failed"
            return self._result(request, steps, preflight, performed, "ROLLBACK_POSTFLIGHT_FAILED")
        steps[4]["status"] = "succeeded"
        performed.append("postflight")
        versions = [
            {
                "name": self._upgrade._node_name(node),
                "version": self._upgrade._node_version(node),
            }
            for node in nodes_after
        ]
        return self._result(
            request, steps, preflight, performed, None, versions=versions
        )

    def _result(
        self,
        request: dict[str, str],
        steps: list[dict[str, Any]],
        preflight: dict[str, Any],
        performed: list[str],
        failure: str | None,
        *,
        versions: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return {
            "cluster_name": request["cluster_name"],
            "snapshot_path": request["snapshot_path"],
            "execution_mode": request["execution_mode"],
            "approval_required": True,
            "rollback_simulated": False,
            "rolled_back": failure is None,
            "restore_dir": self._restore_dir,
            "previous_member_dir": self._member_backup,
            "performed_steps": performed,
            "steps": steps,
            "preflight": preflight,
            "node_versions": versions or [],
            "failure_code": failure,
        }
