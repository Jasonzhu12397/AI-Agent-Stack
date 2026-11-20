"""Real profiling helpers on top of cProfile.

Static rules find obvious waste; a profile finds where time actually went. Only
the allowlisted ``python -m cProfile -o <file> -m pytest`` shape can be executed
in the sandbox, so profiling cannot become an arbitrary code runner.
"""

from __future__ import annotations

import cProfile
import io
import pstats
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sandbox import run


@dataclass
class Hotspot:
    function: str
    location: str
    calls: int
    total_seconds: float
    cumulative_seconds: float

    def as_dict(self) -> dict:
        return {
            "function": self.function,
            "location": self.location,
            "calls": self.calls,
            "total_seconds": round(self.total_seconds, 6),
            "cumulative_seconds": round(self.cumulative_seconds, 6),
        }


@dataclass
class ProfileReport:
    total_calls: int
    total_seconds: float
    hotspots: list[Hotspot]
    returncode: int = 0
    output_tail: str = ""

    def as_dict(self) -> dict:
        return {
            "total_calls": self.total_calls,
            "total_seconds": round(self.total_seconds, 6),
            "returncode": self.returncode,
            "output_tail": self.output_tail,
            "hotspots": [hotspot.as_dict() for hotspot in self.hotspots],
        }


def _report_from_stats(stats: pstats.Stats, top: int) -> ProfileReport:
    entries = []
    total_calls = 0
    total_seconds = 0.0
    for (filename, line, function), values in stats.stats.items():
        calls, _, total, cumulative = values[:4]
        total_calls += calls
        total_seconds += total
        entries.append((cumulative, total, calls, filename, line, function))
    entries.sort(reverse=True)
    hotspots = [
        Hotspot(
            function=function,
            location=f"{filename}:{line}",
            calls=calls,
            total_seconds=total,
            cumulative_seconds=cumulative,
        )
        for cumulative, total, calls, filename, line, function in entries[:top]
    ]
    return ProfileReport(total_calls=total_calls, total_seconds=total_seconds, hotspots=hotspots)


def profile_callable(
    function: Callable[..., Any], *args: Any, top: int = 15, **kwargs: Any
) -> ProfileReport:
    profiler = cProfile.Profile()
    profiler.enable()
    try:
        function(*args, **kwargs)
    finally:
        profiler.disable()
    stats = pstats.Stats(profiler)
    stats.sort_stats("cumulative")
    return _report_from_stats(stats, top)


def load_profile(path: str | Path, top: int = 15) -> ProfileReport:
    stats = pstats.Stats(str(path))
    stats.sort_stats("cumulative")
    return _report_from_stats(stats, top)


def render_report(report: ProfileReport) -> str:
    lines = [
        f"total calls: {report.total_calls}",
        f"total time: {report.total_seconds:.3f}s",
        "",
        "| cumulative | total | calls | function | location |",
        "|---|---|---|---|---|",
    ]
    for hotspot in report.hotspots:
        lines.append(
            f"| {hotspot.cumulative_seconds:.4f}s | {hotspot.total_seconds:.4f}s | "
            f"{hotspot.calls} | {hotspot.function} | `{hotspot.location}` |"
        )
    return "\n".join(lines)


def profile_pytest(root: Path, top: int = 15, timeout: int = 300) -> ProfileReport:
    root = root.resolve()
    directory = root / ".dev-agent" / "profiles"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "pytest.prof"
    result = run(
        [
            sys.executable,
            "-m",
            "cProfile",
            "-o",
            str(output),
            "-m",
            "pytest",
            "-q",
        ],
        root,
        timeout=timeout,
    )
    if not output.exists():
        return ProfileReport(
            total_calls=0,
            total_seconds=0.0,
            hotspots=[],
            returncode=result.returncode,
            output_tail=(result.stdout + result.stderr)[-2000:],
        )
    report = load_profile(output, top)
    report.returncode = result.returncode
    report.output_tail = (result.stdout + result.stderr)[-2000:]
    return report


def render_text_stats(path: str | Path, limit: int = 25) -> str:
    stream = io.StringIO()
    stats = pstats.Stats(str(path), stream=stream)
    stats.sort_stats("cumulative").print_stats(limit)
    return stream.getvalue()
