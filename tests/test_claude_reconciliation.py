from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path

from agent_port.application.backup import BackupService
from agent_port.application.restore import RestoreApplyService, RestorePlanService, RollbackService
from agent_port.domain.models import OperationKind


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _source_records(claude_home: Path, project: Path) -> list[dict[str, object]]:
    records = [
        json.loads(line)
        for line in (claude_home / "projects/-synthetic-project/session-1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    for record in records:
        if "cwd" in record:
            record["cwd"] = str(project)
    return records


def _plan_with_existing_session(
    claude_home: Path,
    tmp_path: Path,
    destination_records: list[dict[str, object]],
) -> tuple[Path, Path, object]:
    archive = tmp_path / "source.agentpack"
    BackupService().execute(claude_home, archive)
    source_project = claude_home.parent / "project"
    destination_home = tmp_path / "destination-home"
    destination = destination_home / ".claude"
    destination_project = tmp_path / "destination-project"
    destination_project.mkdir()
    _write_jsonl(
        destination / "projects/-existing/session-1.jsonl",
        destination_records,
    )
    plan_path = tmp_path / "restore-plan.json"
    plan = RestorePlanService().execute(
        archive,
        plan_path,
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{source_project}={destination_project}"],
    )
    return plan_path, destination, plan


def test_source_ahead_session_is_appended_and_rollback_is_exact(
    claude_home: Path, tmp_path: Path
) -> None:
    destination_project = tmp_path / "destination-project"
    records = _source_records(claude_home, destination_project)
    plan_path, destination, plan = _plan_with_existing_session(claude_home, tmp_path, records[:1])

    assert plan.ready is True
    assert any(operation.kind is OperationKind.MERGE_JSONL for operation in plan.operations)
    original = (destination / "projects/-existing/session-1.jsonl").read_bytes()
    result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
    restored = destination / "projects/-existing/session-1.jsonl"
    assert len(restored.read_text(encoding="utf-8").splitlines()) == len(records)
    assert result.verification.valid is True

    RollbackService().execute(Path(result.run_directory), confirm_harness_closed=True)
    assert restored.read_bytes() == original


def test_destination_ahead_session_is_preserved(claude_home: Path, tmp_path: Path) -> None:
    destination_project = tmp_path / "destination-project"
    records = _source_records(claude_home, destination_project)
    records.append(
        {"type": "assistant", "sessionId": "session-1", "message": {"content": "resumed"}}
    )
    plan_path, destination, plan = _plan_with_existing_session(claude_home, tmp_path, records)

    assert plan.ready is True
    transcript = next(
        operation for operation in plan.operations if operation.identity == "session-1"
    )
    assert transcript.kind is OperationKind.SKIP
    assert any(item.code == "destination-session-appended" for item in plan.diagnostics)
    before = (destination / "projects/-existing/session-1.jsonl").read_bytes()
    RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
    assert (destination / "projects/-existing/session-1.jsonl").read_bytes() == before


def test_divergent_and_malformed_session_histories_block(claude_home: Path, tmp_path: Path) -> None:
    destination_project = tmp_path / "destination-project"
    records = _source_records(claude_home, destination_project)
    records[1] = {
        "type": "assistant",
        "sessionId": "session-1",
        "message": {"content": "diverged"},
    }
    _plan_path, _destination, divergent = _plan_with_existing_session(
        claude_home, tmp_path / "diverged", records
    )
    assert divergent.ready is False
    assert any(item.kind == "session-id-collision" for item in divergent.conflicts)

    malformed_root = tmp_path / "malformed"
    malformed_root.mkdir()
    destination_project = malformed_root / "destination-project"
    valid = _source_records(claude_home, destination_project)[0]
    archive = malformed_root / "source.agentpack"
    BackupService().execute(claude_home, archive)
    destination_home = malformed_root / "destination-home"
    destination = destination_home / ".claude"
    transcript = destination / "projects/-existing/session-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps(valid) + "\n{\n", encoding="utf-8")
    destination_project.mkdir()
    plan = RestorePlanService().execute(
        archive,
        malformed_root / "plan.json",
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{claude_home.parent / 'project'}={destination_project}"],
    )
    assert plan.ready is False
    assert any(item.kind == "malformed-session-collision" for item in plan.conflicts)


def test_duplicate_destination_session_id_blocks(claude_home: Path, tmp_path: Path) -> None:
    destination_project = tmp_path / "destination-project"
    records = _source_records(claude_home, destination_project)
    plan_path, destination, _plan = _plan_with_existing_session(claude_home, tmp_path, records)
    _write_jsonl(destination / "projects/-duplicate/session-copy.jsonl", records)
    plan_path.unlink()
    plan = RestorePlanService().execute(
        tmp_path / "source.agentpack",
        plan_path,
        destination=destination,
        destination_home=destination.parent,
        mapping_values=[f"{claude_home.parent / 'project'}={destination_project}"],
    )
    assert plan.ready is False
    assert any(item.kind == "ambiguous-destination-session-id" for item in plan.conflicts)


def test_destination_transcript_with_multiple_session_ids_blocks(
    claude_home: Path, tmp_path: Path
) -> None:
    destination_project = tmp_path / "destination-project"
    records = _source_records(claude_home, destination_project)
    records.append({"type": "assistant", "sessionId": "unexpected-session"})

    _plan_path, _destination, plan = _plan_with_existing_session(claude_home, tmp_path, records)

    assert plan.ready is False
    assert any(item.kind == "ambiguous-destination-session-id" for item in plan.conflicts)


def test_multiple_patch_versions_share_one_compatibility_profile(
    claude_home: Path, tmp_path: Path
) -> None:
    project = claude_home.parent / "project"
    _write_jsonl(
        claude_home / "projects/-synthetic-project/session-2.jsonl",
        [
            {
                "type": "system",
                "sessionId": "session-2",
                "cwd": str(project),
                "version": "2.3.9",
            }
        ],
    )
    archive = tmp_path / "source.agentpack"
    result = BackupService().execute(claude_home, archive)
    assert result.manifest.harness_version is None
    assert result.manifest.harness_versions == ["2.3.4", "2.3.9"]
    assert result.manifest.compatibility_profiles == ["2.3"]

    destination_home = tmp_path / "destination-home"
    destination = destination_home / ".claude"
    new_project = tmp_path / "new-project"
    new_project.mkdir()
    _write_jsonl(
        destination / "projects/-existing/seed.jsonl",
        [
            {
                "type": "system",
                "sessionId": "seed",
                "cwd": str(new_project),
                "version": "2.3.7",
            }
        ],
    )
    _write_jsonl(
        destination / "projects/-stale/old/subagents/agent.jsonl",
        [
            {
                "type": "system",
                "sessionId": "stale",
                "cwd": str(new_project),
                "version": "9.9.0",
            }
        ],
    )
    _write_jsonl(
        destination / "projects/-existing/._noise.jsonl",
        [
            {
                "type": "system",
                "sessionId": "noise",
                "cwd": str(new_project),
                "version": "8.8.0",
            }
        ],
    )
    plan = RestorePlanService().execute(
        archive,
        tmp_path / "plan.json",
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{project}={new_project}"],
    )
    assert plan.ready is True


def test_incompatible_or_unknown_source_profiles_block(claude_home: Path, tmp_path: Path) -> None:
    project = claude_home.parent / "project"
    _write_jsonl(
        claude_home / "projects/-synthetic-project/session-2.jsonl",
        [
            {
                "type": "system",
                "sessionId": "session-2",
                "cwd": str(project),
                "version": "2.4.0",
            }
        ],
    )
    archive = tmp_path / "mixed.agentpack"
    BackupService().execute(claude_home, archive)
    destination_home = tmp_path / "destination-home"
    destination = destination_home / ".claude"
    new_project = tmp_path / "new-project"
    new_project.mkdir()
    _write_jsonl(
        destination / "projects/-existing/seed.jsonl",
        [
            {
                "type": "system",
                "sessionId": "seed",
                "cwd": str(new_project),
                "version": "2.3.7",
            }
        ],
    )
    mixed = RestorePlanService().execute(
        archive,
        tmp_path / "mixed-plan.json",
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{project}={new_project}"],
    )
    assert mixed.ready is False
    assert any(item.kind == "incompatible-claude-source-versions" for item in mixed.conflicts)

    for transcript in claude_home.rglob("*.jsonl"):
        values = [json.loads(line) for line in transcript.read_text().splitlines()]
        for value in values:
            value.pop("version", None)
        _write_jsonl(transcript, values)
    unknown_archive = tmp_path / "unknown.agentpack"
    BackupService().execute(claude_home, unknown_archive)
    unknown = RestorePlanService().execute(
        unknown_archive,
        tmp_path / "unknown-plan.json",
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{project}={new_project}"],
    )
    assert unknown.ready is False
    assert any(item.kind == "unknown-claude-version" for item in unknown.conflicts)


def test_legacy_missing_version_metadata_is_derived_from_transcripts(
    claude_home: Path, tmp_path: Path
) -> None:
    source = tmp_path / "source.agentpack"
    legacy = tmp_path / "legacy.agentpack"
    BackupService().execute(claude_home, source)
    _rewrite_manifest_without_versions(source, legacy)
    destination_project = tmp_path / "destination-project"
    records = _source_records(claude_home, destination_project)
    _plan_path, _destination, plan = _plan_with_existing_session(
        claude_home, tmp_path / "baseline", records
    )
    legacy_plan_path = tmp_path / "legacy-plan.json"
    legacy_plan = RestorePlanService().execute(
        legacy,
        legacy_plan_path,
        destination=Path(plan.destination),
        destination_home=Path(plan.destination_home),
        mapping_values=[f"{claude_home.parent / 'project'}={destination_project}"],
    )
    assert legacy_plan.ready is True


def _rewrite_manifest_without_versions(source: Path, destination: Path) -> None:
    members: dict[str, tuple[zipfile.ZipInfo, bytes]] = {}
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            if info.is_dir() or info.filename == "checksums.json":
                continue
            data = archive.read(info)
            if info.filename == "manifest.json":
                value = json.loads(data)
                value.pop("harness_version", None)
                value.pop("harness_versions", None)
                value.pop("compatibility_profiles", None)
                data = (
                    json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
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
