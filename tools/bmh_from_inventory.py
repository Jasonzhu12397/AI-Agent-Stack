#!/usr/bin/env python3
"""Render BareMetalHost manifests from a physical-host inventory.

The Metal3 lab is meant for real hardware: the operator describes each machine
once (BMC endpoint, credentials, boot NIC MAC) and this tool turns that
inventory into the Kubernetes objects Ironic needs - one Opaque Secret carrying
the BMC credentials plus one metal3.io/v1alpha1 BareMetalHost per machine.

Nothing here knows about libvirt, vbmc, sushy-tools or virtual machines; there
is no VM to create on real hardware, so no VM data is emitted.

Usage:
    python3 tools/bmh_from_inventory.py --inventory hosts.yaml --out-dir hosts
    python3 tools/bmh_from_inventory.py --inventory hosts.yaml --count
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - PyYAML is a declared dependency
    sys.stderr.write("PyYAML is required: python3 -m pip install 'PyYAML>=6.0,<7.0'\n")
    raise SystemExit(2) from None

SECRET_NAME_SUFFIX = "-bmc"
MANIFEST_NAME = "bmh.yaml"
CLUSTER_ARTIFACT_NAME = "bmhs.yaml"
NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
MAC_PATTERN = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
BMC_SCHEMES = ("redfish", "redfish-virtualmedia", "redfish-uefihttp", "ipmi", "idrac", "ilo5")


class InventoryError(RuntimeError):
    """Raised for anything the operator has to fix in the inventory."""


@dataclass
class Machine:
    name: str
    bmc_address: str
    boot_mac: str
    username: str
    password: str
    boot_mode: str = "UEFI"
    verify_bmc_ca: bool = False
    online: bool = True
    root_device_hints: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


def _fail(path: Path, message: str) -> InventoryError:
    return InventoryError(f"{path}: {message}")


def _require(mapping: dict[str, Any], key: str, path: Path, machine: str) -> Any:
    value = mapping.get(key)
    if value in (None, ""):
        raise _fail(path, f"machine {machine!r} is missing {key!r}")
    return value


def normalise_mac(value: str) -> str:
    """Accept aa:bb:cc:dd:ee:ff, AA-BB-... and aabbccddeeff."""

    candidate = str(value).strip().lower().replace("-", ":")
    if ":" not in candidate and len(candidate) == 12:
        candidate = ":".join(candidate[i : i + 2] for i in range(0, 12, 2))
    if not MAC_PATTERN.match(candidate):
        raise InventoryError(f"{value!r} is not a valid MAC address")
    return candidate


def normalise_bmc_address(value: str) -> str:
    """Ensure the address carries an Ironic driver scheme."""

    candidate = str(value).strip()
    if not candidate:
        raise InventoryError("bmc address is empty")
    if "+" in candidate:
        scheme, remainder = candidate.split("+", 1)
        if scheme not in BMC_SCHEMES:
            raise InventoryError(
                f"unsupported BMC scheme {scheme!r}; expected one of {', '.join(BMC_SCHEMES)}",
            )
        return f"{scheme}+{remainder}"
    if "://" in candidate and candidate.split("://", 1)[0] in BMC_SCHEMES:
        # already an Ironic driver URL such as ipmi://10.0.0.12
        return candidate
    if candidate.startswith(("http://", "https://")):
        return f"redfish+{candidate}"
    if "/redfish/" in candidate or candidate.count(".") >= 1:
        return f"redfish+https://{candidate}"
    raise InventoryError(f"cannot infer a BMC scheme for {candidate!r}; use '<scheme>+<url>'")


def _machine_from_entry(path: Path, entry: dict[str, Any], index: int) -> Machine:
    if not isinstance(entry, dict):
        raise _fail(path, f"machines[{index}] must be a mapping")

    name = str(_require(entry, "name", path, f"machines[{index}]")).strip().lower()
    if not NAME_PATTERN.match(name):
        raise _fail(
            path,
            f"machine name {name!r} is not a valid DNS label (lowercase letters, digits and '-')",
        )
    if len(name) > 40:
        raise _fail(path, f"machine name {name!r} exceeds 40 characters")

    bmc_address = normalise_bmc_address(_require(entry, "bmc_address", path, name))
    boot_mac = normalise_mac(_require(entry, "boot_mac", path, name))
    username = str(_require(entry, "username", path, name))
    password = str(_require(entry, "password", path, name))

    boot_mode = str(entry.get("boot_mode", "UEFI")).strip() or "UEFI"
    hints = entry.get("root_device_hints") or {}
    if not isinstance(hints, dict):
        raise _fail(path, f"machine {name!r}: root_device_hints must be a mapping")

    labels = entry.get("labels") or {}
    if not isinstance(labels, dict):
        raise _fail(path, f"machine {name!r}: labels must be a mapping")
    labels = {str(key): str(value) for key, value in labels.items()}

    return Machine(
        name=name,
        bmc_address=bmc_address,
        boot_mac=boot_mac,
        username=username,
        password=password,
        boot_mode=boot_mode,
        verify_bmc_ca=bool(entry.get("verify_bmc_ca", False)),
        online=bool(entry.get("online", True)),
        root_device_hints=dict(hints),
        labels=labels,
    )


def load_inventory(path: Path) -> list[Machine]:
    """Parse the inventory file into machines, validating every field."""

    if not path.is_file():
        raise _fail(path, "inventory file not found")

    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _fail(path, f"invalid JSON: {exc}") from exc
    else:
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise _fail(path, f"invalid YAML: {exc}") from exc

    if not isinstance(document, dict) or "machines" not in document:
        raise _fail(path, "expected a mapping with a top-level 'machines' list")

    machines = document["machines"]
    if not isinstance(machines, list) or not machines:
        raise _fail(path, "'machines' must be a non-empty list")

    parsed = [_machine_from_entry(path, entry, index) for index, entry in enumerate(machines)]
    names = [machine.name for machine in parsed]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise _fail(path, f"duplicate machine name(s): {', '.join(duplicates)}")
    macs = [machine.boot_mac for machine in parsed]
    duplicate_macs = sorted({mac for mac in macs if macs.count(mac) > 1})
    if duplicate_macs:
        raise _fail(path, f"duplicate boot_mac value(s): {', '.join(duplicate_macs)}")

    return parsed


def secret_manifest(machine: Machine) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": machine.name + SECRET_NAME_SUFFIX},
        "type": "Opaque",
        "stringData": {"username": machine.username, "password": machine.password},
    }


def baremetalhost_manifest(machine: Machine) -> dict[str, Any]:
    bmc: dict[str, Any] = {
        "address": machine.bmc_address,
        "credentialsName": machine.name + SECRET_NAME_SUFFIX,
        "disableCertificateVerification": not machine.verify_bmc_ca,
    }
    spec: dict[str, Any] = {
        "online": machine.online,
        "bootMACAddress": machine.boot_mac,
        "bootMode": machine.boot_mode,
        "bmc": bmc,
    }
    if machine.root_device_hints:
        spec["rootDeviceHints"] = machine.root_device_hints
    metadata: dict[str, Any] = {"name": machine.name}
    if machine.labels:
        metadata["labels"] = machine.labels
    return {
        "apiVersion": "metal3.io/v1alpha1",
        "kind": "BareMetalHost",
        "metadata": metadata,
        "spec": spec,
    }


def render_documents(machines: Iterable[Machine]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for machine in machines:
        documents.append(secret_manifest(machine))
        documents.append(baremetalhost_manifest(machine))
    return documents


def write_manifests(machines: Sequence[Machine], out_dir: Path) -> tuple[Path, Path]:
    """Write one combined manifest plus one file per machine."""

    out_dir.mkdir(parents=True, exist_ok=True)
    # Only remove files this tool owns; anything else in the directory is left
    # untouched because it may belong to the operator.
    owned = list(out_dir.glob(f"*{MANIFEST_NAME}")) + list(out_dir.glob(CLUSTER_ARTIFACT_NAME))
    for stale in owned:
        stale.unlink()

    for machine in machines:
        per_machine = out_dir / f"{machine.name}-{MANIFEST_NAME}"
        per_machine.write_text(
            yaml.safe_dump_all(
                render_documents([machine]), sort_keys=False, default_flow_style=False,
            ),
            encoding="utf-8",
        )

    combined = out_dir / CLUSTER_ARTIFACT_NAME
    combined.write_text(
        yaml.safe_dump_all(
            render_documents(machines), sort_keys=False, default_flow_style=False,
        ),
        encoding="utf-8",
    )
    return combined, out_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render BareMetalHost manifests for physical machines (no libvirt, no VMs).",
    )
    parser.add_argument(
        "--inventory", required=True, type=Path,
        help="YAML or JSON inventory of physical machines",
    )
    parser.add_argument("--out-dir", type=Path, help="directory where BMH manifests are written")
    parser.add_argument(
        "--count", action="store_true", help="print the number of machines and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        machines = load_inventory(args.inventory)
        if args.count:
            print(len(machines))
            return 0
        if args.out_dir is None:
            raise InventoryError("--out-dir is required unless --count is used")
        combined, directory = write_manifests(machines, args.out_dir)
        print(f"rendered {len(machines)} BareMetalHost manifest(s)")
        print(f"combined manifest: {combined}")
        print(f"per-machine files: {directory}")
        return 0
    except InventoryError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
