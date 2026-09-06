from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_port.adapters.claude_code.paths import encode_project_path
from agent_port.adapters.restore_support import apply_append_only_jsonl, merge_jsonl
from agent_port.application.backup import BackupService
from agent_port.application.registry import AdapterRegistry
from agent_port.application.restore import (
    RestoreApplyService,
    RestorePlanService,
    RollbackService,
)
from agent_port.domain.errors import RestoreError
from agent_port.domain.models import HarnessName, OperationKind
from agent_port.infrastructure.path_mapping import PathMapper, parse_mapping
from agent_port.presentation.cli import app


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def make_v1_archive(source: Path, destination: Path) -> None:
    members: dict[str, tuple[zipfile.ZipInfo, bytes]] = {}
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            if info.is_dir() or info.filename in {
                "payloads.json",
                "timestamps.json",
                "checksums.json",
            }:
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


def make_legacy_v2_archive(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        members = [
            (info, archive.read(info))
            for info in archive.infolist()
            if info.filename != "timestamps.json"
        ]
    checksums = json.loads(
        next(data for info, data in members if info.filename == "checksums.json")
    )
    checksums["entries"].pop("timestamps.json")
    checksums_data = (
        json.dumps(checksums, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    with zipfile.ZipFile(destination, "w") as archive:
        for info, data in members:
            archive.writestr(info, checksums_data if info.filename == "checksums.json" else data)


def _codex_destination(root: Path, project: Path, migration: int = 40) -> Path:
    destination = root / ".codex"
    destination.mkdir(parents=True)
    with closing(sqlite3.connect(destination / "state.sqlite")) as connection, connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, rollout_path TEXT)"
        )
        connection.execute("CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO _sqlx_migrations VALUES (?)", (migration,))
        connection.execute(
            "CREATE TABLE thread_dynamic_tools ("
            "thread_id TEXT, name TEXT, definition TEXT, PRIMARY KEY (thread_id, name))"
        )
        connection.execute(
            "CREATE TABLE thread_spawn_edges ("
            "parent_thread_id TEXT, child_thread_id TEXT, relation TEXT, "
            "PRIMARY KEY (parent_thread_id, child_thread_id))"
        )
    project.mkdir()
    return destination


def test_codex_saved_plan_apply_verify_and_manual_rollback(
    codex_home: Path, tmp_path: Path
) -> None:
    source_transcript = codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl"
    os.utime(
        source_transcript,
        ns=(1_700_000_200_333_444_555, 1_700_000_200_333_444_555),
    )
    source_mtime = source_transcript.stat().st_mtime_ns
    archive = tmp_path / "source.agentpack"
    BackupService().execute(codex_home, archive)
    source_meta = json.loads(source_transcript.read_text().splitlines()[0])
    old_project = Path(source_meta["payload"]["cwd"])
    destination_home = tmp_path / "new-home"
    new_project = tmp_path / "new-project"
    destination = _codex_destination(destination_home, new_project)
    plan_path = tmp_path / "restore-plan.json"

    plan = RestorePlanService().execute(
        archive,
        plan_path,
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{old_project}={new_project}"],
    )
    assert plan.ready is True
    assert plan.archive.format_version == 2

    result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
    restored = destination / "sessions/2026/06/30/rollout-thread-1.jsonl"
    assert result.verification.valid is True

    assert restored.is_file()
    assert restored.stat().st_mtime_ns == source_mtime
    first = json.loads(restored.read_text(encoding="utf-8").splitlines()[0])
    assert first["payload"]["cwd"] == str(new_project)
    source_event = (
        (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_bytes().splitlines()[1]
    )
    assert restored.read_bytes().splitlines()[1] == source_event
    assert (destination_home / ".agents/skills/my-skill/SKILL.md").is_file()
    assert (destination / "attachments/synthetic.txt").read_text() == "synthetic attachment"
    restored_index = json.loads((destination / "session_index.jsonl").read_text().splitlines()[0])
    assert restored_index["cwd"] == str(new_project)
    with closing(sqlite3.connect(destination / "state.sqlite")) as connection:
        row = connection.execute(
            "SELECT cwd, rollout_path FROM threads WHERE id='thread-1'"
        ).fetchone()
        dynamic_tools = connection.execute(
            "SELECT COUNT(*) FROM thread_dynamic_tools WHERE thread_id='thread-1'"
        ).fetchone()[0]
        spawn_edges = connection.execute(
            "SELECT COUNT(*) FROM thread_spawn_edges WHERE parent_thread_id='thread-1'"
        ).fetchone()[0]
    assert row == (str(new_project), str(restored))
    assert dynamic_tools == 1
    assert spawn_edges == 1

    rolled_back = RollbackService().execute(Path(result.run_directory), confirm_harness_closed=True)
    assert rolled_back.restored >= 3
    assert not restored.exists()
    assert not (destination_home / ".agents/skills/my-skill").exists()
    with closing(sqlite3.connect(destination / "state.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0


def test_legacy_v2_archive_restores_with_timestamp_ordering_warning(
    claude_home: Path, tmp_path: Path
) -> None:
    current = tmp_path / "current.agentpack"
    legacy = tmp_path / "legacy.agentpack"
    BackupService().execute(claude_home, current)
    make_legacy_v2_archive(current, legacy)
    source_record = json.loads(
        (claude_home / "projects/-synthetic-project/session-1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    destination = tmp_path / "destination" / ".claude"
    destination.mkdir(parents=True)
    project = tmp_path / "destination-project"
    project.mkdir()
    write_jsonl(
        destination / "projects/-existing/destination-session.jsonl",
        [
            {
                "type": "system",
                "sessionId": "destination-session",
                "cwd": str(project),
                "version": "2.3.9",
            }
        ],
    )
    plan_path = tmp_path / "legacy-plan.json"

    plan = RestorePlanService().execute(
        legacy,
        plan_path,
        destination=destination,
        destination_home=destination.parent,
        mapping_values=[f"{source_record['cwd']}={project}"],
    )

    assert "source-timestamps-unavailable" in {item.code for item in plan.diagnostics}
    result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
    assert result.verification.valid is True
    initial = json.loads(
        (Path(result.run_directory) / "initial-verification.json").read_text(encoding="utf-8")
    )
    assert initial["status"] == "verified"


def test_changed_destination_makes_plan_stale(codex_home: Path, tmp_path: Path) -> None:
    archive = tmp_path / "source.agentpack"
    BackupService().execute(codex_home, archive)
    source_meta = json.loads(
        (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
    )
    old_project = Path(source_meta["payload"]["cwd"])
    destination_home = tmp_path / "new-home"
    new_project = tmp_path / "new-project"
    destination = _codex_destination(destination_home, new_project)
    plan_path = tmp_path / "restore-plan.json"
    RestorePlanService().execute(
        archive,
        plan_path,
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{old_project}={new_project}"],
    )
    with closing(sqlite3.connect(destination / "state.sqlite")) as connection, connection:
        connection.execute(
            "INSERT INTO threads VALUES ('new-thread', ?, '/synthetic/rollout.jsonl')",
            (str(new_project),),
        )
    with pytest.raises(RestoreError, match="changed after planning"):
        RestoreApplyService().execute(plan_path, confirm_harness_closed=True)


def test_skill_replace_is_explicit_and_rollback_restores_old_skill(
    codex_home: Path, tmp_path: Path
) -> None:
    source_skill = codex_home.parent / ".agents/skills/my-skill/SKILL.md"
    os.utime(source_skill, ns=(1_700_000_300_444_555_666, 1_700_000_300_444_555_666))
    source_skill_mtime = source_skill.stat().st_mtime_ns
    archive = tmp_path / "source.agentpack"
    BackupService().execute(codex_home, archive)
    source_meta = json.loads(
        (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
    )
    old_project = Path(source_meta["payload"]["cwd"])
    destination_home = tmp_path / "new-home"
    new_project = tmp_path / "new-project"
    destination = _codex_destination(destination_home, new_project)
    old_skill = destination_home / ".agents/skills/my-skill"
    old_skill.mkdir(parents=True)
    (old_skill / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Old destination skill.\n---\n",
        encoding="utf-8",
    )
    blocked = RestorePlanService().execute(
        archive,
        tmp_path / "blocked-skill.json",
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{old_project}={new_project}"],
    )
    assert any(conflict.kind == "skill-collision" for conflict in blocked.conflicts)

    plan_path = tmp_path / "replace-skill.json"
    RestorePlanService().execute(
        archive,
        plan_path,
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{old_project}={new_project}"],
        skill_conflicts=["my-skill=replace"],
    )
    result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
    assert "Synthetic test skill" in (old_skill / "SKILL.md").read_text()
    assert (old_skill / "SKILL.md").stat().st_mtime_ns == source_skill_mtime
    RollbackService().execute(Path(result.run_directory), confirm_harness_closed=True)
    assert "Old destination skill" in (old_skill / "SKILL.md").read_text()


def test_failure_after_file_install_automatically_rolls_back(
    codex_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "source.agentpack"
    BackupService().execute(codex_home, archive)
    source_meta = json.loads(
        (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
    )
    old_project = Path(source_meta["payload"]["cwd"])
    destination_home = tmp_path / "new-home"
    new_project = tmp_path / "new-project"
    destination = _codex_destination(destination_home, new_project)
    registry = AdapterRegistry()
    plan_path = tmp_path / "failure-plan.json"
    RestorePlanService(registry).execute(
        archive,
        plan_path,
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{old_project}={new_project}"],
    )
    adapter = registry.for_harness(HarnessName.CODEX)

    def fail_apply(plan: object, staging: Path) -> None:
        del plan, staging
        raise RestoreError("synthetic database failure")

    monkeypatch.setattr(adapter, "apply_restore", fail_apply)
    with pytest.raises(RestoreError, match="synthetic database failure"):
        RestoreApplyService(registry).execute(plan_path, confirm_harness_closed=True)
    assert not (destination / "sessions/2026/06/30/rollout-thread-1.jsonl").exists()
    assert not (destination_home / ".agents/skills/my-skill").exists()


def test_manual_rollback_refuses_newer_changes(codex_home: Path, tmp_path: Path) -> None:
    archive = tmp_path / "source.agentpack"
    BackupService().execute(codex_home, archive)
    source_meta = json.loads(
        (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
    )
    old_project = Path(source_meta["payload"]["cwd"])
    destination_home = tmp_path / "new-home"
    new_project = tmp_path / "new-project"
    destination = _codex_destination(destination_home, new_project)
    plan_path = tmp_path / "rollback-plan.json"
    RestorePlanService().execute(
        archive,
        plan_path,
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{old_project}={new_project}"],
    )
    result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
    restored = destination / "sessions/2026/06/30/rollout-thread-1.jsonl"
    restored.write_text(restored.read_text() + '{"type":"newer-user-change"}\n')
    with pytest.raises(RestoreError, match="changed after restore"):
        RollbackService().execute(Path(result.run_directory), confirm_harness_closed=True)


def test_differing_codex_session_collision_blocks_plan(codex_home: Path, tmp_path: Path) -> None:
    archive = tmp_path / "source.agentpack"
    BackupService().execute(codex_home, archive)
    source_record = json.loads(
        (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
    )
    old_project = Path(source_record["payload"]["cwd"])
    destination_home = tmp_path / "new-home"
    new_project = tmp_path / "new-project"
    destination = _codex_destination(destination_home, new_project)
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
    plan = RestorePlanService().execute(
        archive,
        tmp_path / "blocked.json",
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{old_project}={new_project}"],
    )
    assert plan.ready is False
    assert any(conflict.kind == "session-id-collision" for conflict in plan.conflicts)


def test_unsupported_codex_migration_blocks_plan(codex_home: Path, tmp_path: Path) -> None:
    archive = tmp_path / "source.agentpack"
    BackupService().execute(codex_home, archive)
    source_meta = json.loads(
        (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
    )
    old_project = Path(source_meta["payload"]["cwd"])
    destination_home = tmp_path / "unsupported-home"
    new_project = tmp_path / "unsupported-project"
    destination = _codex_destination(destination_home, new_project, migration=38)
    plan = RestorePlanService().execute(
        archive,
        tmp_path / "unsupported-plan.json",
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{old_project}={new_project}"],
    )
    assert plan.ready is False
    assert any(conflict.kind == "unsupported-codex-schema" for conflict in plan.conflicts)


def test_claude_restore_maps_project_and_preserves_existing_session(
    claude_home: Path, tmp_path: Path
) -> None:
    write_jsonl(
        claude_home / "projects/-synthetic-project/session-1/subagents/agent-1.jsonl",
        [
            {
                "type": "assistant",
                "sessionId": "session-1",
                "message": {"content": "subagent"},
            }
        ],
    )
    archive = tmp_path / "claude.agentpack"
    BackupService().execute(claude_home, archive)
    source_project = Path(
        json.loads(
            (claude_home / "projects/-synthetic-project/session-1.jsonl")
            .read_text()
            .splitlines()[0]
        )["cwd"]
    )
    destination = tmp_path / "new-claude-home" / ".claude"
    destination_project = tmp_path / "claude-new-project"
    destination_project.mkdir()
    write_jsonl(
        destination / "projects/-existing/session-2.jsonl",
        [
            {
                "type": "system",
                "sessionId": "session-2",
                "cwd": str(destination_project),
                "version": "2.3.9",
            }
        ],
    )
    plan_path = tmp_path / "claude-plan.json"
    plan = RestorePlanService().execute(
        archive,
        plan_path,
        destination=destination,
        destination_home=destination.parent,
        mapping_values=[f"{source_project}={destination_project}"],
    )
    assert plan.ready is True
    result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
    restored = [path for path in destination.rglob("session-1.jsonl") if path.is_file()]
    assert len(restored) == 1
    restored_subagent = destination / "projects" / encode_project_path(str(destination_project))
    assert (restored_subagent / "session-1/subagents/agent-1.jsonl").is_file()
    first = json.loads(restored[0].read_text().splitlines()[0])
    assert first["cwd"] == str(destination_project)
    assert (destination / "projects/-existing/session-2.jsonl").is_file()
    assert result.verification.valid is True

    repeated_plan_path = tmp_path / "claude-repeat-plan.json"
    repeated = RestorePlanService().execute(
        archive,
        repeated_plan_path,
        destination=destination,
        destination_home=destination.parent,
        mapping_values=[f"{source_project}={destination_project}"],
    )
    assert repeated.ready, repeated.conflicts
    assert all(operation.kind is OperationKind.SKIP for operation in repeated.operations)
    assert (
        RestoreApplyService()
        .execute(repeated_plan_path, confirm_harness_closed=True)
        .verification.valid
    )


@pytest.mark.parametrize(
    ("source_mtime", "destination_mtime"),
    [
        (1_700_000_000_000_000_000, 1_800_000_000_000_000_000),
        (1_900_000_000_000_000_000, 1_800_000_000_000_000_000),
    ],
)
def test_append_only_merge_uses_newer_source_or_destination_timestamp(
    tmp_path: Path, source_mtime: int, destination_mtime: int
) -> None:
    source = tmp_path / "source.jsonl"
    destination = tmp_path / "destination.jsonl"
    write_jsonl(source, [{"id": "one"}, {"id": "two"}])
    write_jsonl(destination, [{"id": "one"}])
    os.utime(source, ns=(source_mtime, source_mtime))
    os.utime(destination, ns=(destination_mtime, destination_mtime))
    expected = max(source.stat().st_mtime_ns, destination.stat().st_mtime_ns)

    apply_append_only_jsonl(source, destination)

    assert len(destination.read_text(encoding="utf-8").splitlines()) == 2
    assert destination.stat().st_mtime_ns == expected


def test_index_merge_uses_newer_source_or_destination_timestamp(tmp_path: Path) -> None:
    source = tmp_path / "source-index.jsonl"
    destination = tmp_path / "destination-index.jsonl"
    write_jsonl(source, [{"id": "source"}])
    write_jsonl(destination, [{"id": "destination"}])
    os.utime(source, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    os.utime(destination, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))
    expected = destination.stat().st_mtime_ns

    merge_jsonl(source, destination)

    assert len(destination.read_text(encoding="utf-8").splitlines()) == 2
    assert destination.stat().st_mtime_ns == expected


def test_archive_v2_contains_verified_payload_inventory(claude_home: Path, tmp_path: Path) -> None:
    archive = tmp_path / "claude.agentpack"
    BackupService().execute(claude_home, archive)
    with zipfile.ZipFile(archive) as source:
        manifest = json.loads(source.read("manifest.json"))
        payloads = json.loads(source.read("payloads.json"))["payloads"]
    assert manifest["format_version"] == 2
    assert any(payload["role"] == "transcript" for payload in payloads)
    assert all(payload["sha256"].startswith("sha256:") for payload in payloads)


def test_unambiguous_v1_codex_archive_can_restore(codex_home: Path, tmp_path: Path) -> None:
    v2 = tmp_path / "source-v2.agentpack"
    v1 = tmp_path / "source-v1.agentpack"
    BackupService().execute(codex_home, v2)
    make_v1_archive(v2, v1)
    source_meta = json.loads(
        (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
    )
    old_project = Path(source_meta["payload"]["cwd"])
    destination_home = tmp_path / "v1-home"
    new_project = tmp_path / "v1-project"
    destination = _codex_destination(destination_home, new_project)
    plan_path = tmp_path / "v1-plan.json"
    plan = RestorePlanService().execute(
        v1,
        plan_path,
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{old_project}={new_project}"],
    )
    assert plan.ready is True
    result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
    assert result.verification.valid is True


def test_ambiguous_v1_codex_archive_is_rejected(codex_home: Path, tmp_path: Path) -> None:
    with closing(sqlite3.connect(codex_home / "other.sqlite")) as connection, connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, rollout_path TEXT)"
        )
        connection.execute("CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO _sqlx_migrations VALUES (39)")
    v2 = tmp_path / "ambiguous-v2.agentpack"
    v1 = tmp_path / "ambiguous-v1.agentpack"
    BackupService().execute(codex_home, v2)
    make_v1_archive(v2, v1)
    destination_home = tmp_path / "ambiguous-home"
    destination = _codex_destination(destination_home, tmp_path / "ambiguous-project")
    with pytest.raises(RestoreError, match="ambiguous Codex state"):
        RestorePlanService().execute(
            v1,
            tmp_path / "ambiguous-plan.json",
            destination=destination,
            destination_home=destination_home,
            accepted_unmapped=[str(tmp_path / "project")],
        )


def test_path_mapper_uses_component_aware_longest_prefix(tmp_path: Path) -> None:
    source_home = "/Users/old"
    destination_home = tmp_path
    mapper = PathMapper(
        [
            parse_mapping("~/Work=~/Developer/Work", source_home, destination_home),
            parse_mapping("~/Work/Special=~/Developer/Special", source_home, destination_home),
        ]
    )
    assert mapper.apply("/Users/old/Work/Special/repo").value == str(
        destination_home / "Developer/Special/repo"
    )
    assert mapper.apply("/Users/old/Workshop").mapped is False


def test_restore_cli_json_redaction_and_closed_confirmation(
    codex_home: Path, tmp_path: Path
) -> None:
    archive = tmp_path / "cli.agentpack"
    BackupService().execute(codex_home, archive)
    source_meta = json.loads(
        (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl").read_text().splitlines()[0]
    )
    old_project = Path(source_meta["payload"]["cwd"])
    destination_home = tmp_path / "cli-home"
    new_project = tmp_path / "cli-project"
    destination = _codex_destination(destination_home, new_project)
    plan_path = tmp_path / "cli-plan.json"
    runner = CliRunner()
    planned = runner.invoke(
        app,
        [
            "restore",
            "plan",
            str(archive),
            "--destination",
            str(destination),
            "--destination-home",
            str(destination_home),
            "--map",
            f"{old_project}={new_project}",
            "--output",
            str(plan_path),
            "--format",
            "json",
            "--redact-paths",
        ],
    )
    assert planned.exit_code == 0, planned.output
    assert str(destination) not in planned.output
    assert json.loads(planned.output)["destination"].startswith("<redacted>/")
    applied = runner.invoke(app, ["restore", "apply", str(plan_path)])
    assert applied.exit_code == 1
    assert "--confirm-harness-closed" in applied.output
