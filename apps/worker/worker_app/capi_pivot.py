from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class CapiObjectRef:
    kind: str
    name: str
    uid: str
    namespace: str | None = None

    @classmethod
    def from_dict(cls, value: dict) -> CapiObjectRef:
        metadata = value.get("metadata") or {}
        kind = value.get("kind")
        name = metadata.get("name")
        uid = metadata.get("uid")
        if not all(isinstance(item, str) and item for item in (kind, name, uid)):
            raise ValueError("CAPI inventory entries require kind, metadata.name and metadata.uid")
        return cls(kind=kind, name=name, uid=uid, namespace=metadata.get("namespace"))


class PivotError(RuntimeError):
    def __init__(self, message: str, *, retry_safe: bool = False):
        super().__init__(message)
        self.retry_safe = retry_safe


class PivotCommandError(PivotError):
    pass


class PivotVerificationError(PivotError):
    pass


Runner = Callable[[Sequence[str], int], CommandResult]


def subprocess_runner(command: Sequence[str], timeout: int) -> CommandResult:
    completed = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _run(
    runner: Runner,
    command: Sequence[str],
    timeout: int,
    operation: str,
) -> CommandResult:
    try:
        result = runner(command, timeout)
    except subprocess.TimeoutExpired as exc:
        raise PivotError(
            f"{operation} timed out; inspect both clusters before any retry",
            retry_safe=False,
        ) from exc
    except Exception as exc:
        raise PivotError(
            f"{operation} could not be confirmed; inspect both clusters before any retry",
            retry_safe=False,
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no command output").strip()
        raise PivotCommandError(f"{operation} failed: {detail}", retry_safe=False)
    return result


def _get_object(
    runner: Runner,
    kubeconfig: str,
    item: CapiObjectRef,
    timeout: int,
) -> dict | None:
    command = ["kubectl", "--kubeconfig", kubeconfig, "get", item.kind, item.name]
    if item.namespace:
        command.extend(["--namespace", item.namespace])
    command.extend(["--output", "json"])
    try:
        result = runner(command, timeout)
    except subprocess.TimeoutExpired as exc:
        raise PivotVerificationError(
            "Timed out while verifying CAPI object ownership",
            retry_safe=False,
        ) from exc
    except Exception as exc:
        raise PivotVerificationError(
            "Could not verify CAPI object ownership",
            retry_safe=False,
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").lower()
        if "notfound" in detail or "not found" in detail:
            return None
        raise PivotVerificationError(
            f"Unable to read {item.kind}/{item.name}: "
            f"{(result.stderr or result.stdout).strip()}",
            retry_safe=False,
        )
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise PivotVerificationError(
            f"Invalid JSON while reading {item.kind}/{item.name}",
            retry_safe=False,
        ) from exc


def verify_pivot_inventory(
    runner: Runner,
    source_kubeconfig: str,
    target_kubeconfig: str,
    inventory: Iterable[CapiObjectRef],
    *,
    timeout: int = 30,
) -> bool:
    objects = tuple(inventory)
    if not objects:
        raise ValueError("CAPI pivot inventory must not be empty")
    for item in objects:
        target = _get_object(runner, target_kubeconfig, item, timeout)
        if target is None:
            return False
        target_uid = (target.get("metadata") or {}).get("uid")
        if target_uid != item.uid:
            raise PivotVerificationError(
                f"Target object {item.kind}/{item.name} has a different UID; "
                "refusing to report a successful pivot",
                retry_safe=False,
            )
        source = _get_object(runner, source_kubeconfig, item, timeout)
        if source is not None:
            source_uid = (source.get("metadata") or {}).get("uid")
            if source_uid == item.uid:
                return False
            raise PivotVerificationError(
                f"Source object {item.kind}/{item.name} was replaced with a different UID",
                retry_safe=False,
            )
    return True


def pivot_cluster_api(
    source_kubeconfig: str,
    target_kubeconfig: str,
    namespace: str,
    inventory: Iterable[CapiObjectRef],
    *,
    runner: Runner = subprocess_runner,
    command_timeout: int = 300,
    verification_timeout: int = 300,
    poll_interval: float = 5.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    if not source_kubeconfig or not target_kubeconfig or not namespace:
        raise ValueError("Source kubeconfig, target kubeconfig and namespace are required")
    if source_kubeconfig == target_kubeconfig:
        raise ValueError("Source and target kubeconfigs must be different")
    if command_timeout <= 0 or verification_timeout <= 0 or poll_interval <= 0:
        raise ValueError("Timeouts and poll_interval must be greater than zero")

    objects = tuple(inventory)
    if not objects:
        raise ValueError("CAPI pivot inventory must not be empty")
    identities = [(item.kind, item.namespace, item.name) for item in objects]
    if len(set(identities)) != len(identities):
        raise ValueError("CAPI pivot inventory contains duplicate object identities")
    if any(not item.uid for item in objects):
        raise ValueError("Every CAPI inventory entry must include its original UID")

    base_command = [
        "clusterctl",
        "move",
        "--kubeconfig",
        source_kubeconfig,
        "--to-kubeconfig",
        target_kubeconfig,
        "--namespace",
        namespace,
    ]
    _run(
        runner,
        [*base_command, "--dry-run"],
        command_timeout,
        "clusterctl move dry-run",
    )
    _run(runner, base_command, command_timeout, "clusterctl move")

    deadline = monotonic() + verification_timeout
    last_error = None
    while monotonic() < deadline:
        try:
            if verify_pivot_inventory(
                runner,
                source_kubeconfig,
                target_kubeconfig,
                objects,
                timeout=min(command_timeout, 30),
            ):
                return {
                    "state": "verified",
                    "pivot_executed": True,
                    "verified_objects": len(objects),
                    "verification": "target_uid_matches_and_source_objects_absent",
                }
        except PivotVerificationError as exc:
            last_error = exc
            raise
        sleep(min(poll_interval, max(deadline - monotonic(), 0)))

    detail = f": {last_error}" if last_error else ""
    raise PivotVerificationError(
        "clusterctl move returned success but post-pivot ownership verification "
        f"did not complete before the deadline{detail}; do not rerun clusterctl move",
        retry_safe=False,
    )
