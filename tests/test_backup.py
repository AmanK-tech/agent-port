from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_port.adapters.codex.database import inspect_database
from agent_port.application.backup import BackupService
from agent_port.application.inspection import InspectService
from agent_port.domain.errors import ArchiveError, BackupError
from agent_port.domain.models import ArchiveInspectionReport, PayloadRole
from agent_port.infrastructure.archive import AgentPackReader

FIXED_TIME = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


def _rewrite_timestamp_document(source: Path, destination: Path, transform: Any) -> None:
    with zipfile.ZipFile(source) as archive:
        members = [(info, archive.read(info)) for info in archive.infolist()]
    timestamp_data = next(data for info, data in members if info.filename == "timestamps.json")
    updated = transform(json.loads(timestamp_data))
    updated_data = (
        json.dumps(updated, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    checksums = json.loads(
        next(data for info, data in members if info.filename == "checksums.json")
    )
    checksums["entries"]["timestamps.json"].update(
        sha256=f"sha256:{hashlib.sha256(updated_data).hexdigest()}",
        size=len(updated_data),
    )
    checksums_data = (
        json.dumps(checksums, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    with zipfile.ZipFile(destination, "w") as archive:
        for info, data in members:
            if info.filename == "timestamps.json":
                data = updated_data
            elif info.filename == "checksums.json":
                data = checksums_data
            archive.writestr(info, data)


def test_database_inspection_closes_all_sqlite_connections(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connections: list[TrackingConnection] = []
    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        closed = False

        def close(self) -> None:
            self.closed = True
            super().close()

    def tracking_connect(*args: Any, **kwargs: Any) -> TrackingConnection:
        kwargs["factory"] = TrackingConnection
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    inspect_database(codex_home / "state.sqlite")

    assert len(connections) == 3
    assert all(connection.closed for connection in connections)


def test_codex_backup_is_verified_and_preserves_executable_mode(
    codex_home: Path, tmp_path: Path
) -> None:
    output = tmp_path / "codex.agentpack"
    result = BackupService(clock=lambda: FIXED_TIME).execute(codex_home, output)
    assert result.manifest.counts.conversations == 1
    inspected = InspectService().execute(output)
    assert isinstance(inspected, ArchiveInspectionReport)
    assert inspected.checksums_valid is True
    source_script = codex_home.parent / ".agents" / "skills" / "my-skill" / "run.sh"
    expected_mode = stat.S_IMODE(source_script.stat().st_mode)
    with zipfile.ZipFile(output) as archive:
        script = archive.getinfo("native/skills/my-skill/run.sh")
        assert stat.S_IMODE(script.external_attr >> 16) == expected_mode
        assert "auth.json" not in archive.namelist()


def test_archive_bytes_are_deterministic(codex_home: Path, tmp_path: Path) -> None:
    first = tmp_path / "first.agentpack"
    second = tmp_path / "second.agentpack"
    service = BackupService(clock=lambda: FIXED_TIME)
    service.execute(codex_home, first)
    service.execute(codex_home, second)
    assert first.read_bytes() == second.read_bytes()


def test_timestamp_metadata_round_trips_regular_native_files(
    claude_home: Path, tmp_path: Path
) -> None:
    transcript = claude_home / "projects/-synthetic-project/session-1.jsonl"
    os.utime(transcript, ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
    expected_mtime = transcript.stat().st_mtime_ns
    output = tmp_path / "timestamps.agentpack"

    BackupService(clock=lambda: FIXED_TIME).execute(claude_home, output)

    member = "native/sessions/projects/-synthetic-project/session-1.jsonl"
    with zipfile.ZipFile(output) as archive:
        timestamps = json.loads(archive.read("timestamps.json"))
        assert timestamps["version"] == 1
        assert timestamps["entries"][member] == expected_mtime
        assert archive.getinfo(member).date_time == (1980, 1, 1, 0, 0, 0)
    extracted = tmp_path / "extracted"
    AgentPackReader().materialize(output, extracted)
    assert (extracted / member).stat().st_mtime_ns == expected_mtime


@pytest.mark.parametrize("bad_value", [-1, "1700000000", True])
def test_timestamp_metadata_rejects_non_integer_or_negative_values(
    claude_home: Path, tmp_path: Path, bad_value: object
) -> None:
    output = tmp_path / "source.agentpack"
    corrupted = tmp_path / f"bad-{bad_value!s}.agentpack"
    BackupService(clock=lambda: FIXED_TIME).execute(claude_home, output)
    member = "native/sessions/projects/-synthetic-project/session-1.jsonl"

    def corrupt(value: dict[str, object]) -> dict[str, object]:
        entries = value["entries"]
        assert isinstance(entries, dict)
        entries[member] = bad_value
        return value

    _rewrite_timestamp_document(output, corrupted, corrupt)

    with pytest.raises(ArchiveError, match=r"Invalid agentpack archive|non-negative"):
        InspectService().execute(corrupted)


def test_timestamp_metadata_must_exactly_match_regular_native_members(
    claude_home: Path, tmp_path: Path
) -> None:
    output = tmp_path / "source.agentpack"
    corrupted = tmp_path / "missing-timestamp.agentpack"
    BackupService(clock=lambda: FIXED_TIME).execute(claude_home, output)
    member = "native/sessions/projects/-synthetic-project/session-1.jsonl"

    def omit(value: dict[str, object]) -> dict[str, object]:
        entries = value["entries"]
        assert isinstance(entries, dict)
        entries.pop(member)
        return value

    _rewrite_timestamp_document(output, corrupted, omit)

    with pytest.raises(ArchiveError, match="Timestamp inventory"):
        InspectService().execute(corrupted)


def test_existing_archive_is_not_overwritten(codex_home: Path, tmp_path: Path) -> None:
    output = tmp_path / "exists.agentpack"
    output.write_bytes(b"keep me")
    with pytest.raises(BackupError, match="overwrite"):
        BackupService(clock=lambda: FIXED_TIME).execute(codex_home, output)
    assert output.read_bytes() == b"keep me"


def test_checksum_corruption_is_rejected(claude_home: Path, tmp_path: Path) -> None:
    output = tmp_path / "claude.agentpack"
    corrupted = tmp_path / "corrupted.agentpack"
    BackupService(clock=lambda: FIXED_TIME).execute(claude_home, output)
    target = "native/sessions/projects/-synthetic-project/session-1.jsonl"
    with zipfile.ZipFile(output) as source, zipfile.ZipFile(corrupted, "w") as destination:
        for info in source.infolist():
            destination.writestr(info, b"changed" if info.filename == target else source.read(info))
    with pytest.raises(ArchiveError, match="Checksum mismatch"):
        InspectService().execute(corrupted)


def test_claude_backup_manifest_distinguishes_conversations_from_subagents(
    claude_home: Path, tmp_path: Path
) -> None:
    active = claude_home / "projects" / "-synthetic-project"
    subagent = active / "session-1" / "subagents" / "agent-1.jsonl"
    subagent.parent.mkdir(parents=True)
    subagent.write_text(
        '{"type":"assistant","sessionId":"subagent-session"}\n',
        encoding="utf-8",
    )
    tool_result = active / "session-1" / "tool-results" / "result.jsonl"
    tool_result.parent.mkdir(parents=True)
    tool_result.write_text('{"result":"synthetic"}\n', encoding="utf-8")
    memory = active / "session-1" / "memory" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("synthetic memory", encoding="utf-8")
    stale = claude_home / "projects" / "-cleaned-up-project"
    stale_subagent = stale / "old" / "subagents" / "agent.jsonl"
    stale_subagent.parent.mkdir(parents=True)
    stale_subagent.write_text(
        '{"type":"assistant","sessionId":"stale-subagent"}\n', encoding="utf-8"
    )
    (stale / "memory.txt").write_text("stale", encoding="utf-8")
    for name in (".DS_Store", "._session-1.jsonl", "Thumbs.db", "desktop.ini"):
        (active / name).write_text("noise", encoding="utf-8")

    archive_path = tmp_path / "claude-counts.agentpack"
    result = BackupService(clock=lambda: FIXED_TIME).execute(claude_home, archive_path)

    assert result.manifest.counts.conversations == 1
    assert result.manifest.counts.subagent_transcripts == 1
    assert result.manifest.counts.transcript_files == 2
    assert result.manifest.counts.projects == 1
    assert result.manifest.counts.attachments == 2
    payloads = AgentPackReader().read_payloads(archive_path).payloads
    assert next(item for item in payloads if item.original_path.endswith("result.jsonl")).role is (
        PayloadRole.ATTACHMENT
    )
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert not any("-cleaned-up-project" in name for name in names)
    assert not any(Path(name).name in {".DS_Store", "Thumbs.db", "desktop.ini"} for name in names)
    assert not any(Path(name).name.startswith("._") for name in names)
    assert any(name.endswith("tool-results/result.jsonl") for name in names)
    assert any(name.endswith("memory/MEMORY.md") for name in names)


def test_unknown_live_wal_database_is_backed_up_opaquely(tmp_path: Path) -> None:
    source = tmp_path / ".codex"
    source.mkdir()
    database = source / "unknown.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE future_state (value TEXT)")
        connection.execute("INSERT INTO future_state VALUES ('synthetic')")
        connection.commit()
        assert database.with_name(f"{database.name}-wal").exists()
        output = tmp_path / "opaque.agentpack"
        result = BackupService(clock=lambda: FIXED_TIME).execute(
            source, output, requested_harness="codex"
        )
    finally:
        connection.close()
    assert result.manifest.native_schema[0].recognized is False
    with zipfile.ZipFile(output) as archive:
        assert "native/state/unknown.sqlite" in archive.namelist()


def test_internal_skill_symlink_is_preserved(claude_home: Path, tmp_path: Path) -> None:
    skill = claude_home / "skills" / "personal"
    target = skill / "reference.txt"
    target.write_text("synthetic reference", encoding="utf-8")
    try:
        os.symlink("reference.txt", skill / "latest.txt")
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    output = tmp_path / "symlink.agentpack"
    BackupService(clock=lambda: FIXED_TIME).execute(claude_home, output)
    inspected = InspectService().execute(output)
    assert isinstance(inspected, ArchiveInspectionReport)
    with zipfile.ZipFile(output) as archive:
        link = archive.getinfo("native/skills/personal/latest.txt")
        assert stat.S_ISLNK(link.external_attr >> 16)
        assert archive.read(link) == b"reference.txt"
