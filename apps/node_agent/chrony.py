from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


class ChronyError(RuntimeError):
    pass


MANAGED_BEGIN = "# BEGIN AI-AGENT-STACK MANAGED CHRONY"
MANAGED_END = "# END AI-AGENT-STACK MANAGED CHRONY"


def _run(runner, command: list[str], timeout: int = 60):
    try:
        result = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChronyError("CHRONY_COMMAND_EXECUTION_FAILED:" + command[0]) from exc
    if result.returncode != 0:
        raise ChronyError("CHRONY_COMMAND_FAILED:" + command[0])
    return result.stdout or ""


def _read_os_release(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ChronyError("CHRONY_OS_RELEASE_UNAVAILABLE") from exc
    values = {}
    for line in lines:
        match = re.fullmatch(r'([A-Z_]+)=(?:"([^"]*)"|\'([^\']*)\'|([^\s]*))', line)
        if match:
            values[match.group(1)] = next(
                value for value in match.groups()[1:] if value is not None
            )
    return values


def _validate_sources(role: str, sources: list[str], ironic_internal_ip: str | None):
    if role not in {"eph", "target_control_plane", "target_worker"}:
        raise ChronyError("CHRONY_NODE_ROLE_INVALID")
    if not isinstance(sources, list) or not sources or len(sources) > 8:
        raise ChronyError("CHRONY_SOURCES_INVALID")
    if any(not isinstance(source, str) or not source.strip() for source in sources):
        raise ChronyError("CHRONY_SOURCE_INVALID")
    normalized = [source.strip() for source in sources]
    if len(set(normalized)) != len(normalized):
        raise ChronyError("CHRONY_SOURCE_DUPLICATED")
    hostname_pattern = re.compile(
        r"(?i)^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$"
    )
    for source in normalized:
        try:
            ipaddress.ip_address(source)
        except ValueError:
            if not hostname_pattern.fullmatch(source):
                raise ChronyError("CHRONY_SOURCE_INVALID") from None
    if role == "target_worker":
        if not isinstance(ironic_internal_ip, str):
            raise ChronyError("WORKER_NTP_MUST_USE_IRONIC_INTERNAL_IP")
        try:
            expected = ipaddress.ip_address(ironic_internal_ip)
            actual = [ipaddress.ip_address(source) for source in normalized]
        except ValueError as exc:
            raise ChronyError("WORKER_NTP_SOURCE_MUST_BE_IP") from exc
        if not expected.is_private or actual != [expected]:
            raise ChronyError("WORKER_NTP_MUST_USE_ONLY_IRONIC_INTERNAL_IP")
    elif role == "target_control_plane":
        if ironic_internal_ip is not None:
            try:
                ipaddress.ip_address(ironic_internal_ip)
            except ValueError as exc:
                raise ChronyError("IRONIC_INTERNAL_IP_INVALID") from exc
    return normalized


def _render_config(
    role: str,
    sources: list[str],
    allowed_clients: list[str],
    ironic_internal_ip: str | None = None,
) -> str:
    lines = [MANAGED_BEGIN]
    if role == "target_control_plane":
        if not isinstance(ironic_internal_ip, str):
            raise ChronyError("CONTROL_PLANE_IRONIC_INTERNAL_IP_REQUIRED")
        try:
            internal_ip = ipaddress.ip_address(ironic_internal_ip)
        except ValueError as exc:
            raise ChronyError("IRONIC_INTERNAL_IP_INVALID") from exc
        if not internal_ip.is_private:
            raise ChronyError("IRONIC_INTERNAL_IP_MUST_BE_PRIVATE")
        for source in sources:
            lines.append("server " + source + " iburst")
        lines.append("bindaddress " + str(internal_ip))
        if not isinstance(allowed_clients, list) or not allowed_clients:
            raise ChronyError("CONTROL_PLANE_NTP_ALLOWED_CLIENTS_REQUIRED")
        networks = []
        for item in allowed_clients:
            try:
                network = ipaddress.ip_network(item, strict=False)
            except (TypeError, ValueError) as exc:
                raise ChronyError("CONTROL_PLANE_NTP_ALLOWED_CLIENT_INVALID") from exc
            if not network.is_private:
                raise ChronyError("CONTROL_PLANE_NTP_ALLOWED_CLIENT_MUST_BE_PRIVATE")
            networks.append(str(network))
        for network in sorted(set(networks)):
            lines.append("allow " + network)
        lines.append("port 123")
        lines.append("cmdport 0")
    else:
        for source in sources:
            lines.append("server " + source + " iburst")
        lines.extend(["makestep 1.0 3", "rtcsync"])
    lines.append(MANAGED_END)
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_managed_config(path: Path, block: str):
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    original = "\n".join(
        line
        for line in original.splitlines()
        if not re.match(
            r"^\s*(?:server|pool|peer|sourcedir|confdir|include)\s+",
            line,
            re.IGNORECASE,
        )
    )
    if MANAGED_BEGIN in original:
        start = original.index(MANAGED_BEGIN)
        end = original.find(MANAGED_END, start)
        if end < 0:
            raise ChronyError("CHRONY_MANAGED_CONFIG_BLOCK_CORRUPT")
        end += len(MANAGED_END)
        updated = original[:start] + block.rstrip() + original[end:]
    else:
        backup = path.with_name(path.name + ".ai-agent-stack.bak")
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
        updated = original.rstrip() + ("\n\n" if original.strip() else "") + block
    _atomic_write(path, updated.rstrip() + "\n")


def _service_name(os_id: str) -> str:
    server_distros = {
        "sles",
        "opensuse",
        "opensuse-leap",
        "rhel",
        "centos",
        "rocky",
        "almalinux",
    }
    return "chronyd" if os_id in server_distros else "chrony"


def _verify_sync(runner, chronyc: str, expected_source: str | None, max_offset_seconds: float):
    sources = _run(runner, [chronyc, "-n", "sources"], timeout=20)
    tracking = _run(runner, [chronyc, "tracking"], timeout=20)
    selected = []
    for line in sources.splitlines():
        if re.match(r"^\s*\^\*", line):
            fields = line.split()
            if len(fields) >= 2:
                selected.append(fields[1])
    if not selected:
        raise ChronyError("CHRONY_NO_SELECTED_TIME_SOURCE")
    if expected_source is not None:
        try:
            expected = str(ipaddress.ip_address(expected_source))
            actual = [str(ipaddress.ip_address(value)) for value in selected]
        except ValueError as exc:
            raise ChronyError("CHRONY_SELECTED_SOURCE_INVALID") from exc
        if actual != [expected]:
            raise ChronyError("WORKER_NOT_SYNCED_FROM_IRONIC_INTERNAL_IP")
    leap = re.search(r"(?im)^Leap status\s*:\s*(.+?)\s*$", tracking)
    offset = re.search(r"(?im)^System time\s*:\s*([+-]?[0-9.eE+-]+)\s+seconds", tracking)
    if not leap or leap.group(1).strip().lower() != "normal":
        raise ChronyError("CHRONY_LEAP_STATUS_NOT_NORMAL")
    if not offset:
        raise ChronyError("CHRONY_SYSTEM_OFFSET_UNAVAILABLE")
    try:
        offset_value = abs(float(offset.group(1)))
    except ValueError as exc:
        raise ChronyError("CHRONY_SYSTEM_OFFSET_INVALID") from exc
    if offset_value > max_offset_seconds:
        raise ChronyError("CHRONY_SYSTEM_OFFSET_EXCEEDED")
    return {
        "synchronized": True,
        "selected_sources": selected,
        "leap_status": leap.group(1).strip(),
        "system_offset_seconds": offset_value,
    }


def configure_chrony(
    node_role: str,
    sources: list[str],
    *,
    ironic_internal_ip: str | None = None,
    allowed_clients: list[str] | None = None,
    root: str = "/",
    max_offset_seconds: float = 0.5,
    runner=None,
    executable_lookup=None,
    require_root: bool = True,
) -> dict[str, Any]:
    runner = runner or subprocess.run
    lookup = executable_lookup or shutil.which
    if require_root and os.geteuid() != 0:
        raise ChronyError("CHRONY_ROOT_REQUIRED")
    if (
        not isinstance(max_offset_seconds, (int, float))
        or isinstance(max_offset_seconds, bool)
        or max_offset_seconds <= 0
    ):
        raise ChronyError("CHRONY_MAX_OFFSET_INVALID")
    normalized = _validate_sources(node_role, sources, ironic_internal_ip)
    root_path = Path(root).resolve()
    os_release = _read_os_release(root_path / "etc/os-release")
    os_id = os_release.get("ID", "").lower()
    if os_id in {"ubuntu", "debian"}:
        config_path = root_path / "etc/chrony/chrony.conf"
        package_command = ["apt-get", "install", "-y", "chrony"]
    elif os_id in {"sles", "opensuse", "opensuse-leap", "rhel", "centos", "rocky", "almalinux"}:
        config_path = root_path / "etc/chrony.conf"
        package_command = (
            ["zypper", "--non-interactive", "install", "chrony"]
            if os_id in {"sles", "opensuse", "opensuse-leap"}
            else ["dnf", "install", "-y", "chrony"]
        )
    else:
        raise ChronyError("CHRONY_OS_UNSUPPORTED")
    if root_path != Path("/"):
        raise ChronyError("CHRONY_OFFLINE_ROOT_EXECUTION_UNSUPPORTED")
    package_manager = package_command[0]
    if not lookup(package_manager):
        raise ChronyError("CHRONY_PACKAGE_MANAGER_NOT_FOUND")
    _run(runner, package_command, timeout=600)
    block = _render_config(
        node_role,
        normalized,
        allowed_clients or [],
        ironic_internal_ip=ironic_internal_ip,
    )
    _write_managed_config(config_path, block)
    service = _service_name(os_id)
    systemctl = lookup("systemctl")
    chronyc = lookup("chronyc")
    if not systemctl or not chronyc:
        raise ChronyError("CHRONY_RUNTIME_BINARIES_MISSING")
    _run(runner, [systemctl, "enable", "--now", service], timeout=60)
    active = _run(runner, [systemctl, "is-active", service], timeout=20).strip()
    if active != "active":
        raise ChronyError("CHRONY_SERVICE_NOT_ACTIVE")
    expected = ironic_internal_ip if node_role == "target_worker" else None
    verification = _verify_sync(runner, chronyc, expected, float(max_offset_seconds))
    listener_verified = False
    if node_role == "target_control_plane":
        ss = lookup("ss")
        if not ss:
            raise ChronyError("CHRONY_LISTENER_CHECK_BINARY_MISSING")
        listener_output = _run(runner, [ss, "-H", "-lun"], timeout=20)
        endpoint = str(ipaddress.ip_address(ironic_internal_ip)) + ":123"
        listener_verified = any(
            endpoint in line for line in listener_output.splitlines()
        )
        if not listener_verified:
            raise ChronyError("CONTROL_PLANE_INTERNAL_NTP_LISTENER_NOT_READY")
    return {
        "configured": True,
        "node_role": node_role,
        "os_id": os_id,
        "config_path": str(config_path),
        "service": service,
        "sources": normalized,
        "external_ntp_allowed": node_role != "target_worker",
        "internal_ntp_server_enabled": node_role == "target_control_plane",
        "internal_ntp_listener_verified": listener_verified,
        **verification,
    }



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    try:
        if config_path.is_symlink() or not config_path.is_file():
            raise ChronyError("CHRONY_CONFIG_FILE_INVALID")
        stat = config_path.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o022 or stat.st_size > 65536:
            raise ChronyError("CHRONY_CONFIG_FILE_PERMISSIONS_INVALID")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        allowed = {
            "node_role",
            "sources",
            "ironic_internal_ip",
            "allowed_clients",
            "max_offset_seconds",
        }
        if not isinstance(config, dict) or set(config) - allowed:
            raise ChronyError("CHRONY_CONFIG_SCHEMA_INVALID")
        result = configure_chrony(
            config.get("node_role"),
            config.get("sources"),
            ironic_internal_ip=config.get("ironic_internal_ip"),
            allowed_clients=config.get("allowed_clients"),
            max_offset_seconds=config.get("max_offset_seconds", 0.5),
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, ChronyError) as exc:
        error = str(exc) if isinstance(exc, ChronyError) else "CHRONY_CONFIG_READ_FAILED"
        print(json.dumps({"configured": False, "error": error}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
