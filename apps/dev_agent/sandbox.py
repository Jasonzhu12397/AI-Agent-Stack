"""Command execution inside a constrained sandbox policy.

The in-process runner is the default. It never uses a shell, strips secrets from
the environment, confines the working directory to the project and refuses
commands that are not on the allowlist. An optional Docker runner provides
filesystem isolation when a container runtime is available.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

OUTPUT_LIMIT = 20_000
DEFAULT_TIMEOUT = 300

GIT_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "add",
    "commit",
    "branch",
    "switch",
    "checkout",
    "merge",
    "rebase",
    "init",
    "rev-parse",
    "ls-files",
}
NPM_SUBCOMMANDS = {"test", "run", "audit"}
PYTHON_MODULES = {"pytest", "ruff", "coverage"}
DENIED_TOKENS = {"push", "reset", "--hard", "--force", "deploy", "apply", "destroy"}


class SandboxViolation(RuntimeError):
    """The requested command is outside the sandbox policy."""


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= OUTPUT_LIMIT:
        return text, False
    return text[:OUTPUT_LIMIT] + "\n...[truncated]", True


def validate_command(command: list[str]) -> None:
    if not command:
        raise SandboxViolation("empty command")
    for token in command:
        if token.lower() in DENIED_TOKENS:
            raise SandboxViolation(f"high-risk token is not allowed: {token}")
    program = command[0]
    arguments = command[1:]
    if program == "git":
        if not arguments or arguments[0] not in GIT_SUBCOMMANDS:
            raise SandboxViolation("git subcommand is not allowed")
        if arguments[0] == "branch" and any(flag in arguments for flag in ("-D", "-d")):
            raise SandboxViolation("branch deletion requires manual approval")
    elif program == "npm":
        if not arguments or arguments[0] not in NPM_SUBCOMMANDS:
            raise SandboxViolation("npm subcommand is not allowed")
    elif program in {"python", "python3"} or (
        Path(program).name in {"python", "python3"}
        and os.path.realpath(program) == os.path.realpath(sys.executable)
    ):
        if len(arguments) < 2 or arguments[0] != "-m" or arguments[1] not in PYTHON_MODULES:
            raise SandboxViolation("python may only run the allowlisted modules")
    elif program in {"pytest", "ruff", "go", "pip-audit"}:
        if program == "go" and (not arguments or arguments[0] not in {"test", "build", "vet"}):
            raise SandboxViolation("go subcommand is not allowed")
    else:
        raise SandboxViolation(f"program is not allowed in the sandbox: {program}")


def _environment(root: Path) -> dict[str, str]:
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    interpreter_directory = str(Path(sys.executable).resolve().parent)
    base_path = environment.get("PATH", os.defpath)
    environment["PATH"] = interpreter_directory + os.pathsep + base_path
    environment["PYTHONPATH"] = str(root)
    environment["HOME"] = environment.get("HOME", str(root))
    return environment


def run(command: list[str], root: Path, timeout: int = DEFAULT_TIMEOUT) -> CommandResult:
    validate_command(command)
    root = root.resolve()
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=_environment(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        error = (exc.stderr or "") + f"\ncommand timed out after {timeout}s"
        stdout, truncated = _truncate(str(output))
        stderr, error_truncated = _truncate(str(error))
        return CommandResult(command, 124, stdout, stderr, truncated or error_truncated)
    stdout, truncated_stdout = _truncate(completed.stdout)
    stderr, truncated_stderr = _truncate(completed.stderr)
    return CommandResult(
        command,
        completed.returncode,
        stdout,
        stderr,
        truncated_stdout or truncated_stderr,
    )


class DockerSandbox:
    """Run one allowlisted command inside a throwaway container.

    The project is mounted read-only by default; callers that need to write
    (for example a test run that produces coverage data) must opt in explicitly.
    """

    def __init__(self, image: str = "python:3.12-slim", network: bool = False) -> None:
        self.image = image
        self.network = network

    def command(self, command: list[str], root: Path, writable: bool = False) -> list[str]:
        validate_command(command)
        docker = [
            "docker",
            "run",
            "--rm",
            "--network",
            "bridge" if self.network else "none",
            "--read-only" if not writable else "--tmpfs",
        ]
        if not writable:
            docker.append(f"{root}:/workspace:ro")
        else:
            docker[-1] = "/tmp"
            docker.extend(["-v", f"{root}:/workspace:rw"])
        docker.extend(["-w", "/workspace", self.image, *command])
        return docker

    def check_available(self) -> bool:
        try:
            completed = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0
