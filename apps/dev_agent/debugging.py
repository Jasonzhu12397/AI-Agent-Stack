"""Crash and log analysis for Python tracebacks and gdb backtraces."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TRACEBACK_FRAME = re.compile(r'File "(.+)", line (\d+), in (.+)')
GDB_FRAME = re.compile(r"#(\d+)\s+(?:0x[0-9a-f]+\s+in\s+)?(.+?)\s+at\s+(.+):(\d+)")
EXCEPTION_LINE = re.compile(
    r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt|Exit))(?::?\s*(.*))?$"
)
ROOT_CAUSE_HINTS = {
    "KeyError": "A mapping lookup used a key that is not present; guard the lookup or fix the key.",
    "AttributeError": (
        "An object is None or has an unexpected type; check the value before attribute access."
    ),
    "TypeError": "Argument or operand types do not match the call site; trace the value's origin.",
    "FileNotFoundError": "A path is missing or resolved relative to the wrong working directory.",
    "ConnectionError": "A network dependency is unreachable or lacks a timeout/retry policy.",
    "SyntaxError": "The module failed to import; fix the reported line before running tests.",
    "IndexError": "A sequence index is out of range; validate length or use a safe accessor.",
    "ZeroDivisionError": "A denominator can be zero; add an explicit guard.",
}


@dataclass
class StackFrame:
    file: str
    line: int
    function: str
    code: str = ""


@dataclass
class CrashReport:
    exception_type: str = ""
    message: str = ""
    frames: list[StackFrame] = field(default_factory=list)
    chained: list[str] = field(default_factory=list)

    @property
    def origin(self) -> StackFrame | None:
        return self.frames[-1] if self.frames else None

    def as_dict(self) -> dict:
        return {
            "exception_type": self.exception_type,
            "message": self.message,
            "frames": [frame.__dict__ for frame in self.frames],
            "origin": self.origin.__dict__ if self.origin else None,
            "chained": self.chained,
            "hint": ROOT_CAUSE_HINTS.get(self.exception_type, ""),
        }


def analyse_traceback(text: str) -> CrashReport:
    report = CrashReport()
    lines = text.splitlines()
    frames: list[StackFrame] = []
    index = 0
    while index < len(lines):
        match = TRACEBACK_FRAME.search(lines[index])
        if match:
            code = ""
            if index + 1 < len(lines):
                candidate = lines[index + 1].strip()
                if candidate and not candidate.startswith(("File ", "Traceback")):
                    code = candidate
            frames.append(StackFrame(match.group(1), int(match.group(2)), match.group(3), code))
        elif "During handling of the above exception" in lines[index]:
            report.chained.extend(f"{frame.file}:{frame.line} {frame.function}" for frame in frames)
            frames = []
        else:
            candidate = lines[index].strip()
            match_exception = EXCEPTION_LINE.match(candidate)
            if match_exception and not candidate.startswith(("File ", "Traceback")):
                report.exception_type = match_exception.group(1).rsplit(".", 1)[-1]
                report.message = (match_exception.group(2) or "").strip()
        index += 1
    report.frames = [frame for frame in frames if frame.function != "<module>" or True]
    return report


def parse_gdb_backtrace(text: str) -> list[StackFrame]:
    frames: list[StackFrame] = []
    for line in text.splitlines():
        match = GDB_FRAME.search(line)
        if match:
            frames.append(
                StackFrame(
                    file=match.group(3),
                    line=int(match.group(4)),
                    function=match.group(2).strip(),
                )
            )
    return frames


def summarise(report: CrashReport) -> str:
    if not report.exception_type and not report.frames:
        return "No Python traceback or gdb frame was recognised."
    parts = [f"{report.exception_type}: {report.message}".strip()]
    if report.origin:
        parts.append(
            f"origin {report.origin.file}:{report.origin.line} in {report.origin.function}"
        )
        if report.origin.code:
            parts.append(f"code: {report.origin.code}")
    hint = ROOT_CAUSE_HINTS.get(report.exception_type)
    if hint:
        parts.append(f"hint: {hint}")
    return "\n".join(parts)
