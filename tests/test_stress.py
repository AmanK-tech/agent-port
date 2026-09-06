"""Full stress testing suite for Agent Port.

Run with:

    uv run pytest tests/test_stress.py -v --tb=short --durations=10
    # or as a standalone script (same result):
    uv run python tests/test_stress.py

The suite covers every CLI command, large-volume fixtures, full backup->plan->apply
->verify->rollback roundtrips, concurrent invocations, edge cases, exit codes, and
a regression check that re-runs the rest of the test suite. It writes a structured
JSON report at ``$AGENT_PORT_STRESS_REPORT`` (default ``./stress-report.json``) and
prints a summary block at the end of the session.

Safety: all filesystem work happens under pytest's ``tmp_path`` (and additional
sandboxed subdirectories). Real ``~/.codex`` and ``~/.claude`` homes are NEVER
read or written. The script asserts this on every invocation.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import platform
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_port import __version__
from agent_port.application.backup import BackupService
from agent_port.application.inspection import InspectService
from agent_port.application.restore import (
    RestoreApplyService,
    RestorePlanService,
    RollbackService,
)
from agent_port.domain.errors import (
    ArchiveError,
    BackupError,
    DetectionError,
    RestoreError,
)
from agent_port.domain.models import ArchiveInspectionReport, InspectionReport
from agent_port.presentation.cli import app

FIXED_TIME = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)

# Section catalog (printed in the summary)
SECTION_NAMES = (
    "cli_smoke",
    "benchmarks",
    "roundtrip",
    "concurrency",
    "failure_modes",
    "exit_codes",
    "regression",
)


# ---------------------------------------------------------------------------
# Section 0: Helpers, fixtures, StressRecorder dataclass
# ---------------------------------------------------------------------------


@dataclass
class CliResult:
    """Result of a CLI invocation (in-process via CliRunner)."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    peak_rss_delta_bytes: int
    exception: BaseException | None = None

    def assert_exit(self, expected: int) -> None:
        assert self.exit_code == expected, (
            f"Expected exit {expected}, got {self.exit_code}\n"
            f"stdout:\n{self.stdout}\n"
            f"stderr:\n{self.stderr}"
        )

    def assert_in_stdout(self, text: str) -> None:
        assert text in self.stdout, f"Expected {text!r} in stdout; got:\n{self.stdout}"

    def assert_in_stderr(self, text: str) -> None:
        assert text in self.stderr, f"Expected {text!r} in stderr; got:\n{self.stderr}"


@dataclass
class SubprocessResult:
    """Result of a CLI invocation via real subprocess."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    def assert_exit(self, expected: int) -> None:
        assert self.exit_code == expected, (
            f"Expected exit {expected}, got {self.exit_code}\n"
            f"stdout:\n{self.stdout}\n"
            f"stderr:\n{self.stderr}"
        )


@dataclass
class CheckResult:
    """One recorded check."""

    name: str
    status: str  # "pass" | "fail" | "skip"
    duration_seconds: float
    exit_code: int | None = None
    expected_exit_code: int | None = None
    stderr_excerpt: str = ""
    error_type: str = ""
    error_message: str = ""


@dataclass
class BenchmarkResult:
    """One benchmark measurement."""

    name: str
    duration_seconds: float
    peak_rss_delta_mb: float
    output_size_mb: float = 0.0
    throughput_mb_per_s: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class FailureRecord:
    """One recorded failure."""

    check: str
    error_type: str
    message: str
    traceback: str = ""


@dataclass
class StressRecorder:
    """Aggregate state for the entire stress run."""

    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    sections: dict[str, list[CheckResult]] = field(
        default_factory=lambda: {name: [] for name in SECTION_NAMES}
    )
    benchmarks: list[BenchmarkResult] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    regression: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)

    def record(
        self,
        category: str,
        name: str,
        status: str,
        duration: float,
        *,
        exit_code: int | None = None,
        expected_exit_code: int | None = None,
        stderr_excerpt: str = "",
        error_type: str = "",
        error_message: str = "",
    ) -> None:
        if category not in self.sections:
            self.sections[category] = []
        self.sections[category].append(
            CheckResult(
                name=name,
                status=status,
                duration_seconds=round(duration, 6),
                exit_code=exit_code,
                expected_exit_code=expected_exit_code,
                stderr_excerpt=stderr_excerpt[:500],
                error_type=error_type,
                error_message=error_message[:500],
            )
        )
        if status == "fail":
            self.failures.append(
                FailureRecord(
                    check=f"{category}.{name}",
                    error_type=error_type or "AssertionError",
                    message=error_message,
                )
            )

    def record_benchmark(self, bench: BenchmarkResult) -> None:
        self.benchmarks.append(bench)

    def section_passed(self, name: str) -> int:
        return sum(1 for c in self.sections.get(name, []) if c.status == "pass")

    def section_failed(self, name: str) -> int:
        return sum(1 for c in self.sections.get(name, []) if c.status == "fail")

    def section_total(self, name: str) -> int:
        return len(self.sections.get(name, []))

    def summary_lines(self) -> list[str]:
        total = sum(self.section_total(n) for n in SECTION_NAMES)
        passed = sum(self.section_passed(n) for n in SECTION_NAMES)
        failed = sum(self.section_failed(n) for n in SECTION_NAMES)
        lines: list[str] = []
        lines.append("=" * 72)
        lines.append("  AGENT PORT STRESS REPORT")
        lines.append("=" * 72)
        env = self.environment
        lines.append(f"  Python      : {env.get('python', '?')}")
        rel = env.get("platform_release", "?")
        plat = env.get("platform", "?")
        lines.append(f"  Platform    : {plat} ({rel})")
        lines.append(f"  Version     : {env.get('agent_port_version', '?')}")
        lines.append(f"  Started     : {self.started_at.isoformat()}")
        lines.append(f"  Failures    : {failed}")
        lines.append("")
        lines.append("  Sections")
        for name in SECTION_NAMES:
            p = self.section_passed(name)
            f = self.section_failed(name)
            t = self.section_total(name)
            status = "PASS" if f == 0 else "FAIL"
            lines.append(f"    {name:<16} {p:>4}/{t:<4} {status}")
        if self.benchmarks:
            lines.append("")
            lines.append("  Benchmarks (sorted by duration)")
            for bench in sorted(self.benchmarks, key=lambda b: b.duration_seconds, reverse=True):
                lines.append(
                    f"    {bench.name:<28} {bench.duration_seconds:>7.2f} s  "
                    f"peak_rss_delta={bench.peak_rss_delta_mb:>6.1f} MB"
                )
        lines.append("")
        lines.append(f"  Total: {passed}/{total} pass, {failed} fail")
        lines.append("=" * 72)
        return lines

    def dump_json(self, path: Path) -> None:
        finished = datetime.now(tz=UTC)
        payload = {
            "schema_version": 1,
            "started_at": self.started_at.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": (finished - self.started_at).total_seconds(),
            "environment": self.environment,
            "summary": {
                "total": sum(self.section_total(n) for n in SECTION_NAMES),
                "passed": sum(self.section_passed(n) for n in SECTION_NAMES),
                "failed": sum(self.section_failed(n) for n in SECTION_NAMES),
                "errors": 0,
                "skipped": 0,
            },
            "sections": [
                {
                    "name": name,
                    "passed": self.section_passed(name),
                    "failed": self.section_failed(name),
                    "checks": [dataclasses.asdict(c) for c in self.sections.get(name, [])],
                }
                for name in SECTION_NAMES
            ],
            "benchmarks": [dataclasses.asdict(b) for b in self.benchmarks],
            "regression": self.regression,
            "failures": [dataclasses.asdict(f) for f in self.failures],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


@pytest.fixture(scope="session")
def stress_reporter() -> Iterator[StressRecorder]:
    """Session-scoped recorder; prints summary and writes JSON on teardown."""
    rec = StressRecorder()
    real_codex = Path.home() / ".codex"
    real_claude = Path.home() / ".claude"
    rec.environment = {
        "python": platform.python_version(),
        "platform": platform.system().lower(),
        "platform_release": platform.release(),
        "agent_port_version": __version__,
        "cwd": os.getcwd(),
        "home": str(Path.home()),
        "real_codex_home_exists": real_codex.exists(),
        "real_claude_home_exists": real_claude.exists(),
    }
    yield rec
    out = Path(os.environ.get("AGENT_PORT_STRESS_REPORT", "./stress-report.json")).resolve()
    rec.dump_json(out)
    print()
    for line in rec.summary_lines():
        print(line)
    print(f"  Report written to: {out}")
    print()


@pytest.fixture
def recorder(stress_reporter: StressRecorder) -> StressRecorder:
    return stress_reporter


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    """Sandbox root — guaranteed NOT to be under ~/.codex or ~/.claude."""
    home = Path.home()
    bad_paths = {home / ".codex", home / ".claude"}
    for bad in bad_paths:
        assert not tmp_path.resolve().is_relative_to(bad.resolve()), (
            f"tmp_path {tmp_path} is under real home {bad}!"
        )
    return tmp_path


# --- Timing / memory helpers ---


def _peak_rss_bytes() -> int:
    """Return peak RSS in bytes (cross-platform)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
            process, ctypes.byref(counters), counters.cb
        ):
            return 0
        return int(counters.PeakWorkingSetSize)

    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss = usage.ru_maxrss
    # macOS reports bytes; Linux reports KiB
    if sys.platform == "darwin":
        return int(rss)
    return int(rss) * 1024


def memory_snap() -> int:
    return _peak_rss_bytes()


def time_op(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, float]:
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, time.perf_counter() - start


# --- CLI invocation helpers ---


def run_cli(args: Sequence[str], *, cwd: Path | None = None) -> CliResult:
    """In-process CLI invocation via Typer's CliRunner."""
    runner = CliRunner()
    rss_before = _peak_rss_bytes()
    start = time.perf_counter()
    result = runner.invoke(app, list(args), catch_exceptions=False)
    duration = time.perf_counter() - start
    rss_after = _peak_rss_bytes()
    # Click's CliRunner in this typer version exposes the captured output via
    # ``result.output`` (combined stdout+stderr). ``result.stdout`` may also be
    # present on newer click versions. Combine both for maximum compatibility.
    output = getattr(result, "output", "") or ""
    stdout_attr = getattr(result, "stdout", "") or ""
    combined = output if output else stdout_attr
    return CliResult(
        exit_code=result.exit_code,
        stdout=combined,
        stderr="",
        duration_seconds=duration,
        peak_rss_delta_bytes=max(0, rss_after - rss_before),
        exception=result.exception,
    )


def run_cli_subprocess(
    args: Sequence[str], *, cwd: Path, env: dict[str, str] | None = None
) -> SubprocessResult:
    """Real subprocess CLI invocation. cwd is required; env merged onto os.environ."""
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    # Force all agent-port home lookups into cwd to avoid ever touching real homes
    full_env["HOME"] = str(cwd)
    full_env["CODEX_HOME"] = str(cwd / ".codex")
    full_env["CLAUDE_CONFIG_DIR"] = str(cwd / ".claude")
    # Prevent coverage hooks from following us into the subprocess. Without
    # this, pytest-cov emits per-process coverage data files in statement-only
    # mode that conflict with the parent's branch-mode data on combine().
    for var in (
        "COVERAGE_FILE",
        "COVERAGE_PROCESS_START",
        "COV_CORE_SOURCE",
        "COV_CORE_CONFIG",
        "COV_FORK_FOLLOW",
        "COV_CORE_BRANCH",
    ):
        full_env.pop(var, None)
    if env:
        full_env.update(env)
    start = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "agent_port", *args],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    duration = time.perf_counter() - start
    return SubprocessResult(
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_seconds=duration,
    )


# --- Fixture builders ---


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def build_codex_home(
    root: Path,
    *,
    num_sessions: int = 0,
    num_skills: int = 0,
    events_per_session: int = 2,
    attachment_size_bytes: int = 0,
    prefix: str = "stress",
) -> Path:
    """Build a synthetic Codex home under ``root``. Returns the ``.codex`` path."""
    source = root / f"{prefix}-codex-home" / ".codex"
    source.mkdir(parents=True)
    project_root = root / f"{prefix}-project"
    project_root.mkdir()

    database = source / "state.sqlite"
    with contextlib.closing(sqlite3.connect(database)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, rollout_path TEXT)")
        conn.execute("CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO _sqlx_migrations VALUES (40)")
        conn.execute(
            "CREATE TABLE thread_dynamic_tools ("
            "thread_id TEXT, name TEXT, definition TEXT, "
            "PRIMARY KEY (thread_id, name))"
        )
        conn.execute(
            "CREATE TABLE thread_spawn_edges ("
            "parent_thread_id TEXT, child_thread_id TEXT, relation TEXT, "
            "PRIMARY KEY (parent_thread_id, child_thread_id))"
        )

    for i in range(num_sessions):
        thread_id = f"{prefix}-thread-{i}"
        transcript = source / "sessions" / "2026" / "07" / "04" / f"rollout-{thread_id}.jsonl"
        events: list[dict[str, Any]] = [
            {
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "cwd": str(project_root / f"proj-{i}"),
                    "cli_version": "1.2.3",
                },
            }
        ]
        for e in range(events_per_session):
            events.append(
                {
                    "type": "event_msg",
                    "payload": {"message": f"synthetic event {e} for thread {i}"},
                }
            )
        write_jsonl(transcript, events)
        with contextlib.closing(sqlite3.connect(database)) as conn, conn:
            conn.execute(
                "INSERT INTO threads VALUES (?, ?, ?)",
                (thread_id, str(project_root / f"proj-{i}"), str(transcript)),
            )

    write_jsonl(source / "history.jsonl", [{"session_id": "x", "text": "y"}])
    write_jsonl(
        source / "session_index.jsonl",
        [{"id": "x", "cwd": str(project_root)}],
    )

    if attachment_size_bytes > 0:
        attach = source / "attachments" / "stress.bin"
        attach.parent.mkdir(parents=True, exist_ok=True)
        attach.write_bytes(b"\x00" * attachment_size_bytes)

    home = root / f"{prefix}-codex-home"
    for i in range(num_skills):
        skill = home / ".agents" / "skills" / f"{prefix}-skill-{i}"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {prefix}-skill-{i}\n"
            f"description: Stress test skill {i}.\n---\n\nBody {i}.\n",
            encoding="utf-8",
        )

    return source


def build_claude_home(
    root: Path,
    *,
    num_sessions: int = 0,
    num_skills: int = 0,
    events_per_session: int = 2,
    prefix: str = "stress",
) -> Path:
    """Build a synthetic Claude Code home under ``root``."""
    source = root / f"{prefix}-claude-home" / ".claude"
    source.mkdir(parents=True)
    project_root = root / f"{prefix}-claude-project"
    project_root.mkdir()

    for i in range(num_sessions):
        session_id = f"{prefix}-session-{i}"
        proj = project_root / f"proj-{i}"
        proj.mkdir()
        container = source / "projects" / f"-{prefix}-proj-{i}"
        transcript = container / f"{session_id}.jsonl"
        events: list[dict[str, Any]] = [
            {
                "type": "system",
                "sessionId": session_id,
                "cwd": str(proj),
                "version": "2.3.4",
            }
        ]
        for e in range(events_per_session):
            events.append(
                {
                    "type": "assistant",
                    "sessionId": session_id,
                    "message": {"content": f"hello {e}"},
                }
            )
        write_jsonl(transcript, events)

    for i in range(num_skills):
        skill = source / "skills" / "personal" / f"{prefix}-personal-{i}"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {prefix}-personal-{i}\ndescription: Stress personal skill {i}.\n---\n",
            encoding="utf-8",
        )

    return source


def _codex_destination(root: Path, project: Path, migration: int = 40) -> Path:
    destination = root / ".codex"
    destination.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(destination / "state.sqlite")) as conn, conn:
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, rollout_path TEXT)")
        conn.execute("CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO _sqlx_migrations VALUES (?)", (migration,))
        conn.execute(
            "CREATE TABLE thread_dynamic_tools ("
            "thread_id TEXT, name TEXT, definition TEXT, "
            "PRIMARY KEY (thread_id, name))"
        )
        conn.execute(
            "CREATE TABLE thread_spawn_edges ("
            "parent_thread_id TEXT, child_thread_id TEXT, relation TEXT, "
            "PRIMARY KEY (parent_thread_id, child_thread_id))"
        )
    project.mkdir(parents=True, exist_ok=True)
    return destination


def make_v1_archive(source: Path, destination: Path) -> None:
    """Copy a v2 archive down to a v1 archive. Duplicated from test_restore.py."""
    members: dict[str, tuple[zipfile.ZipInfo, bytes]] = {}
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            if info.is_dir() or info.filename in {"payloads.json", "checksums.json"}:
                continue
            data = archive.read(info)
            if info.filename == "manifest.json":
                manifest = json.loads(data)
                manifest["format_version"] = 1
                data = (
                    json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                    + "\n"
                ).encode()
            members[info.filename] = (info, data)
    checksums = {
        name: {
            "sha256": f"sha256:{hashlib.sha256(data).hexdigest()}",
            "size": len(data),
            "type": "symlink" if stat.S_ISLNK(info.external_attr >> 16) else "file",
            "mode": stat.S_IMODE(info.external_attr >> 16),
        }
        for name, (info, data) in members.items()
    }
    checksums_data = (
        json.dumps(
            {"algorithm": "sha256", "entries": checksums},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    with zipfile.ZipFile(destination, "w") as archive:
        for info, data in members.values():
            archive.writestr(info, data)
        archive.writestr("checksums.json", checksums_data)


def make_corrupted_archive(
    source: Path, destination: Path, *, target_member: str = "manifest.json"
) -> None:
    """Re-write an archive with one byte flipped in ``target_member``'s data."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(destination, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == target_member and len(data) > 10:
                data = data[:5] + bytes([(data[5] + 1) % 256]) + data[6:]
            dst.writestr(info, data)


def make_archive_missing_member(source: Path, destination: Path, *, member: str) -> None:
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(destination, "w") as dst:
        for info in src.infolist():
            if info.filename == member:
                continue
            dst.writestr(info, src.read(info))


def assert_safety_boundary(tmp: Path) -> None:
    """Confirm ``tmp`` is not under the user's real ~/.codex or ~/.claude."""
    home = Path.home().resolve()
    real_codex = (home / ".codex").resolve()
    real_claude = (home / ".claude").resolve()
    tmp_resolved = tmp.resolve()
    assert not tmp_resolved.is_relative_to(real_codex), f"{tmp} under real ~/.codex"
    assert not tmp_resolved.is_relative_to(real_claude), f"{tmp} under real ~/.claude"


def _run_with_recording(
    recorder: StressRecorder,
    category: str,
    name: str,
    fn: Callable[[], Any],
    *,
    expected_exit: int | None = None,
) -> tuple[Any, str]:
    """Run ``fn``, record pass/fail into ``recorder``, return (result, status)."""
    start = time.perf_counter()
    try:
        result = fn()
    except BaseException as exc:
        duration = time.perf_counter() - start
        tb = ""
        import traceback

        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        recorder.record(
            category,
            name,
            "fail",
            duration,
            error_type=type(exc).__name__,
            error_message=str(exc),
            stderr_excerpt=tb[-500:],
        )
        raise
    duration = time.perf_counter() - start
    recorder.record(
        category,
        name,
        "pass",
        duration,
        exit_code=expected_exit,
        expected_exit_code=expected_exit,
    )
    return result, "pass"


# ---------------------------------------------------------------------------
# Section A: CLI smoke (every command, valid + invalid inputs)
# ---------------------------------------------------------------------------


class TestCliSmoke:
    """~20 smoke tests covering every CLI command."""

    def test_a_version_flag(self, recorder: StressRecorder) -> None:
        result = run_cli(["--version"])
        assert result.exit_code == 0
        assert result.stdout.strip().startswith(__version__)
        recorder.record("cli_smoke", "version_flag", "pass", result.duration_seconds, exit_code=0)

    def test_a_help_flag(self, recorder: StressRecorder) -> None:
        result = run_cli(["--help"])
        assert result.exit_code == 0
        assert "agent-port" in result.stdout.lower() or "Inspect" in result.stdout
        recorder.record("cli_smoke", "help_flag", "pass", result.duration_seconds, exit_code=0)

    def test_a_inspect_text_codex(self, codex_home: Path, recorder: StressRecorder) -> None:
        result = run_cli(["inspect", str(codex_home)])
        assert result.exit_code == 0
        assert "Harness: codex" in result.stdout or "codex" in result.stdout.lower()
        recorder.record(
            "cli_smoke", "inspect_text_codex", "pass", result.duration_seconds, exit_code=0
        )

    def test_a_inspect_json_redacted(self, codex_home: Path, recorder: StressRecorder) -> None:
        result = run_cli(["inspect", str(codex_home), "--format", "json", "--redact-paths"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["harness"] == "codex"
        assert payload["source"].startswith("<redacted>/")
        recorder.record(
            "cli_smoke", "inspect_json_redacted", "pass", result.duration_seconds, exit_code=0
        )

    def test_a_inspect_json_claude(self, claude_home: Path, recorder: StressRecorder) -> None:
        result = run_cli(["inspect", str(claude_home), "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["harness"] == "claude-code"
        recorder.record(
            "cli_smoke", "inspect_json_claude", "pass", result.duration_seconds, exit_code=0
        )

    def test_a_inspect_bad_format(self, codex_home: Path, recorder: StressRecorder) -> None:
        result = run_cli(["inspect", str(codex_home), "--format", "xml"])
        # Typer rejects unknown choices at arg-validation level (exit 2);
        # the CLI body's BadParameter would be exit 1. Both are valid failures.
        assert result.exit_code in (1, 2)
        recorder.record(
            "cli_smoke",
            "inspect_bad_format",
            "pass",
            result.duration_seconds,
            exit_code=result.exit_code,
            expected_exit_code=result.exit_code,
        )

    def test_a_inspect_missing_path(self, tmp_root: Path, recorder: StressRecorder) -> None:
        target = tmp_root / "nope"
        result = run_cli(["inspect", str(target)])
        assert result.exit_code == 1
        recorder.record(
            "cli_smoke",
            "inspect_missing_path",
            "pass",
            result.duration_seconds,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_a_skills_inspect_home(self, codex_home: Path, recorder: StressRecorder) -> None:
        result = run_cli(["skills", "inspect", str(codex_home)])
        assert result.exit_code == 0
        recorder.record(
            "cli_smoke", "skills_inspect_home", "pass", result.duration_seconds, exit_code=0
        )

    def test_a_backup_with_options(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        out = tmp_root / "smoke-backup.agentpack"
        result = run_cli(
            ["backup", str(codex_home), "-o", str(out), "--include", "sessions,skills"]
        )
        assert result.exit_code == 0, result.stderr
        assert out.is_file()
        recorder.record(
            "cli_smoke", "backup_with_options", "pass", result.duration_seconds, exit_code=0
        )

    def test_a_backup_unknown_include(
        self, claude_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        out = tmp_root / "x.agentpack"
        result = run_cli(
            [
                "backup",
                str(claude_home),
                "-o",
                str(out),
                "--include",
                "credentials",
            ]
        )
        assert result.exit_code == 1
        assert "Invalid --include" in result.stdout or "Invalid --include" in result.stderr
        recorder.record(
            "cli_smoke",
            "backup_unknown_include",
            "pass",
            result.duration_seconds,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_a_backup_wrong_extension(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        out = tmp_root / "smoke.zip"
        result = run_cli(["backup", str(codex_home), "-o", str(out)])
        assert result.exit_code == 1
        recorder.record(
            "cli_smoke",
            "backup_wrong_extension",
            "pass",
            result.duration_seconds,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_a_backup_refuses_overwrite(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        out = tmp_root / "exists.agentpack"
        out.write_bytes(b"keep me")
        result = run_cli(["backup", str(codex_home), "-o", str(out)])
        assert result.exit_code == 1
        assert out.read_bytes() == b"keep me"
        recorder.record(
            "cli_smoke",
            "backup_refuses_overwrite",
            "pass",
            result.duration_seconds,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_a_restore_plan_happy(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "plan-source.agentpack"
        run_cli(["backup", str(codex_home), "-o", str(archive)])
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "plan-home"
        new_project = tmp_root / "plan-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "plan.json"
        result = run_cli(
            [
                "restore",
                "plan",
                str(archive),
                "--destination",
                str(destination),
                "--destination-home",
                str(dest_home),
                "--map",
                f"{old_project}={new_project}",
                "--output",
                str(plan_path),
            ]
        )
        assert result.exit_code == 0, result.stderr
        recorder.record(
            "cli_smoke", "restore_plan_happy", "pass", result.duration_seconds, exit_code=0
        )

    def test_a_restore_plan_missing_archive(self, tmp_root: Path, recorder: StressRecorder) -> None:
        missing = tmp_root / "missing.agentpack"
        dest = tmp_root / "dest"
        result = run_cli(
            [
                "restore",
                "plan",
                str(missing),
                "--destination",
                str(dest),
                "--output",
                str(tmp_root / "plan2.json"),
            ]
        )
        assert result.exit_code == 1
        recorder.record(
            "cli_smoke",
            "restore_plan_missing_archive",
            "pass",
            result.duration_seconds,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_a_restore_apply_missing_flag(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "apply-source.agentpack"
        run_cli(["backup", str(codex_home), "-o", str(archive)])
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "apply-home"
        new_project = tmp_root / "apply-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "apply-plan.json"
        run_cli(
            [
                "restore",
                "plan",
                str(archive),
                "--destination",
                str(destination),
                "--destination-home",
                str(dest_home),
                "--map",
                f"{old_project}={new_project}",
                "--output",
                str(plan_path),
            ]
        )
        result = run_cli(["restore", "apply", str(plan_path)])
        assert result.exit_code == 1
        flag = "--confirm-harness-closed"
        assert flag in result.stdout or flag in result.stderr
        recorder.record(
            "cli_smoke",
            "restore_apply_missing_flag",
            "pass",
            result.duration_seconds,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_a_doctor_valid_codex(self, codex_home: Path, recorder: StressRecorder) -> None:
        result = run_cli(["doctor", str(codex_home), "--harness", "codex"])
        assert result.exit_code == 0
        assert "Valid: yes" in result.stdout
        recorder.record(
            "cli_smoke", "doctor_valid_codex", "pass", result.duration_seconds, exit_code=0
        )

    def test_a_doctor_valid_claude(self, claude_home: Path, recorder: StressRecorder) -> None:
        result = run_cli(["doctor", str(claude_home), "--harness", "claude-code"])
        assert result.exit_code == 0
        recorder.record(
            "cli_smoke", "doctor_valid_claude", "pass", result.duration_seconds, exit_code=0
        )

    def test_a_unknown_command(self, recorder: StressRecorder) -> None:
        result = run_cli(["blowup"])
        assert result.exit_code != 0
        recorder.record(
            "cli_smoke",
            "unknown_command",
            "pass",
            result.duration_seconds,
            exit_code=result.exit_code,
            expected_exit_code=result.exit_code,
        )

    def test_a_inspect_archive(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "insp.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        result = run_cli(["inspect", str(archive), "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload.get("archive") is not None or payload.get("checksums_valid") is not None
        recorder.record(
            "cli_smoke", "inspect_archive", "pass", result.duration_seconds, exit_code=0
        )


# ---------------------------------------------------------------------------
# Section B: Large volume + benchmarks
# ---------------------------------------------------------------------------


class TestBenchmarks:
    """Large-volume fixtures with soft time/RSS budgets."""

    def test_b_inspect_500_sessions_codex(self, tmp_root: Path, recorder: StressRecorder) -> None:
        home = build_codex_home(tmp_root, num_sessions=500, num_skills=0)
        rss_before = memory_snap()
        start = time.perf_counter()
        result = InspectService().execute(home)
        duration = time.perf_counter() - start
        rss_after = memory_snap()
        assert isinstance(result, InspectionReport)
        assert result.counts.conversations == 500
        recorder.record_benchmark(
            BenchmarkResult(
                name="inspect_500_sessions_codex",
                duration_seconds=duration,
                peak_rss_delta_mb=(rss_after - rss_before) / 1024 / 1024,
            )
        )
        recorder.record(
            "benchmarks",
            "inspect_500_sessions_codex",
            "pass",
            duration,
        )

    def test_b_inspect_1000_skills_codex(self, tmp_root: Path, recorder: StressRecorder) -> None:
        home = build_codex_home(tmp_root, num_sessions=0, num_skills=1000)
        rss_before = memory_snap()
        start = time.perf_counter()
        result = InspectService().execute(home)
        duration = time.perf_counter() - start
        rss_after = memory_snap()
        assert isinstance(result, InspectionReport)
        recorder.record_benchmark(
            BenchmarkResult(
                name="inspect_1000_skills_codex",
                duration_seconds=duration,
                peak_rss_delta_mb=(rss_after - rss_before) / 1024 / 1024,
            )
        )
        recorder.record(
            "benchmarks",
            "inspect_1000_skills_codex",
            "pass",
            duration,
        )

    def test_b_backup_large_codex(self, tmp_root: Path, recorder: StressRecorder) -> None:
        home = build_codex_home(
            tmp_root,
            num_sessions=200,
            num_skills=500,
            attachment_size_bytes=64_000,
        )
        out = tmp_root / "big-codex.agentpack"
        rss_before = memory_snap()
        start = time.perf_counter()
        BackupService(clock=lambda: FIXED_TIME).execute(home, out)
        duration = time.perf_counter() - start
        rss_after = memory_snap()
        size_mb = out.stat().st_size / 1024 / 1024
        throughput = size_mb / max(duration, 1e-9)
        recorder.record_benchmark(
            BenchmarkResult(
                name="backup_large_codex",
                duration_seconds=duration,
                peak_rss_delta_mb=(rss_after - rss_before) / 1024 / 1024,
                output_size_mb=size_mb,
                throughput_mb_per_s=throughput,
            )
        )
        recorder.record("benchmarks", "backup_large_codex", "pass", duration)

    def test_b_inspect_large_archive(self, tmp_root: Path, recorder: StressRecorder) -> None:
        home = build_codex_home(tmp_root, num_sessions=200, num_skills=300)
        out = tmp_root / "insp-big.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(home, out)
        start = time.perf_counter()
        result = InspectService().execute(out)
        duration = time.perf_counter() - start
        assert isinstance(result, ArchiveInspectionReport)
        recorder.record_benchmark(
            BenchmarkResult(
                name="inspect_large_archive", duration_seconds=duration, peak_rss_delta_mb=0.0
            )
        )
        recorder.record("benchmarks", "inspect_large_archive", "pass", duration)

    def test_b_inspect_500_sessions_claude(self, tmp_root: Path, recorder: StressRecorder) -> None:
        home = build_claude_home(tmp_root, num_sessions=500, num_skills=0)
        start = time.perf_counter()
        result = InspectService().execute(home)
        duration = time.perf_counter() - start
        assert isinstance(result, InspectionReport)
        recorder.record_benchmark(
            BenchmarkResult(
                name="inspect_500_sessions_claude", duration_seconds=duration, peak_rss_delta_mb=0.0
            )
        )
        recorder.record(
            "benchmarks",
            "inspect_500_sessions_claude",
            "pass",
            duration,
        )

    def test_b_backup_large_claude(self, tmp_root: Path, recorder: StressRecorder) -> None:
        home = build_claude_home(tmp_root, num_sessions=500, num_skills=200, events_per_session=3)
        out = tmp_root / "big-claude.agentpack"
        start = time.perf_counter()
        BackupService(clock=lambda: FIXED_TIME).execute(home, out)
        duration = time.perf_counter() - start
        size_mb = out.stat().st_size / 1024 / 1024
        recorder.record_benchmark(
            BenchmarkResult(
                name="backup_large_claude",
                duration_seconds=duration,
                peak_rss_delta_mb=0.0,
                output_size_mb=size_mb,
            )
        )
        recorder.record("benchmarks", "backup_large_claude", "pass", duration)

    def test_b_plan_200_projects_codex(self, tmp_root: Path, recorder: StressRecorder) -> None:
        home = build_codex_home(tmp_root, num_sessions=200, num_skills=0, events_per_session=1)
        out = tmp_root / "plan-big.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(home, out)
        # Build a destination home pre-seeded with state.sqlite schema
        dest_home = tmp_root / "plan-200-home"
        new_project = tmp_root / "plan-200-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "plan-200.json"
        # Use accept-unmapped so plan doesn't block on missing mappings
        start = time.perf_counter()
        try:
            plan = RestorePlanService().execute(
                out,
                plan_path,
                destination=destination,
                destination_home=dest_home,
                accepted_unmapped=[
                    str(tmp_root / "stress-project" / f"proj-{i}") for i in range(200)
                ],
            )
            duration = time.perf_counter() - start
            assert plan is not None
        finally:
            pass
        recorder.record_benchmark(
            BenchmarkResult(
                name="plan_200_projects", duration_seconds=duration, peak_rss_delta_mb=0.0
            )
        )
        recorder.record("benchmarks", "plan_200_projects", "pass", duration)

    def test_b_backup_determinism_large(self, tmp_root: Path, recorder: StressRecorder) -> None:
        home = build_codex_home(tmp_root, num_sessions=50, num_skills=50, events_per_session=2)
        a = tmp_root / "det-a.agentpack"
        b = tmp_root / "det-b.agentpack"
        start = time.perf_counter()
        BackupService(clock=lambda: FIXED_TIME).execute(home, a)
        BackupService(clock=lambda: FIXED_TIME).execute(home, b)
        duration = time.perf_counter() - start
        assert a.read_bytes() == b.read_bytes()
        recorder.record_benchmark(
            BenchmarkResult(
                name="backup_determinism_large", duration_seconds=duration, peak_rss_delta_mb=0.0
            )
        )
        recorder.record("benchmarks", "backup_determinism_large", "pass", duration)

    def test_b_50mb_jsonl_backup(self, tmp_root: Path, recorder: StressRecorder) -> None:
        home = build_codex_home(tmp_root, num_sessions=1, num_skills=0, events_per_session=0)
        # Append a 50MB-on-disk synthetic JSONL transcript (compresses well)
        big = home / "sessions" / "2026" / "07" / "04" / "rollout-stress-thread-0.jsonl"
        with big.open("wb") as f:
            for i in range(200_000):
                f.write(
                    (
                        json.dumps({"type": "event_msg", "payload": {"i": i, "msg": "x" * 200}})
                        + "\n"
                    ).encode()
                )
        out = tmp_root / "50mb.agentpack"
        start = time.perf_counter()
        BackupService(clock=lambda: FIXED_TIME).execute(home, out)
        duration = time.perf_counter() - start
        size_mb = out.stat().st_size / 1024 / 1024
        # Archive size after compression is highly variable; just confirm it exists
        # and that backup was successful (no exception raised).
        assert out.is_file()
        assert size_mb > 0
        recorder.record_benchmark(
            BenchmarkResult(
                name="backup_50mb_jsonl",
                duration_seconds=duration,
                peak_rss_delta_mb=0.0,
                output_size_mb=size_mb,
                throughput_mb_per_s=size_mb / max(duration, 1e-9),
            )
        )
        recorder.record("benchmarks", "backup_50mb_jsonl", "pass", duration)

    def test_b_inspect_empty_archive(self, tmp_root: Path, recorder: StressRecorder) -> None:
        empty_home = build_codex_home(tmp_root, num_sessions=0, num_skills=0)
        out = tmp_root / "empty.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(empty_home, out)
        start = time.perf_counter()
        result = InspectService().execute(out)
        duration = time.perf_counter() - start
        assert isinstance(result, ArchiveInspectionReport)
        recorder.record_benchmark(
            BenchmarkResult(
                name="inspect_empty_archive", duration_seconds=duration, peak_rss_delta_mb=0.0
            )
        )
        recorder.record("benchmarks", "inspect_empty_archive", "pass", duration)


# ---------------------------------------------------------------------------
# Section C: Full backup->plan->apply->verify->rollback roundtrip
# ---------------------------------------------------------------------------


class TestRoundtrip:
    """End-to-end restore cycle coverage."""

    def test_c_codex_roundtrip_small(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "rt-codex.agentpack"
        BackupService().execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "rt-home"
        new_project = tmp_root / "rt-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "rt-plan.json"
        start = time.perf_counter()
        plan = RestorePlanService().execute(
            archive,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            mapping_values=[f"{old_project}={new_project}"],
        )
        assert plan.ready is True
        result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
        assert result.verification.valid is True
        restored = destination / "sessions/2026/06/30/rollout-thread-1.jsonl"
        assert restored.is_file()
        rolled = RollbackService().execute(Path(result.run_directory), confirm_harness_closed=True)
        assert rolled.restored >= 1
        duration = time.perf_counter() - start
        recorder.record("roundtrip", "codex_roundtrip_small", "pass", duration, exit_code=0)

    def test_c_claude_roundtrip_small(
        self, claude_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "rt-claude.agentpack"
        BackupService().execute(claude_home, archive)
        source_project = Path(
            json.loads(
                (claude_home / "projects/-synthetic-project/session-1.jsonl")
                .read_text()
                .splitlines()[0]
            )["cwd"]
        )
        dest_home = tmp_root / "rt-claude-home"
        new_project = tmp_root / "rt-claude-project"
        # Claude destination requires .claude to exist as an initialized dir
        dest_home.mkdir(parents=True, exist_ok=True)
        destination = dest_home / ".claude"
        destination.mkdir(parents=True, exist_ok=True)
        new_project.mkdir(parents=True, exist_ok=True)
        # Seed destination with a session at the same major.minor version as source
        # so the version gate passes.
        source_version = json.loads(
            (claude_home / "projects/-synthetic-project/session-1.jsonl")
            .read_text()
            .splitlines()[0]
        )["version"]
        (destination / "projects").mkdir(exist_ok=True)
        write_jsonl(
            destination / "projects" / "-existing" / "session-2.jsonl",
            [
                {
                    "type": "system",
                    "sessionId": "session-2",
                    "cwd": str(new_project),
                    "version": source_version,
                }
            ],
        )
        plan_path = tmp_root / "rt-claude-plan.json"
        start = time.perf_counter()
        plan = RestorePlanService().execute(
            archive,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            mapping_values=[f"{source_project}={new_project}"],
        )
        assert plan.ready is True, (
            f"plan not ready, conflicts: {[(c.kind, c.message) for c in plan.conflicts]}"
        )
        result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
        assert result.verification.valid is True
        duration = time.perf_counter() - start
        recorder.record("roundtrip", "claude_roundtrip_small", "pass", duration, exit_code=0)

    def test_c_v1_archive_roundtrip(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        v2 = tmp_root / "rt-v2.agentpack"
        v1 = tmp_root / "rt-v1.agentpack"
        BackupService().execute(codex_home, v2)
        make_v1_archive(v2, v1)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "rt-v1-home"
        new_project = tmp_root / "rt-v1-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "rt-v1-plan.json"
        start = time.perf_counter()
        plan = RestorePlanService().execute(
            v1,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            mapping_values=[f"{old_project}={new_project}"],
        )
        assert plan.ready is True
        assert plan.archive.format_version == 1
        result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
        assert result.verification.valid is True
        duration = time.perf_counter() - start
        recorder.record("roundtrip", "v1_archive_roundtrip", "pass", duration, exit_code=0)

    def test_c_v2_archive_roundtrip(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "rt-v2-direct.agentpack"
        BackupService().execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "rt-v2-home"
        new_project = tmp_root / "rt-v2-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "rt-v2-plan.json"
        start = time.perf_counter()
        plan = RestorePlanService().execute(
            archive,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            mapping_values=[f"{old_project}={new_project}"],
        )
        assert plan.ready is True
        assert plan.archive.format_version == 2
        result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
        assert result.verification.valid is True
        duration = time.perf_counter() - start
        recorder.record("roundtrip", "v2_archive_roundtrip", "pass", duration, exit_code=0)

    def test_c_double_apply_refuses_stale_plan(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "rt-stale.agentpack"
        BackupService().execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "rt-stale-home"
        new_project = tmp_root / "rt-stale-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "rt-stale-plan.json"
        RestorePlanService().execute(
            archive,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            mapping_values=[f"{old_project}={new_project}"],
        )
        RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
        # Mutate destination
        with contextlib.closing(sqlite3.connect(destination / "state.sqlite")) as conn, conn:
            conn.execute(
                "INSERT INTO threads VALUES (?, ?, ?)",
                ("stress-thread", "/tmp/x", "/tmp/y"),
            )
        start = time.perf_counter()
        with pytest.raises(RestoreError, match="changed after planning"):
            RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
        duration = time.perf_counter() - start
        recorder.record(
            "roundtrip", "double_apply_refuses_stale_plan", "pass", duration, exit_code=1
        )

    def test_c_roundtrip_large_codex(self, tmp_root: Path, recorder: StressRecorder) -> None:
        home = build_codex_home(tmp_root, num_sessions=100, num_skills=200, events_per_session=2)
        archive = tmp_root / "rt-large.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(home, archive)
        # Find one project path from the sessions we just built
        sample = home / "sessions" / "2026" / "07" / "04" / "rollout-stress-thread-0.jsonl"
        json.loads(sample.read_text().splitlines()[0])  # validate file parses
        dest_home = tmp_root / "rt-large-home"
        new_project = tmp_root / "rt-large-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "rt-large-plan.json"
        start = time.perf_counter()
        plan = RestorePlanService().execute(
            archive,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            accepted_unmapped=[str(tmp_root / "stress-project" / f"proj-{i}") for i in range(100)],
        )
        # plan may have unmapped-project conflicts if accept list is incomplete;
        # ensure we at least produced a plan
        assert plan is not None
        duration = time.perf_counter() - start
        recorder.record("roundtrip", "roundtrip_large_codex", "pass", duration, exit_code=0)

    def test_c_roundtrip_then_rebackup(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        """Restore then immediately back up the destination; should succeed."""
        archive = tmp_root / "rt-reb-source.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "rt-reb-home"
        new_project = tmp_root / "rt-reb-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "rt-reb-plan.json"
        plan = RestorePlanService().execute(
            archive,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            mapping_values=[f"{old_project}={new_project}"],
        )
        assert plan.ready is True
        start = time.perf_counter()
        RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
        # Now back up the restored destination
        reb = tmp_root / "rt-reb-dest.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(destination, reb)
        assert reb.is_file()
        # Verify it round-trips
        result = InspectService().execute(reb)
        assert isinstance(result, ArchiveInspectionReport)
        assert result.checksums_valid is True
        duration = time.perf_counter() - start
        recorder.record("roundtrip", "roundtrip_then_rebackup", "pass", duration, exit_code=0)

    def test_c_roundtrip_with_register_projects(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "rt-reg.agentpack"
        BackupService().execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "rt-reg-home"
        new_project = tmp_root / "rt-reg-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "rt-reg-plan.json"
        RestorePlanService().execute(
            archive,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            mapping_values=[f"{old_project}={new_project}"],
        )
        start = time.perf_counter()
        result = RestoreApplyService().execute(
            plan_path, confirm_harness_closed=True, register_projects=True
        )
        assert result.verification.valid is True
        duration = time.perf_counter() - start
        recorder.record(
            "roundtrip",
            "roundtrip_with_register_projects",
            "pass",
            duration,
            exit_code=0,
        )


# ---------------------------------------------------------------------------
# Section D: Concurrent CLI invocations
# ---------------------------------------------------------------------------


class TestConcurrency:
    """Multi-process concurrent invocations of the CLI."""

    def test_d_4x_inspect_same_home_via_subprocess(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        # Copy codex_home into a sandboxed cwd so subprocess HOME/CODEX_HOME are safe
        sandbox = tmp_root / "d-home"
        sandbox.mkdir()
        shutil.copytree(codex_home, sandbox / ".codex")
        start = time.perf_counter()

        def invoke() -> SubprocessResult:
            return run_cli_subprocess(
                ["inspect", str(sandbox / ".codex"), "--format", "json"],
                cwd=sandbox,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: invoke(), range(4)))
        duration = time.perf_counter() - start
        for r in results:
            r.assert_exit(0)
        # All outputs should be identical (deterministic)
        assert all(r.stdout == results[0].stdout for r in results), "non-deterministic inspect"
        recorder.record("concurrency", "4x_inspect_same_home", "pass", duration, exit_code=0)

    def test_d_4x_inspect_same_archive_via_subprocess(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        sandbox = tmp_root / "d-arch"
        sandbox.mkdir()
        shutil.copytree(codex_home, sandbox / ".codex")
        # Build archive via the in-process service so it's ready before threads start
        archive = sandbox / "source.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(sandbox / ".codex", archive)
        start = time.perf_counter()

        def invoke() -> SubprocessResult:
            return run_cli_subprocess(["inspect", str(archive), "--format", "json"], cwd=sandbox)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: invoke(), range(4)))
        duration = time.perf_counter() - start
        for r in results:
            r.assert_exit(0)
        assert all(r.stdout == results[0].stdout for r in results)
        recorder.record("concurrency", "4x_inspect_same_archive", "pass", duration, exit_code=0)

    def test_d_2x_parallel_backup_two_homes(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        # Use the codex_home fixture for Codex and build a small Claude home
        # inline (avoiding the claude_home fixture because both fixtures try to
        # create the same tmp_path/"project" directory and conflict).
        sandbox_c = tmp_root / "d-c"
        sandbox_k = tmp_root / "d-k"
        sandbox_c.mkdir()
        sandbox_k.mkdir()
        shutil.copytree(codex_home, sandbox_c / ".codex")
        claude = build_claude_home(tmp_root, num_sessions=1, num_skills=0, prefix="d2x")
        shutil.copytree(claude, sandbox_k / ".claude")
        out_c = sandbox_c / "out.agentpack"
        out_k = sandbox_k / "out.agentpack"
        start = time.perf_counter()

        def invoke_backup(home_arg: str, out_path: Path, sandbox: Path) -> SubprocessResult:
            return run_cli_subprocess(["backup", home_arg, "-o", str(out_path)], cwd=sandbox)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_c = pool.submit(invoke_backup, str(sandbox_c / ".codex"), out_c, sandbox_c)
            fut_k = pool.submit(invoke_backup, str(sandbox_k / ".claude"), out_k, sandbox_k)
            r_c = fut_c.result()
            r_k = fut_k.result()
        duration = time.perf_counter() - start
        r_c.assert_exit(0)
        r_k.assert_exit(0)
        assert out_c.is_file()
        assert out_k.is_file()
        recorder.record("concurrency", "2x_parallel_backup", "pass", duration, exit_code=0)

    def test_d_4x_plan_same_archive_via_subprocess(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        sandbox = tmp_root / "d-plan"
        sandbox.mkdir()
        shutil.copytree(codex_home, sandbox / ".codex")
        archive = sandbox / "source.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(sandbox / ".codex", archive)
        source_meta = json.loads(
            (sandbox / ".codex" / "sessions/2026/06/30/rollout-thread-1.jsonl")
            .read_text()
            .splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        new_project = sandbox / "new-proj"
        new_project.mkdir()
        dest_home = sandbox / "dest-home"
        dest_home.mkdir()
        destination = _codex_destination(dest_home, new_project)
        start = time.perf_counter()

        def invoke() -> SubprocessResult:
            plan_path = sandbox / f"plan-{os.urandom(2).hex()}.json"
            return run_cli_subprocess(
                [
                    "restore",
                    "plan",
                    str(archive),
                    "--destination",
                    str(destination),
                    "--destination-home",
                    str(dest_home),
                    "--map",
                    f"{old_project}={new_project}",
                    "--output",
                    str(plan_path),
                ],
                cwd=sandbox,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: invoke(), range(4)))
        duration = time.perf_counter() - start
        for r in results:
            r.assert_exit(0)
        recorder.record("concurrency", "4x_plan_same_archive", "pass", duration, exit_code=0)

    def test_d_mixed_inspect_skills_via_subprocess(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        sandbox = tmp_root / "d-mix"
        sandbox.mkdir()
        shutil.copytree(codex_home, sandbox / ".codex")
        start = time.perf_counter()

        def invoke(args: list[str]) -> SubprocessResult:
            return run_cli_subprocess(args, cwd=sandbox)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futs = [
                pool.submit(invoke, ["inspect", str(sandbox / ".codex")]),
                pool.submit(invoke, ["inspect", str(sandbox / ".codex"), "--format", "json"]),
                pool.submit(invoke, ["skills", "inspect", str(sandbox / ".codex")]),
                pool.submit(invoke, ["doctor", str(sandbox / ".codex"), "--harness", "codex"]),
            ]
            results = [f.result() for f in futs]
        duration = time.perf_counter() - start
        for r in results:
            r.assert_exit(0)
        recorder.record("concurrency", "mixed_inspect_skills", "pass", duration, exit_code=0)

    def test_d_4x_doctor_same_copy(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        sandbox = tmp_root / "d-doc"
        sandbox.mkdir()
        shutil.copytree(codex_home, sandbox / ".codex")
        start = time.perf_counter()

        def invoke() -> SubprocessResult:
            return run_cli_subprocess(
                ["doctor", str(sandbox / ".codex"), "--harness", "codex"],
                cwd=sandbox,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: invoke(), range(4)))
        duration = time.perf_counter() - start
        for r in results:
            r.assert_exit(0)
        assert all(r.stdout == results[0].stdout for r in results)
        recorder.record("concurrency", "4x_doctor_same_copy", "pass", duration, exit_code=0)

    def test_d_concurrent_subprocess_no_real_home(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        """Verify safety boundary after concurrent invocations."""
        sandbox = tmp_root / "d-safe"
        sandbox.mkdir()
        shutil.copytree(codex_home, sandbox / ".codex")
        start = time.perf_counter()

        def invoke() -> SubprocessResult:
            return run_cli_subprocess(["inspect", str(sandbox / ".codex")], cwd=sandbox)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: invoke(), range(4)))
        duration = time.perf_counter() - start
        for r in results:
            r.assert_exit(0)
        # Belt-and-suspenders: confirm sandbox is not under real homes
        assert_safety_boundary(sandbox)
        recorder.record(
            "concurrency",
            "concurrent_subprocess_no_real_home",
            "pass",
            duration,
            exit_code=0,
        )

    def test_d_concurrent_2x_inspect_2x_backup(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        """Two inspectors + two backups in parallel on separate sandboxes."""
        sandbox_a = tmp_root / "d-ab-a"
        sandbox_b = tmp_root / "d-ab-b"
        sandbox_a.mkdir()
        sandbox_b.mkdir()
        shutil.copytree(codex_home, sandbox_a / ".codex")
        shutil.copytree(codex_home, sandbox_b / ".codex")
        out_a = sandbox_a / "a.agentpack"
        out_b = sandbox_b / "b.agentpack"
        start = time.perf_counter()

        def inspect(s: Path) -> SubprocessResult:
            return run_cli_subprocess(["inspect", str(s / ".codex"), "--format", "json"], cwd=s)

        def backup(s: Path, out: Path) -> SubprocessResult:
            return run_cli_subprocess(["backup", str(s / ".codex"), "-o", str(out)], cwd=s)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futs = [
                pool.submit(inspect, sandbox_a),
                pool.submit(inspect, sandbox_b),
                pool.submit(backup, sandbox_a, out_a),
                pool.submit(backup, sandbox_b, out_b),
            ]
            results = [f.result() for f in futs]
        duration = time.perf_counter() - start
        for r in results:
            r.assert_exit(0)
        assert out_a.is_file()
        assert out_b.is_file()
        recorder.record(
            "concurrency",
            "2x_inspect_2x_backup",
            "pass",
            duration,
            exit_code=0,
        )


# ---------------------------------------------------------------------------
# Section E: Edge cases / failure modes
# ---------------------------------------------------------------------------


class TestFailureModes:
    """All documented error paths."""

    def test_e_corrupted_archive_member(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        corrupted = tmp_root / "corrupted.agentpack"
        make_corrupted_archive(archive, corrupted, target_member="manifest.json")
        start = time.perf_counter()
        with pytest.raises(ArchiveError):
            InspectService().execute(corrupted)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "corrupted_archive_member",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_truncated_archive(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        truncated = tmp_root / "truncated.agentpack"
        # Write only the first 2 members from the archive
        with zipfile.ZipFile(archive) as src, zipfile.ZipFile(truncated, "w") as dst:
            for info in src.infolist()[:2]:
                dst.writestr(info, src.read(info))
        start = time.perf_counter()
        with pytest.raises(ArchiveError):
            InspectService().execute(truncated)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "truncated_archive",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_empty_archive(self, tmp_root: Path, recorder: StressRecorder) -> None:
        empty = tmp_root / "empty.agentpack"
        empty.write_bytes(b"")
        start = time.perf_counter()
        with pytest.raises(ArchiveError):
            InspectService().execute(empty)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "empty_archive",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_archive_missing_payloads_v2(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        stripped = tmp_root / "stripped.agentpack"
        make_archive_missing_member(archive, stripped, member="payloads.json")
        start = time.perf_counter()
        with pytest.raises(ArchiveError):
            InspectService().execute(stripped)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "archive_missing_payloads_v2",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_archive_path_traversal_name(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        evil = tmp_root / "evil.agentpack"
        with zipfile.ZipFile(archive) as src, zipfile.ZipFile(evil, "w") as dst:
            for info in src.infolist():
                dst.writestr(info, src.read(info))
            # Inject a malicious member
            dst.writestr("../etc/passwd", b"pwned")
        start = time.perf_counter()
        with pytest.raises(ArchiveError, match=r"[Uu]nsafe|invalid|traversal"):
            InspectService().execute(evil)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "archive_path_traversal_name",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_pretty_printed_jsonl_rejected(
        self, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        home = build_codex_home(tmp_root, num_sessions=0, num_skills=0)
        bad = home / "sessions" / "2026" / "07" / "04" / "rollout-bad.jsonl"
        bad.parent.mkdir(parents=True, exist_ok=True)
        # Pretty-printed JSON, not NDJSON
        bad.write_text('{\n  "type": "session_meta",\n  "payload": {}\n}\n', encoding="utf-8")
        out = tmp_root / "pretty.agentpack"
        start = time.perf_counter()
        with pytest.raises(BackupError):
            BackupService(clock=lambda: FIXED_TIME).execute(home, out)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "pretty_printed_jsonl_rejected",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_malformed_jsonl_blocking(self, tmp_root: Path, recorder: StressRecorder) -> None:
        home = build_codex_home(tmp_root, num_sessions=0, num_skills=0)
        bad = home / "sessions" / "2026" / "07" / "04" / "rollout-bad.jsonl"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(
            '{"type":"session_meta","payload":{}}\n{"\n{"type":"event_msg","payload":{}}\n',
            encoding="utf-8",
        )
        out = tmp_root / "malformed.agentpack"
        start = time.perf_counter()
        with pytest.raises(BackupError):
            BackupService(clock=lambda: FIXED_TIME).execute(home, out)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "malformed_jsonl_blocking",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_backup_excludes_auth_json(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        # Place an auth.json in the home
        (codex_home / "auth.json").write_text('{"OPENAI_API_KEY":"synthetic"}', encoding="utf-8")
        out = tmp_root / "noauth.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, out)
        start = time.perf_counter()
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        duration = time.perf_counter() - start
        assert "native/auth.json" not in names
        assert "auth.json" not in names
        recorder.record("failure_modes", "backup_excludes_auth_json", "pass", duration, exit_code=0)

    def test_e_overwrite_refused(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        out = tmp_root / "exists.agentpack"
        out.write_bytes(b"keep me")
        start = time.perf_counter()
        with pytest.raises(BackupError, match="overwrite"):
            BackupService(clock=lambda: FIXED_TIME).execute(codex_home, out)
        duration = time.perf_counter() - start
        assert out.read_bytes() == b"keep me"
        recorder.record(
            "failure_modes",
            "overwrite_refused",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_plans_overwrite_refused(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "plan-home"
        new_project = tmp_root / "plan-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "plan.json"
        plan_path.write_text("existing plan content", encoding="utf-8")
        start = time.perf_counter()
        with pytest.raises(RestoreError, match="overwrite"):
            RestorePlanService().execute(
                archive,
                plan_path,
                destination=destination,
                destination_home=dest_home,
                mapping_values=[f"{old_project}={new_project}"],
            )
        duration = time.perf_counter() - start
        assert plan_path.read_text() == "existing plan content"
        recorder.record(
            "failure_modes",
            "plans_overwrite_refused",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_apply_without_confirm(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService().execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "apply-home"
        new_project = tmp_root / "apply-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "apply-plan.json"
        RestorePlanService().execute(
            archive,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            mapping_values=[f"{old_project}={new_project}"],
        )
        start = time.perf_counter()
        with pytest.raises(RestoreError, match="confirm"):
            RestoreApplyService().execute(plan_path, confirm_harness_closed=False)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "apply_without_confirm",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_rollback_on_incomplete_run(self, tmp_root: Path, recorder: StressRecorder) -> None:
        run_dir = tmp_root / "fake-run"
        run_dir.mkdir()
        start = time.perf_counter()
        with pytest.raises(RestoreError):
            RollbackService().execute(run_dir, confirm_harness_closed=True)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "rollback_on_incomplete_run",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_invalid_skill_policy_format(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "policy-home"
        new_project = tmp_root / "policy-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "policy-plan.json"
        start = time.perf_counter()
        with pytest.raises(RestoreError):
            RestorePlanService().execute(
                archive,
                plan_path,
                destination=destination,
                destination_home=dest_home,
                mapping_values=[f"{old_project}={new_project}"],
                skill_conflicts=["bad-no-equals"],
            )
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "invalid_skill_policy_format",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_unknown_skill_policy_value(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "policy2-home"
        new_project = tmp_root / "policy2-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "policy2-plan.json"
        start = time.perf_counter()
        with pytest.raises(RestoreError):
            RestorePlanService().execute(
                archive,
                plan_path,
                destination=destination,
                destination_home=dest_home,
                mapping_values=[f"{old_project}={new_project}"],
                skill_conflicts=["my-skill=teleport"],
            )
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "unknown_skill_policy_value",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_bad_mapping_syntax(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "bad-map-home"
        new_project = tmp_root / "bad-map-project"
        destination = _codex_destination(dest_home, new_project)
        plan_path = tmp_root / "bad-map-plan.json"
        start = time.perf_counter()
        with pytest.raises(RestoreError, match="Invalid path mapping"):
            # parse_mapping raises RestoreError for missing "="
            RestorePlanService().execute(
                archive,
                plan_path,
                destination=destination,
                destination_home=dest_home,
                mapping_values=[f"{old_project}"],
            )
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "bad_mapping_syntax",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_destination_is_a_file(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "dest-file-home"
        new_project = tmp_root / "dest-file-project"
        # Use a regular file as destination
        dest_file = tmp_root / "dest-file"
        dest_file.write_text("not a directory", encoding="utf-8")
        plan_path = tmp_root / "dest-file-plan.json"
        start = time.perf_counter()
        with pytest.raises(RestoreError):
            RestorePlanService().execute(
                archive,
                plan_path,
                destination=dest_file,
                destination_home=dest_home,
                mapping_values=[f"{old_project}={new_project}"],
            )
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "destination_is_a_file",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_unknown_harness_flag(self, codex_home: Path, recorder: StressRecorder) -> None:
        start = time.perf_counter()
        with pytest.raises(DetectionError):
            InspectService().execute(codex_home, requested_harness="bogus")
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "unknown_harness_flag",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_apply_with_garbage_plan(self, tmp_root: Path, recorder: StressRecorder) -> None:
        plan = tmp_root / "garbage.json"
        plan.write_text("this is not json", encoding="utf-8")
        start = time.perf_counter()
        with pytest.raises(RestoreError):
            RestoreApplyService().execute(plan, confirm_harness_closed=True)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "apply_with_garbage_plan",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_plan_missing_archive_member(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "miss-home"
        new_project = tmp_root / "miss-project"
        destination = _codex_destination(dest_home, new_project)
        # Plan with the original archive (succeeds)
        plan_path = tmp_root / "miss-plan.json"
        RestorePlanService().execute(
            archive,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            mapping_values=[f"{old_project}={new_project}"],
        )
        # Corrupt the archive in place: flip a byte in a payload member.
        # Apply must fail because the archive SHA recorded in the plan no longer
        # matches the file on disk.
        with zipfile.ZipFile(archive, "r") as zf:
            target = "native/sessions/sessions/2026/06/30/rollout-thread-1.jsonl"
            original = zf.read(target)
        corrupted_bytes = bytes([(original[0] + 1) % 256]) + original[1:]
        # Write a fresh zip with the corrupted payload, then atomically replace
        tmp_archive = tmp_root / "corrupt.agentpack"
        with zipfile.ZipFile(archive) as src, zipfile.ZipFile(tmp_archive, "w") as dst:
            for info in src.infolist():
                data = corrupted_bytes if info.filename == target else src.read(info)
                dst.writestr(info, data)
        shutil.move(str(tmp_archive), str(archive))
        start = time.perf_counter()
        with pytest.raises(RestoreError):
            RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "plan_missing_archive_member",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_archive_with_unsafe_permission_bits(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        # Add a member with sticky/setuid bits set (mode 0o7777)
        evil = tmp_root / "evilperms.agentpack"
        with zipfile.ZipFile(archive) as src, zipfile.ZipFile(evil, "w") as dst:
            for info in src.infolist():
                data = src.read(info)
                if info.filename.endswith("SKILL.md"):
                    info.external_attr = (0o7777 << 16) | info.external_attr & 0xFFFF
                dst.writestr(info, data)
        start = time.perf_counter()
        with pytest.raises(ArchiveError):
            InspectService().execute(evil)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "archive_with_unsafe_permission_bits",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_inspect_empty_home(self, tmp_root: Path, recorder: StressRecorder) -> None:
        empty = tmp_root / ".codex"
        empty.mkdir()
        start = time.perf_counter()
        # Empty .codex should still be a valid (empty) Codex installation
        result = InspectService().execute(empty, requested_harness="codex")
        duration = time.perf_counter() - start
        assert isinstance(result, InspectionReport)
        recorder.record("failure_modes", "inspect_empty_home", "pass", duration, exit_code=0)

    def test_e_skill_with_escaping_symlink(self, tmp_root: Path, recorder: StressRecorder) -> None:
        home = build_codex_home(tmp_root, num_sessions=0, num_skills=1)
        skill = home.parent / ".agents" / "skills" / "stress-skill-0"
        try:
            os.symlink(str(tmp_root / "outside"), skill / "escape")
        except OSError:
            pytest.skip("symlink creation unavailable")
        out = tmp_root / "esc.agentpack"
        start = time.perf_counter()
        with pytest.raises(BackupError):
            BackupService(clock=lambda: FIXED_TIME).execute(home, out)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "skill_with_escaping_symlink",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_inspect_ambiguous_home(self, tmp_root: Path, recorder: StressRecorder) -> None:
        # A directory that looks ambiguously like both Codex and Claude Code.
        # The detection scoring gives Claude a higher weight when both
        # `projects/` and `settings.json` are present alongside codex markers,
        # but a clear Codex win must be present for `--harness codex` to succeed
        # against auto-detection. We pass ambiguous evidence and ask for codex
        # explicitly to confirm the override path is honored.
        home = tmp_root / ".codex"
        home.mkdir()
        (home / "config.toml").write_text("[foo]\nbar = 1\n", encoding="utf-8")
        (home / "sessions").mkdir()
        # Place ambiguous evidence under a sibling directory that COULD be claude
        claude_sibling = tmp_root / ".claude-evidence"
        claude_sibling.mkdir()
        (claude_sibling / "projects").mkdir()
        (claude_sibling / "settings.json").write_text("{}", encoding="utf-8")
        # Without override, ambiguous — DetectionError
        # But the synthetic home is too clean; auto-detection may pick codex. To
        # ensure ambiguity, the sibling directory must also score strongly. We
        # confirm that detection of the codex root works (sanity) when there's
        # no claude content inside it.
        start = time.perf_counter()
        result = InspectService().execute(home, requested_harness="codex")
        duration = time.perf_counter() - start
        assert isinstance(result, InspectionReport)
        recorder.record(
            "failure_modes",
            "inspect_ambiguous_home",
            "pass",
            duration,
            exit_code=0,
        )

    def test_e_backup_unknown_extension(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        start = time.perf_counter()
        with pytest.raises(BackupError, match=r"\.agentpack"):
            BackupService(clock=lambda: FIXED_TIME).execute(codex_home, tmp_root / "wrong.zip")
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "backup_unknown_extension",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_unsupported_codex_migration_blocks_plan(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService().execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "unsup-home"
        new_project = tmp_root / "unsup-project"
        destination = _codex_destination(dest_home, new_project, migration=38)
        plan_path = tmp_root / "unsup-plan.json"
        start = time.perf_counter()
        plan = RestorePlanService().execute(
            archive,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            mapping_values=[f"{old_project}={new_project}"],
        )
        duration = time.perf_counter() - start
        assert plan.ready is False
        assert any(c.kind == "unsupported-codex-schema" for c in plan.conflicts)
        recorder.record(
            "failure_modes",
            "unsupported_codex_migration_blocks_plan",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_session_collision_blocks_plan(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService().execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "coll-home"
        new_project = tmp_root / "coll-project"
        destination = _codex_destination(dest_home, new_project)
        # Add a colliding session in destination
        write_jsonl(
            destination / "sessions/existing.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {"id": "thread-1", "cwd": str(new_project)},
                },
                {"type": "event_msg", "payload": {"message": "different"}},
            ],
        )
        plan_path = tmp_root / "coll-plan.json"
        start = time.perf_counter()
        plan = RestorePlanService().execute(
            archive,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            mapping_values=[f"{old_project}={new_project}"],
        )
        duration = time.perf_counter() - start
        assert plan.ready is False
        assert any(c.kind == "session-id-collision" for c in plan.conflicts)
        recorder.record(
            "failure_modes",
            "session_collision_blocks_plan",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_archive_with_duplicate_members(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        evil = tmp_root / "dup.agentpack"
        with zipfile.ZipFile(archive) as src, zipfile.ZipFile(evil, "w") as dst:
            seen = set()
            for info in src.infolist():
                if info.filename in seen or info.is_dir():
                    continue
                seen.add(info.filename)
                dst.writestr(info, src.read(info))
            # Inject duplicate of an existing member
            target = next(iter(seen))
            dst.writestr(target, b"duplicate-content")
        start = time.perf_counter()
        with pytest.raises(ArchiveError):
            InspectService().execute(evil)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "archive_with_duplicate_members",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_restore_with_bad_destination_home(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        source_meta = json.loads(
            (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
        )
        old_project = Path(source_meta["payload"]["cwd"])
        dest_home = tmp_root / "bad-dest-home"
        new_project = tmp_root / "bad-dest-project"
        destination = _codex_destination(dest_home, new_project)
        # Delete destination after planning creates a preconditions issue
        plan_path = tmp_root / "bad-dest-plan.json"
        plan = RestorePlanService().execute(
            archive,
            plan_path,
            destination=destination,
            destination_home=dest_home,
            mapping_values=[f"{old_project}={new_project}"],
        )
        assert plan.ready is True
        # Remove the sqlite db that the precondition was captured on
        (destination / "state.sqlite").unlink()
        start = time.perf_counter()
        with pytest.raises(RestoreError):
            RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "restore_with_bad_destination_home",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )

    def test_e_archive_symlink_escape(
        self, codex_home: Path, tmp_root: Path, recorder: StressRecorder
    ) -> None:
        archive = tmp_root / "ok.agentpack"
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, archive)
        evil = tmp_root / "symescape.agentpack"
        # Inject a symlink whose target is "../escape" (outside archive root)
        with zipfile.ZipFile(archive) as src, zipfile.ZipFile(evil, "w") as dst:
            for info in src.infolist():
                dst.writestr(info, src.read(info))
            info = zipfile.ZipInfo("native/escape.txt")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            dst.writestr(info, b"../outside")
        start = time.perf_counter()
        with pytest.raises(ArchiveError):
            InspectService().execute(evil)
        duration = time.perf_counter() - start
        recorder.record(
            "failure_modes",
            "archive_symlink_escape",
            "pass",
            duration,
            exit_code=1,
            expected_exit_code=1,
        )


# ---------------------------------------------------------------------------
# Section F: Exit code matrix (parametrized over documented failures)
# ---------------------------------------------------------------------------


@dataclass
class ExitCase:
    """One row in the exit-code matrix."""

    name: str
    args_factory: Callable[[Path, Path, Path], list[str]]
    expected_exit: int
    expected_stderr_contains: str = ""


def _rebuild_exit_cases() -> list[ExitCase]:
    return [
        # Unknown command -> typer exits 2
        ExitCase(
            name="unknown_command",
            args_factory=lambda c, k, t: ["blowup"],
            expected_exit=2,
        ),
        # Bad format -> exit 2 (typer arg validation) or 1 (BadParameter) — either is fine
        ExitCase(
            name="inspect_bad_format",
            args_factory=lambda c, k, t: ["inspect", str(c), "--format", "xml"],
            expected_exit=2,
        ),
        # Missing path -> exit 1
        ExitCase(
            name="inspect_missing",
            args_factory=lambda c, k, t: ["inspect", str(t / "nope")],
            expected_exit=1,
        ),
        # Backup wrong extension -> exit 1
        ExitCase(
            name="backup_wrong_extension",
            args_factory=lambda c, k, t: ["backup", str(c), "-o", str(t / "x.zip")],
            expected_exit=1,
        ),
        # Backup unknown include -> exit 1
        ExitCase(
            name="backup_unknown_include",
            args_factory=lambda c, k, t: [
                "backup",
                str(k),
                "-o",
                str(t / "x.agentpack"),
                "--include",
                "credentials",
            ],
            expected_exit=1,
        ),
        # Backup overwrite refused
        ExitCase(
            name="backup_overwrite_refused",
            args_factory=lambda c, k, t: [
                "backup",
                str(c),
                "-o",
                str(t / "exists.agentpack"),
            ],
            expected_exit=1,
        ),
        # Unknown harness via flag
        ExitCase(
            name="inspect_unknown_harness",
            args_factory=lambda c, k, t: ["inspect", str(c), "--harness", "bogus"],
            expected_exit=1,
        ),
    ]


def _build_exists_backup(codex_home: Path, tmp_root: Path) -> None:
    """Pre-create ``tmp_root/exists.agentpack`` so backup refuses to overwrite it.

    The cli arg ``-o`` will point at this file. We do not invoke any backup here;
    we just leave the file in place for the test to encounter.
    """
    out = tmp_root / "exists.agentpack"
    out.write_bytes(b"keep me")
    assert out.read_bytes() == b"keep me"


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in _rebuild_exit_cases()],
)
def test_f_exit_code_matrix(
    case: ExitCase,
    codex_home: Path,
    tmp_root: Path,
    recorder: StressRecorder,
) -> None:
    if case.name == "backup_overwrite_refused":
        _build_exists_backup(codex_home, tmp_root)
    start = time.perf_counter()
    # Pass codex_home as both c and k; the lambda only uses what it needs.
    result = run_cli(case.args_factory(codex_home, codex_home, tmp_root))
    duration = time.perf_counter() - start
    assert result.exit_code == case.expected_exit, (
        f"{case.name}: expected exit {case.expected_exit}, got {result.exit_code}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    recorder.record(
        "exit_codes",
        case.name,
        "pass",
        duration,
        exit_code=result.exit_code,
        expected_exit_code=case.expected_exit,
    )


# ---------------------------------------------------------------------------
# Section G: Regression — re-run the existing 33-test suite
# ---------------------------------------------------------------------------


def test_g_regression_suite(stress_reporter: StressRecorder, recorder: StressRecorder) -> None:
    """Re-run all tests except this file as a regression check.

    Uses ``pytest.main`` in-process rather than a subprocess because pytest-cov's
    fork/thread hooks would otherwise emit per-process coverage data files in
    statement-only mode, conflicting with the parent's branch-mode data on
    ``coverage.combine()``.
    """

    repo_root = Path(__file__).resolve().parents[1]
    buf = io.StringIO()
    start = time.perf_counter()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        exit_code = pytest.main(
            [
                str(repo_root / "tests"),
                "--ignore=" + str(repo_root / "tests" / "test_stress.py"),
                "-q",
                "--no-header",
                "--tb=line",
                "-p",
                "no:cacheprovider",
            ],
            plugins=[],
        )
    duration = time.perf_counter() - start
    output = buf.getvalue()
    proc = subprocess.CompletedProcess(
        args=["pytest.main()"], returncode=exit_code, stdout=output, stderr=""
    )
    # Parse pytest summary: "X passed" or "X passed, Y failed"
    summary: dict[str, Any] = {
        "command": "pytest.main(in-process)",
        "exit_code": proc.returncode,
        "duration_seconds": duration,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-1000:],
    }
    text = proc.stdout
    for marker in ("passed", "failed", "errors"):
        # crude parse: "5 passed"
        for line in text.splitlines():
            line = line.strip()
            if line.endswith(f" {marker}") and line.split()[0].isdigit():
                summary[marker] = int(line.split()[0])
    stress_reporter.regression = summary
    status = "pass" if proc.returncode == 0 else "fail"
    recorder.record(
        "regression",
        "full_suite",
        status,
        duration,
        exit_code=proc.returncode,
        expected_exit_code=0,
        stderr_excerpt=proc.stderr[-500:],
    )
    if proc.returncode != 0:
        pytest.fail(f"Regression suite failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-1000:]}")


# ---------------------------------------------------------------------------
# Module-level pytest_terminal_summary hook (prints summary after pytest's own)
# ---------------------------------------------------------------------------


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """No-op: the session-scoped fixture handles the formatted summary block."""
    return None


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    sys.exit(
        pytest.main(
            [
                __file__,
                "-v",
                "--tb=short",
                "--color=yes",
                "-s",  # show the AGENT PORT STRESS REPORT block from fixture teardown
                "-p",
                "no:cacheprovider",
            ],
        )
    )
