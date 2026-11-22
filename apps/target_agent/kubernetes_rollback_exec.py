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
import os
import shlex
import time
from typing import Any
from urllib.parse import urlsplit

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
            "members": [],
            "checks": [
                self._check(
                    "cluster.control_plane.count",
                    count >= 1,
                    f"{count} control plane(s); a stacked etcd is restored member by member",
                )
            ],
            "blocked": count < 1,
            "refusal": "" if count >= 1 else "ROLLBACK_CONTROL_PLANE_NOT_FOUND",
        }
        if count < 1:
            return assessed
        if count > 1:
            return self._assess_multi(request, control_planes, assessed)
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
        visible, visible_detail = self._snapshot_in_pod_path(request["snapshot_path"])
        checks.append(self._check("rollback.snapshot.path", visible, visible_detail or
            f"{request['snapshot_path']} is inside {DEFAULT_ETCD_DATA_DIR}"))
        status_argv = [
            *self._upgrade._etcdctl_argv(etcd_pod),
            "snapshot",
            "status",
            request["snapshot_path"],
            "--write-out=json",
        ]
        verified = (
            self._run(status_argv, timeout=60)
            if visible
            else {"available": False, "returncode": 1, "stdout": "", "stderr": visible_detail}
        )
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
        command = " ".join(argv)
        # The step already lists the planned command; record the executed one
        # once so evidence and plan cannot drift apart.
        if command not in step["commands"]:
            step["commands"].append(command)
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
            raise RollbackExecutionError(
                assessment["refusal"], "no control-plane node reported itself"
            )
        if assessment["control_planes"] > 1:
            return self._run_multi(request, assessment)
        etcd_pod = assessment["etcd_pod"]

        self._restore_dir = restore_dir_for(request)
        self._member_backup = member_backup_for(self._restore_dir)
        steps = planned_steps(request, restore_dir=self._restore_dir)
        # The plan carries placeholders (``<etcd-pod>``, ``<utc>``); an executed
        # step must list what really ran instead of both.
        for step in steps:
            step["commands"] = []
        performed: list[str] = []

        preflight = {"checks": assessment["checks"], "blocked": assessment["blocked"]}
        steps[0]["status"] = "succeeded" if not preflight["blocked"] else "failed"
        steps[0]["detail"] = (
            f"{sum(1 for item in preflight['checks'] if item['ok'])}/"
            f"{len(preflight['checks'])} checks passed"
        )
        steps[0]["exit_code"] = 0 if not preflight["blocked"] else 1
        steps[0]["commands"] = [
            "kubectl get nodes -o json",
            " ".join(
                [
                    *self._upgrade._etcdctl_argv(etcd_pod),
                    "snapshot",
                    "status",
                    request["snapshot_path"],
                    "--write-out=json",
                ]
            ),
        ]
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
        api_ok, api_detail = self._wait_for_api()
        steps[3]["detail"] = api_detail
        if not api_ok:
            steps[3]["status"] = "failed"
            return self._result(
                request, steps, preflight, performed, "ROLLBACK_API_UNAVAILABLE"
            )
        steps[3]["status"] = "succeeded"
        performed.append("kubelet_restart")

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

    # -- a stacked etcd ------------------------------------------------
    def _control_planes(self) -> list[dict[str, Any]]:
        return [node for node in self._nodes() if self._upgrade._is_control_plane(node)]

    def _members(self, control_planes: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Map every control-plane node to its etcd member and pod.

        The member list is the authority: it carries the name and peer URL the
        restore needs, and the peer URL host is how a member is tied to a node.
        """

        pods = self._upgrade._etcd_pods()
        pod_by_node: dict[str, str] = {}
        for item in pods:
            node_name = str((item.get("spec") or {}).get("nodeName") or "")
            name = str((item.get("metadata") or {}).get("name") or "")
            if node_name and name:
                pod_by_node[node_name] = name
        local_name = self._upgrade._local_control_plane(
            [self._upgrade._node_name(node) for node in control_planes]
        )
        # Commands run over kubectl/on localhost, so one member's list describes
        # the whole cluster; use the local pod when it exists.
        probe_pod = pod_by_node.get(local_name) or next(iter(pod_by_node.values()), "")
        if not probe_pod:
            raise RollbackExecutionError(
                "ROLLBACK_ETCD_UNREADABLE", "no etcd pod runs on a control plane"
            )
        result = self._run(
            [*self._upgrade._etcdctl_argv(probe_pod), "member", "list", "--write-out=json"],
            timeout=60,
        )
        if not self._ok(result):
            raise RollbackExecutionError("ROLLBACK_ETCD_UNREADABLE", _output(result))
        try:
            document = json.loads((result.get("stdout") or "").strip())
        except ValueError as exc:
            raise RollbackExecutionError("ROLLBACK_ETCD_JSON_INVALID", str(exc)) from exc
        entries = document.get("members") if isinstance(document, dict) else None
        if not isinstance(entries, list) or not entries:
            raise RollbackExecutionError("ROLLBACK_ETCD_JSON_INVALID")
        members: list[dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            peer_urls = entry.get("peerURLs")
            peer_url = str(peer_urls[0]) if isinstance(peer_urls, list) and peer_urls else ""
            # ``urlsplit`` keeps a bracketed IPv6 address whole; splitting on
            # the last colon would turn ``https://[fd00::1]:2380`` into
            # ``[fd00::1``, which never matches a node address.
            host = ""
            if peer_url:
                host = urlsplit(peer_url).hostname or ""
            name = str(entry.get("name") or "")
            node = next(
                (
                    self._upgrade._node_name(node)
                    for node in control_planes
                    if self._upgrade._node_address(node) == host
                ),
                "",
            )
            members.append(
                {
                    "name": name,
                    "node": node,
                    "pod": pod_by_node.get(node, ""),
                    "peer_url": peer_url,
                    "address": host,
                }
            )
        if any(not item["node"] or not item["pod"] for item in members):
            raise RollbackExecutionError(
                "ROLLBACK_ETCD_MEMBER_UNMATCHED",
                "an etcd member's peer address is not a control-plane InternalIP",
            )
        return members

    def _ssh_target(self, member: dict[str, str]) -> str:
        if not self._upgrade._remote_user:
            raise RollbackExecutionError(
                "ROLLBACK_REMOTE_EXECUTOR_NOT_CONFIGURED",
                "set KUBERNETES_UPGRADE_REMOTE_USER so remote members can be restored",
            )
        return f"{self._upgrade._remote_user}@{member['address']}"

    def _on_member(
        self, member: dict[str, str], argv: list[str], *, local: bool
    ) -> list[str]:
        if local:
            return argv
        return ["ssh", "-o", "BatchMode=yes", self._ssh_target(member), shlex.join(argv)]

    def _member_ready_check(
        self,
        member: dict[str, str],
        nodes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        node = next(
            (item for item in nodes if self._upgrade._node_name(item) == member["node"]),
            None,
        )
        return self._preflight_node_check(member["node"], node)

    @staticmethod
    def _snapshot_in_pod_path(path: str) -> tuple[bool, str]:
        """Whether the etcd pod can reach a host path at all.

        The etcd static pod mounts the host data directory and nothing else, so
        a snapshot stored anywhere else is readable on the host and invisible
        inside the pod - the restore would fail after the kubelets are already
        stopped. Saying so in preflight keeps that from happening.
        """

        mount = DEFAULT_ETCD_DATA_DIR.rstrip("/")
        if not path.startswith(mount + "/"):
            return False, (
                f"{path} is outside {mount}, the only host path the etcd pod mounts; "
                f"move the snapshot under {mount}/backups"
            )
        return True, ""

    def _preflight_node_check(self, name: str, node: dict[str, Any] | None) -> dict[str, Any]:
        conditions = ((node or {}).get("status") or {}).get("conditions") or []
        ready = any(
            isinstance(item, dict)
            and item.get("type") == "Ready"
            and item.get("status") == "True"
            for item in conditions
        )
        return self._check(
            f"node.ready/{name}",
            ready and node is not None,
            "Ready=True" if ready else "node is not Ready",
        )

    def _assess_multi(
        self,
        request: dict[str, str],
        control_planes: list[dict[str, Any]],
        assessed: dict[str, Any],
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = list(assessed["checks"])
        try:
            local_name = self._upgrade._local_control_plane(
                [self._upgrade._node_name(node) for node in control_planes]
            )
            members = self._members(control_planes)
        except UpgradeExecutionError as error:
            # Which host this agent runs on, and which member it owns, are facts
            # a restore cannot proceed without.
            checks.append(self._check("local.node", False, error.detail or error.code))
            assessed["checks"] = checks
            assessed["blocked"] = True
            return assessed
        assessed["members"] = [dict(item) for item in members]
        for member in members:
            checks.append(self._member_ready_check(member, control_planes))
        visible, visible_detail = self._snapshot_in_pod_path(request["snapshot_path"])
        checks.append(
            self._check(
                "rollback.snapshot.path",
                visible,
                visible_detail
                or f"{request['snapshot_path']} is inside {DEFAULT_ETCD_DATA_DIR}",
            )
        )
        for member in members:
            if member["node"] != local_name:
                # The snapshot lives on this host. Reading it from a remote pod
                # in preflight could only fail, so the remote check proves the
                # file is verified once, locally, and that the copy is staged
                # when the restore actually runs.
                checks.append(
                    self._check(
                        f"etcd.snapshot.readable/{member['node']}",
                        True,
                        f"{request['snapshot_path']} is staged on {member['node']} "
                        "when the restore runs",
                    )
                )
                continue
            result = (
                self._run(
                    [
                        *self._upgrade._etcdctl_argv(member["pod"]),
                        "snapshot",
                        "status",
                        request["snapshot_path"],
                        "--write-out=json",
                    ],
                    timeout=60,
                )
                if visible
                else {
                    "available": False,
                    "returncode": 1,
                    "stdout": "",
                    "stderr": visible_detail,
                }
            )
            body = (result.get("stdout") or "").strip()
            checks.append(
                self._check(
                    f"etcd.snapshot.readable/{member['node']}",
                    self._ok(result),
                    f"{request['snapshot_path']} is readable from {member['pod']}"
                    if self._ok(result)
                    else f"{request['snapshot_path']} is missing or unreadable: {_output(result)}",
                )
            )
            checks.append(
                self._check(
                    f"etcd.snapshot.verified/{member['node']}",
                    self._ok(result) and body.startswith("{") and "hash" in body,
                    "snapshot status returned its hash",
                )
            )
        healthy, detail = self._upgrade._etcd_quorum_healthy(members[0]["pod"])
        checks.append(
            self._check(
                "etcd.quorum",
                healthy,
                detail if healthy else f"etcd is not healthy before the rollback: {detail}",
            )
        )
        remote = [item for item in members if item["node"] != local_name]
        if remote and not self._upgrade._remote_user:
            checks.append(
                self._check(
                    "ssh.configured",
                    False,
                    "set KUBERNETES_UPGRADE_REMOTE_USER for the remote members",
                )
            )
        else:
            checks.extend(self._upgrade._ssh_reachability_checks(control_planes))
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
        assessed["checks"] = checks
        assessed["blocked"] = any(not item["ok"] for item in checks)
        assessed["local_node"] = local_name
        return assessed

    def _stage_snapshot(self, member: dict[str, str], snapshot: str, step: dict[str, Any]) -> bool:
        """Copy the snapshot to a remote member's own etcd data path.

        ``scp`` writes as the login user, so copying straight into
        ``/var/lib/etcd`` (root-owned) fails halfway and leaves a partial file.
        The file therefore lands in a per-user temporary path first and is
        installed by ``sudo`` with the mode etcdctl expects.
        """

        directory = snapshot.rsplit("/", 1)[0]
        target = self._ssh_target(member)
        staging = f"/tmp/{os.path.basename(snapshot)}"
        steps: list[list[str]] = [
            ["ssh", "-o", "BatchMode=yes", target, "sudo", "mkdir", "-p", directory],
            ["scp", "-o", "BatchMode=yes", snapshot, f"{target}:{staging}"],
            [
                "ssh", "-o", "BatchMode=yes", target,
                "sudo", "install", "-m", "0444", staging, snapshot,
            ],
            ["ssh", "-o", "BatchMode=yes", target, "sudo", "rm", "-f", staging],
            ["ssh", "-o", "BatchMode=yes", target, "sudo", "test", "-r", snapshot],
        ]
        for argv in steps:
            if not self._execute(step, argv, STEP_TIMEOUT_SECONDS):
                return False
        return True

    def _run_multi(self, request: dict[str, str], assessment: dict[str, Any]) -> dict[str, Any]:
        """Restore the same snapshot on every member of a stacked etcd.

        Order matters. Every member is restored while the cluster still runs,
        then every kubelet is stopped, the data directories are swapped, and the
        kubelets are started again so the quorum forms from restored data. The
        previous member directory is kept on every node.
        """

        members: list[dict[str, str]] = list(assessment["members"])
        local_name = str(assessment.get("local_node") or "")
        self._local_name = local_name
        self._restore_dir = restore_dir_for(request)
        self._member_backup = member_backup_for(self._restore_dir)
        steps = planned_steps(
            request,
            restore_dir=self._restore_dir,
            members=tuple(members),
        )
        for step in steps:
            step["commands"] = []
        performed: list[str] = []
        preflight = {"checks": assessment["checks"], "blocked": assessment["blocked"]}

        steps[0]["status"] = "succeeded" if not preflight["blocked"] else "failed"
        steps[0]["detail"] = (
            f"{sum(1 for item in preflight['checks'] if item['ok'])}/"
            f"{len(preflight['checks'])} checks passed"
        )
        steps[0]["exit_code"] = 0 if not preflight["blocked"] else 1
        steps[0]["commands"] = ["kubectl get nodes -o json"]
        if members:
            steps[0]["commands"].append(
                " ".join(
                    [
                        *self._upgrade._etcdctl_argv(members[0]["pod"]),
                        "member",
                        "list",
                        "--write-out=json",
                    ]
                )
            )
        for member in members:
            steps[0]["commands"].append(
                " ".join(
                    [
                        *self._upgrade._etcdctl_argv(member["pod"]),
                        "snapshot",
                        "status",
                        request["snapshot_path"],
                        "--write-out=json",
                    ]
                )
            )
        if preflight["blocked"]:
            return self._result(
                request, steps, preflight, performed, "ROLLBACK_PREFLIGHT_BLOCKED"
            )
        performed.append("preflight")

        cluster = ",".join(f"{item['name']}={item['peer_url']}" for item in members)
        swapped: list[dict[str, str]] = []
        for member in members:
            local = member["node"] == local_name
            staged = local or self._stage_snapshot(
                member, request["snapshot_path"], steps[1]
            )
            if not staged:
                steps[1]["detail"] = (
                    f"{request['snapshot_path']} could not be staged on {member['node']}"
                )
                steps[1]["status"] = "failed"
                return self._result(
                    request, steps, preflight, performed, "ROLLBACK_RESTORE_FAILED"
                )
            argv = [
                *self._upgrade._etcdctl_argv(member["pod"]),
                "snapshot",
                "restore",
                request["snapshot_path"],
                f"--data-dir={self._restore_dir}",
                f"--name={member['name']}",
                f"--initial-cluster={cluster}",
                f"--initial-advertise-peer-urls={member['peer_url']}",
            ]
            # kubectl exec reaches a pod on any node from the local admin
            # kubeconfig, so the restore always runs locally and only the file
            # staging and the directory moves happen on the member's host.
            if not self._execute(steps[1], argv, STEP_TIMEOUT_SECONDS):
                return self._result(
                    request, steps, preflight, performed, "ROLLBACK_RESTORE_FAILED"
                )
            if not self._execute(
                steps[1],
                self._on_member(
                    member, ["sudo", "test", "-d", f"{self._restore_dir}/member"], local=local
                ),
                60,
            ):
                steps[1]["status"] = "failed"
                steps[1]["detail"] = (
                    f"{member['node']}: {self._restore_dir}/member was not written"
                )
                return self._result(
                    request, steps, preflight, performed, "ROLLBACK_RESTORE_FAILED"
                )
        steps[1]["status"] = "succeeded"
        performed.append("etcd_restore")

        # Stop every kubelet before any swap: a kubelet left running would
        # restart etcd against the directory that is being moved.
        stop_order = [item for item in members if item["node"] != local_name] + [
            item for item in members if item["node"] == local_name
        ]
        for member in stop_order:
            if not self._execute(
                steps[2],
                self._on_member(
                    member, ["sudo", "systemctl", "stop", "kubelet"],
                    local=member["node"] == local_name,
                ),
                STEP_TIMEOUT_SECONDS,
            ):
                return self._result(
                    request, steps, preflight, performed, "ROLLBACK_KUBELET_STOP_FAILED"
                )
        old_member = f"{DEFAULT_ETCD_DATA_DIR}/member"
        for member in members:
            local = member["node"] == local_name
            if not self._execute(
                steps[2],
                self._on_member(
                    member, ["sudo", "mv", old_member, self._member_backup], local=local
                ),
                STEP_TIMEOUT_SECONDS,
            ):
                self._restore_swapped(swapped)
                return self._result(
                    request, steps, preflight, performed, "ROLLBACK_SWAP_FAILED"
                )
            if not self._execute(
                steps[2],
                self._on_member(
                    member,
                    ["sudo", "mv", f"{self._restore_dir}/member", old_member],
                    local=local,
                ),
                STEP_TIMEOUT_SECONDS,
            ):
                self._execute(
                    steps[2],
                    self._on_member(
                        member,
                        ["sudo", "mv", self._member_backup, old_member],
                        local=local,
                    ),
                    STEP_TIMEOUT_SECONDS,
                )
                self._restore_swapped(swapped)
                steps[2]["detail"] = (
                    f"{member['node']}: restored member could not be moved in; "
                    f"old data kept at {self._member_backup}"
                )
                return self._result(
                    request, steps, preflight, performed, "ROLLBACK_SWAP_FAILED"
                )
            swapped.append(member)
        steps[2]["status"] = "succeeded"
        performed.append("data_swap")

        start_order = [item for item in members if item["node"] == local_name] + [
            item for item in members if item["node"] != local_name
        ]
        for member in start_order:
            if not self._execute(
                steps[3],
                self._on_member(
                    member, ["sudo", "systemctl", "start", "kubelet"],
                    local=member["node"] == local_name,
                ),
                STEP_TIMEOUT_SECONDS,
            ):
                return self._result(
                    request, steps, preflight, performed, "ROLLBACK_KUBELET_START_FAILED"
                )
        api_ok, api_detail = self._wait_for_api()
        steps[3]["detail"] = api_detail
        if not api_ok:
            steps[3]["status"] = "failed"
            return self._result(
                request, steps, preflight, performed, "ROLLBACK_API_UNAVAILABLE"
            )
        steps[3]["status"] = "succeeded"
        performed.append("kubelet_restart")

        healthy, health_detail = self._upgrade._etcd_quorum_healthy(members[0]["pod"])
        steps[4]["commands"].append(
            " ".join(self._upgrade._etcdctl_argv(members[0]["pod"]))
        )
        steps[4]["detail"] = health_detail
        if not healthy:
            steps[4]["status"] = "failed"
            return self._result(
                request, steps, preflight, performed, "ROLLBACK_POSTFLIGHT_FAILED"
            )
        try:
            nodes_after = self._nodes()
        except RollbackExecutionError as error:
            steps[4]["status"] = "failed"
            steps[4]["detail"] = error.detail
            return self._result(
                request, steps, preflight, performed, "ROLLBACK_POSTFLIGHT_FAILED"
            )
        steps[4]["commands"].append("kubectl get nodes -o json")
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

    def _restore_swapped(self, members: list[dict[str, str]]) -> None:
        """Put the pre-rollback member directory back on already-swapped members."""

        local_name = str(getattr(self, "_local_name", "") or "")
        old_member = f"{DEFAULT_ETCD_DATA_DIR}/member"
        for member in members:
            self._run(
                self._on_member(
                    member,
                    ["sudo", "mv", old_member, f"{self._member_backup}.failed-swap"],
                    local=member["node"] == local_name,
                ),
                timeout=STEP_TIMEOUT_SECONDS,
            )
            self._run(
                self._on_member(
                    member,
                    ["sudo", "mv", self._member_backup, old_member],
                    local=member["node"] == local_name,
                ),
                timeout=STEP_TIMEOUT_SECONDS,
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
