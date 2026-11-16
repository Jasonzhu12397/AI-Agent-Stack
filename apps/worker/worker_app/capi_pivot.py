from __future__ import annotations

import hashlib
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


def _inventory_digest(objects: tuple[CapiObjectRef, ...]) -> str:
    payload = [
        {
            "kind": item.kind,
            "name": item.name,
            "namespace": item.namespace,
            "uid": item.uid,
        }
        for item in sorted(
            objects,
            key=lambda value: (value.namespace or "", value.kind, value.name),
        )
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _save_journal(journal, operation_id: str, record: dict) -> None:
    try:
        journal.save_state(operation_id, record)
    except Exception as exc:
        raise PivotError(
            "Unable to persist CAPI pivot journal; inspect both clusters",
            retry_safe=False,
        ) from exc


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
    execute: bool = False,
    operation_id: str | None = None,
    target_agent_ack: Callable[[dict], bool] | None = None,
    journal=None,
) -> dict:
    if not source_kubeconfig or not target_kubeconfig or not namespace:
        raise ValueError("Source kubeconfig, target kubeconfig and namespace are required")
    if source_kubeconfig == target_kubeconfig:
        raise ValueError("Source and target kubeconfigs must be different")
    if command_timeout <= 0 or verification_timeout <= 0 or poll_interval <= 0:
        raise ValueError("Timeouts and poll_interval must be greater than zero")

    if execute and (
        not isinstance(operation_id, str)
        or not operation_id
        or len(operation_id) > 128
        or target_agent_ack is None
        or journal is None
        or not callable(getattr(journal, "load_state", None))
        or not callable(getattr(journal, "save_state", None))
    ):
        raise PivotError(
            "Real CAPI pivot requires operation_id, target Agent acknowledgement "
            "and a durable journal",
            retry_safe=True,
        )

    objects = tuple(inventory)
    if not objects:
        raise ValueError("CAPI pivot inventory must not be empty")
    identities = [(item.kind, item.namespace, item.name) for item in objects]
    if len(set(identities)) != len(identities):
        raise ValueError("CAPI pivot inventory contains duplicate object identities")
    if any(not item.uid for item in objects):
        raise ValueError("Every CAPI inventory entry must include its original UID")
    if any(item.namespace not in (None, namespace) for item in objects):
        raise ValueError("CAPI pivot inventory contains objects outside the requested namespace")

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

    result = {
        "state": "simulation_ready",
        "pivot_executed": False,
        "pivot_simulated": True,
        "execution_mode": "preflight_verified_simulation",
        "dry_run_passed": True,
        "inventory_objects": len(objects),
        "inventory_sha256": _inventory_digest(objects),
        "ownership_transferred": False,
        "side_effects": False,
        "target_agent_acknowledgement_required": True,
        "postflight_required": True,
    }
    if not execute:
        return result

    digest = result["inventory_sha256"]
    try:
        previous = journal.load_state(operation_id)
    except Exception as exc:
        raise PivotError(
            "Unable to read CAPI pivot journal; refusing mutation",
            retry_safe=True,
        ) from exc
    if previous is not None:
        if not isinstance(previous, dict) or previous.get("inventory_sha256") != digest:
            raise PivotVerificationError(
                "Pivot operation ID is already bound to a different inventory",
                retry_safe=False,
            )
        state = previous.get("state")
        if state == "completed":
            if verify_pivot_inventory(
                runner,
                source_kubeconfig,
                target_kubeconfig,
                objects,
                timeout=command_timeout,
            ):
                return {
                    **result,
                    "state": "completed",
                    "pivot_executed": True,
                    "pivot_simulated": False,
                    "execution_mode": "clusterctl_move_verified",
                    "ownership_transferred": True,
                    "side_effects": True,
                    "target_agent_acknowledged": True,
                }
            raise PivotVerificationError(
                "Journal says completed but ownership verification no longer matches",
                retry_safe=False,
            )
        if state in {"executing", "verifying", "uncertain"}:
            raise PivotVerificationError(
                "Pivot operation has an ambiguous prior execution; inspect both clusters "
                "and reconcile before retry",
                retry_safe=False,
            )
        if state != "preflight_complete":
            raise PivotVerificationError(
                "Pivot journal contains an unsupported state",
                retry_safe=False,
            )
    else:
        _save_journal(
            journal,
            operation_id,
            {
                "state": "preflight_complete",
                "inventory_sha256": digest,
                "namespace": namespace,
                "inventory_objects": len(objects),
            },
        )

    acknowledgement = {
        "operation_id": operation_id,
        "namespace": namespace,
        "inventory_sha256": digest,
        "inventory_objects": len(objects),
    }
    try:
        acknowledged = target_agent_ack(acknowledgement)
    except Exception as exc:
        raise PivotError(
            "Target Agent acknowledgement could not be confirmed; refusing mutation",
            retry_safe=True,
        ) from exc
    if acknowledged is not True:
        raise PivotError(
            "Target Agent did not acknowledge CAPI pivot ownership",
            retry_safe=True,
        )
    _save_journal(
        journal,
        operation_id,
        {
            **acknowledgement,
            "state": "executing",
            "target_agent_acknowledged": True,
        },
    )

    try:
        _run(
            runner,
            base_command,
            command_timeout,
            "clusterctl move",
        )
    except PivotError:
        try:
            journal.save_state(
                operation_id,
                {
                    **acknowledgement,
                    "state": "uncertain",
                    "target_agent_acknowledged": True,
                },
            )
        except Exception:
            pass
        raise

    _save_journal(
        journal,
        operation_id,
        {
            **acknowledgement,
            "state": "verifying",
            "target_agent_acknowledged": True,
        },
    )
    deadline = monotonic() + verification_timeout
    while True:
        if verify_pivot_inventory(
            runner,
            source_kubeconfig,
            target_kubeconfig,
            objects,
            timeout=command_timeout,
        ):
            _save_journal(
                journal,
                operation_id,
                {
                    **acknowledgement,
                    "state": "completed",
                    "target_agent_acknowledged": True,
                    "ownership_transferred": True,
                },
            )
            return {
                **result,
                "state": "completed",
                "pivot_executed": True,
                "pivot_simulated": False,
                "execution_mode": "clusterctl_move_verified",
                "ownership_transferred": True,
                "side_effects": True,
                "target_agent_acknowledged": True,
            }
        if monotonic() >= deadline:
            try:
                journal.save_state(
                    operation_id,
                    {
                        **acknowledgement,
                        "state": "uncertain",
                        "target_agent_acknowledged": True,
                    },
                )
            except Exception:
                pass
            raise PivotVerificationError(
                "CAPI pivot command completed but ownership transfer was not verified; "
                "reconcile both clusters before retry",
                retry_safe=False,
            )
        sleep(min(poll_interval, max(0.0, deadline - monotonic())))
