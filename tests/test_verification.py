from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

import agent_port.application.verification as verification_module
from agent_port.application.backup import BackupService
from agent_port.application.handoff import handoff_status_path, write_handoff_status
from agent_port.application.restore import RestoreApplyService, RestorePlanService, RollbackService
from agent_port.application.verification import RestoreVerificationService
from agent_port.domain.errors import RestoreError
from agent_port.domain.models import (
    HandoffState,
    HarnessName,
    RestoreHandoffStatus,
    RestoreVerificationState,
)
from agent_port.presentation.cli import app


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


def _apply_claude(claude_home: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    sidecar = (
        claude_home / "projects/-synthetic-project/session-1/tool-results/synthetic-result.txt"
    )
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("synthetic tool result", encoding="utf-8")
    archive = tmp_path / "source.agentpack"
    BackupService().execute(claude_home, archive)
    source_transcript = claude_home / "projects/-synthetic-project/session-1.jsonl"
    source_record = json.loads(source_transcript.read_text(encoding="utf-8").splitlines()[0])
    destination = tmp_path / "destination" / ".claude"
    project = tmp_path / "destination-project"
    project.mkdir()
    _write_jsonl(
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
    plan_path = tmp_path / "restore-plan.json"
    RestorePlanService().execute(
        archive,
        plan_path,
        destination=destination,
        destination_home=destination.parent,
        mapping_values=[f"{source_record['cwd']}={project}"],
    )
    result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
    restored = next(
        path
        for path in destination.rglob("session-1.jsonl")
        if path != source_transcript and path.is_file()
    )
    return plan_path, Path(result.run_directory), restored


def _codex_destination(root: Path, project: Path) -> Path:
    destination = root / ".codex"
    destination.mkdir(parents=True)
    with closing(sqlite3.connect(destination / "state.sqlite")) as connection, connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, rollout_path TEXT)"
        )
        connection.execute("CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO _sqlx_migrations VALUES (40)")
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


def test_verifier_accepts_append_only_resumed_conversation(
    claude_home: Path, tmp_path: Path
) -> None:
    _plan, run_directory, restored = _apply_claude(claude_home, tmp_path)
    with restored.open("a", encoding="utf-8") as output:
        output.write(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": "session-1",
                    "message": {"content": "new destination message"},
                }
            )
            + "\n"
        )

    verified = RestoreVerificationService().execute(run_directory)

    assert verified.status is RestoreVerificationState.VERIFIED
    assert verified.restore_completed_safely is True
    assert verified.current_data_intact is True
    assert verified.destination_valid is True
    assert verified.counts.verified_conversations == 1
    assert verified.counts.verified_skills == verified.counts.expected_skills == 1
    assert (run_directory / "plan.json").is_file()

    cli = CliRunner().invoke(app, ["restore", "verify", str(run_directory), "--format", "json"])
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output)["status"] == "verified"


def test_transformed_transcript_retains_source_timestamp(claude_home: Path, tmp_path: Path) -> None:
    source = claude_home / "projects/-synthetic-project/session-1.jsonl"
    os.utime(source, ns=(1_700_000_100_222_333_444, 1_700_000_100_222_333_444))
    expected_mtime = source.stat().st_mtime_ns

    plan_path, _run_directory, restored = _apply_claude(claude_home, tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    operation = next(
        item for item in plan["operations"] if item.get("member", "").endswith("session-1.jsonl")
    )

    assert operation["source_mtime_ns"] == expected_mtime
    assert operation["project_source"]
    assert restored.stat().st_mtime_ns == expected_mtime


def test_initial_verification_is_immutable_after_later_activity(
    claude_home: Path, tmp_path: Path
) -> None:
    _plan, run_directory, restored = _apply_claude(claude_home, tmp_path)
    initial_path = run_directory / "initial-verification.json"
    initial_evidence = initial_path.read_bytes()
    with restored.open("a", encoding="utf-8") as output:
        output.write('{"type":"assistant","sessionId":"session-1","later":true}\n')

    current = RestoreVerificationService().execute(run_directory)

    assert current.status is RestoreVerificationState.VERIFIED
    assert current.initial_verification_status is RestoreVerificationState.VERIFIED
    assert current.initial_verification_path == str(initial_path)
    assert initial_path.read_bytes() == initial_evidence
    assert (run_directory / "current-verification.json").is_file()


def test_verifier_detects_truncated_transcript_and_changed_skill(
    claude_home: Path, tmp_path: Path
) -> None:
    _plan, run_directory, restored = _apply_claude(claude_home, tmp_path)
    restored.write_text(restored.read_text(encoding="utf-8").splitlines()[0] + "\n")
    skill = tmp_path / "destination" / ".claude" / "skills" / "personal" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")

    verified = RestoreVerificationService().execute(run_directory)

    assert verified.status is RestoreVerificationState.CHANGED
    assert verified.restore_completed_safely is True
    assert verified.current_data_intact is False
    codes = {item.code for item in verified.diagnostics}
    assert "restored-transcript-truncated" in codes
    assert "restored-checksum-mismatch" in codes


def test_verifier_distinguishes_prefix_change_from_truncation(
    claude_home: Path, tmp_path: Path
) -> None:
    _plan, run_directory, restored = _apply_claude(claude_home, tmp_path)
    records = [json.loads(line) for line in restored.read_text(encoding="utf-8").splitlines()]
    records[1]["message"] = {"content": "replaced history"}
    _write_jsonl(restored, records)

    verified = RestoreVerificationService().execute(run_directory)

    assert verified.status is RestoreVerificationState.CHANGED
    codes = {item.code for item in verified.diagnostics}
    assert "restored-transcript-prefix-changed" in codes
    assert "restored-transcript-truncated" not in codes


def test_verifier_classifies_malformed_current_transcript(
    claude_home: Path, tmp_path: Path
) -> None:
    _plan, run_directory, restored = _apply_claude(claude_home, tmp_path)
    restored.write_text("{not-json}\n", encoding="utf-8")

    verified = RestoreVerificationService().execute(run_directory)

    assert verified.status is RestoreVerificationState.CHANGED
    assert "restored-transcript-malformed-current" in {item.code for item in verified.diagnostics}


def test_verifier_classifies_malformed_retained_evidence(claude_home: Path, tmp_path: Path) -> None:
    plan_path, run_directory, _restored = _apply_claude(claude_home, tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    operation = next(
        item for item in plan["operations"] if item.get("member", "").endswith("session-1.jsonl")
    )
    retained = run_directory / "staged" / "transformed" / Path(*operation["member"].split("/"))
    retained.write_text("{not-json}\n", encoding="utf-8")

    verified = RestoreVerificationService().execute(run_directory)

    assert verified.status is RestoreVerificationState.CHANGED
    assert "restored-transcript-malformed-evidence" in {item.code for item in verified.diagnostics}


def test_verifier_classifies_missing_transcript(claude_home: Path, tmp_path: Path) -> None:
    _plan, run_directory, restored = _apply_claude(claude_home, tmp_path)
    restored.unlink()

    verified = RestoreVerificationService().execute(run_directory)

    assert verified.status is RestoreVerificationState.CHANGED
    assert "restored-transcript-missing" in {item.code for item in verified.diagnostics}


def test_verifier_classifies_unstable_transcript_read(
    claude_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plan, run_directory, restored = _apply_claude(claude_home, tmp_path)
    stable_read = verification_module._stable_read

    def unstable(path: Path) -> bytes | None:
        return None if path == restored else stable_read(path)

    monkeypatch.setattr(verification_module, "_stable_read", unstable)

    verified = RestoreVerificationService().execute(run_directory)

    assert verified.status is RestoreVerificationState.CHANGED
    assert "restored-transcript-unstable-read" in {item.code for item in verified.diagnostics}


def test_incomplete_initial_verification_rolls_back_and_retains_evidence(
    claude_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RestoreVerificationService._verify_current_data

    def incomplete(self: RestoreVerificationService, *args: object, **kwargs: object):
        intact, counts = original(self, *args, **kwargs)  # type: ignore[arg-type]
        return intact, counts.model_copy(update={"verified_conversations": 0})

    monkeypatch.setattr(RestoreVerificationService, "_verify_current_data", incomplete)

    with pytest.raises(RestoreError, match="Initial closed-harness verification failed"):
        _apply_claude(claude_home, tmp_path)

    plan = json.loads((tmp_path / "restore-plan.json").read_text(encoding="utf-8"))
    run_directory = Path(plan["run_directory"])
    initial = json.loads((run_directory / "initial-verification.json").read_text(encoding="utf-8"))
    failure = json.loads((run_directory / "failure.json").read_text(encoding="utf-8"))
    journal = json.loads((run_directory / "journal.json").read_text(encoding="utf-8"))
    assert initial["status"] == "changed"
    assert "incomplete-verification-counts" in {item["code"] for item in initial["diagnostics"]}
    assert failure["rolled_back"] is True
    assert journal["rolled_back"] is True
    assert not list(Path(plan["destination"]).rglob("session-1.jsonl"))


def test_one_bad_project_does_not_invalidate_unrelated_project_count(
    claude_home: Path, tmp_path: Path
) -> None:
    second_project = tmp_path / "second-source-project"
    second_project.mkdir()
    _write_jsonl(
        claude_home / "projects/-second-project/session-2.jsonl",
        [
            {
                "type": "system",
                "sessionId": "session-2",
                "cwd": str(second_project),
                "version": "2.3.4",
            },
            {"type": "assistant", "sessionId": "session-2", "message": {"content": "ok"}},
        ],
    )
    archive = tmp_path / "two-projects.agentpack"
    BackupService().execute(claude_home, archive)
    first_record = json.loads(
        (claude_home / "projects/-synthetic-project/session-1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    destination = tmp_path / "two-project-destination" / ".claude"
    destination.mkdir(parents=True)
    first_destination_project = tmp_path / "first-destination-project"
    second_destination_project = tmp_path / "second-destination-project"
    first_destination_project.mkdir()
    second_destination_project.mkdir()
    _write_jsonl(
        destination / "projects/-existing/destination-session.jsonl",
        [
            {
                "type": "system",
                "sessionId": "destination-session",
                "cwd": str(first_destination_project),
                "version": "2.3.9",
            }
        ],
    )
    plan_path = tmp_path / "two-project-plan.json"
    RestorePlanService().execute(
        archive,
        plan_path,
        destination=destination,
        destination_home=destination.parent,
        mapping_values=[
            f"{first_record['cwd']}={first_destination_project}",
            f"{second_project}={second_destination_project}",
        ],
    )
    result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
    restored_first = next(destination.rglob("session-1.jsonl"))
    restored_first.write_text(
        restored_first.read_text(encoding="utf-8").splitlines()[0] + "\n",
        encoding="utf-8",
    )

    verified = RestoreVerificationService().execute(Path(result.run_directory))

    assert verified.status is RestoreVerificationState.CHANGED
    assert verified.counts.expected_projects == 2
    assert verified.counts.verified_projects == 1


def test_verifier_detects_missing_attachment(claude_home: Path, tmp_path: Path) -> None:
    _plan, run_directory, restored = _apply_claude(claude_home, tmp_path)
    attachment = restored.parent / "session-1/tool-results/synthetic-result.txt"
    attachment.unlink()

    verified = RestoreVerificationService().execute(run_directory)

    assert verified.status is RestoreVerificationState.CHANGED
    assert verified.counts.expected_attachments == 1
    assert verified.counts.verified_attachments == 0
    assert "missing-restored-path" in {item.code for item in verified.diagnostics}


def test_verifier_detects_corrupted_recorded_evidence(claude_home: Path, tmp_path: Path) -> None:
    _plan, run_directory, _restored = _apply_claude(claude_home, tmp_path)
    (run_directory / "result.json").write_text("{}", encoding="utf-8")

    verified = RestoreVerificationService().execute(run_directory)

    assert verified.status is RestoreVerificationState.FAILED
    assert verified.restore_completed_safely is False
    assert {item.code for item in verified.diagnostics} == {"invalid-restore-run"}


def test_verifier_supports_legacy_run_with_explicit_plan(claude_home: Path, tmp_path: Path) -> None:
    plan, run_directory, _restored = _apply_claude(claude_home, tmp_path)
    (run_directory / "plan.json").unlink()

    verified = RestoreVerificationService().execute(run_directory, plan)

    assert verified.status is RestoreVerificationState.VERIFIED


def test_verifier_reports_pending_handoff(tmp_path: Path) -> None:
    run_directory = tmp_path / ".agent-port-runs" / "pending-plan"
    status_path = handoff_status_path(run_directory)
    now = datetime.now(UTC)
    write_handoff_status(
        status_path,
        RestoreHandoffStatus(
            plan_id="pending-plan",
            plan_path=str(tmp_path / "plan.json"),
            run_directory=str(run_directory),
            status_path=str(status_path),
            harness=HarnessName.CLAUDE_CODE,
            state=HandoffState.WAITING,
            armed_at=now,
            updated_at=now,
            expires_at=now + timedelta(minutes=30),
        ),
    )

    verified = RestoreVerificationService().execute(run_directory)

    assert verified.status is RestoreVerificationState.PENDING
    assert verified.valid is False

    cli = CliRunner().invoke(app, ["restore", "verify", str(run_directory), "--format", "json"])
    assert cli.exit_code == 2
    assert json.loads(cli.output)["status"] == "pending"


def test_verifier_reports_rolled_back_run(claude_home: Path, tmp_path: Path) -> None:
    _plan, run_directory, _restored = _apply_claude(claude_home, tmp_path)
    RollbackService().execute(run_directory, confirm_harness_closed=True)

    verified = RestoreVerificationService().execute(run_directory)

    assert verified.status is RestoreVerificationState.ROLLED_BACK
    assert verified.restore_completed_safely is True
    assert verified.current_data_intact is False


def test_verifier_detects_incomplete_codex_database_registration(
    codex_home: Path, tmp_path: Path
) -> None:
    archive = tmp_path / "codex.agentpack"
    BackupService().execute(codex_home, archive)
    source_meta = json.loads(
        (codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    old_project = Path(source_meta["payload"]["cwd"])
    destination_home = tmp_path / "codex-destination"
    new_project = tmp_path / "codex-project"
    destination = _codex_destination(destination_home, new_project)
    plan_path = tmp_path / "codex-plan.json"
    RestorePlanService().execute(
        archive,
        plan_path,
        destination=destination,
        destination_home=destination_home,
        mapping_values=[f"{old_project}={new_project}"],
    )
    result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
    with closing(sqlite3.connect(destination / "state.sqlite")) as connection, connection:
        connection.execute("DELETE FROM thread_dynamic_tools WHERE thread_id='thread-1'")

    verified = RestoreVerificationService().execute(Path(result.run_directory))

    assert verified.status is RestoreVerificationState.CHANGED
    assert "codex-database-record-missing" in {item.code for item in verified.diagnostics}


@pytest.mark.parametrize("archive_state", ["missing", "replaced"])
def test_verification_uses_retained_counts_when_original_archive_changes(
    claude_home: Path, tmp_path: Path, archive_state: str
) -> None:
    plan_path, run_directory, _restored = _apply_claude(claude_home, tmp_path)
    archive = Path(json.loads(plan_path.read_text())["archive"]["path"])
    archive.unlink()
    if archive_state == "replaced":
        BackupService().execute(claude_home, archive, include=frozenset({"skills"}))

    verified = RestoreVerificationService().execute(run_directory)

    assert verified.success_gate_passed, verified.model_dump_json(indent=2)
    assert verified.counts.expected_transcript_files == 1
    assert verified.counts.expected_projects == 1


@pytest.mark.parametrize("multiple_projects", [False, True])
def test_codex_multiple_transcripts_count_one_conversation_and_keep_canonical_rollout(
    codex_home: Path, tmp_path: Path, multiple_projects: bool
) -> None:
    original = codex_home / "sessions/2026/06/30/rollout-thread-1.jsonl"
    duplicate = original.with_name("zzz-thread-1.jsonl")
    duplicate.write_bytes(original.read_bytes())
    if multiple_projects:
        extra_project = tmp_path / "extra-project"
        extra_project.mkdir()
        records = [json.loads(line) for line in original.read_text().splitlines()]
        records.append(
            {"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(extra_project)}}
        )
        _write_jsonl(original, records)
    archive = tmp_path / "multiple.agentpack"
    BackupService().execute(codex_home, archive)
    old_project = json.loads(original.read_text().splitlines()[0])["payload"]["cwd"]
    new_project = tmp_path / "multiple-project"
    destination = _codex_destination(tmp_path / "multiple-destination", new_project)
    plan_path = tmp_path / "multiple-plan.json"
    RestorePlanService().execute(
        archive,
        plan_path,
        destination=destination,
        destination_home=destination.parent,
        mapping_values=[f"{old_project}={new_project}"],
        accepted_unmapped=[str(extra_project)] if multiple_projects else [],
    )

    result = RestoreApplyService().execute(plan_path, confirm_harness_closed=True)
    verified = RestoreVerificationService().execute(Path(result.run_directory))

    assert verified.success_gate_passed, verified.model_dump_json(indent=2)
    assert verified.counts.expected_conversations == verified.counts.verified_conversations == 1
    assert verified.counts.verified_projects == (2 if multiple_projects else 1)
    assert (
        verified.counts.expected_transcript_files == verified.counts.verified_transcript_files == 2
    )
    with closing(sqlite3.connect(destination / "state.sqlite")) as connection:
        rollout = connection.execute(
            "SELECT rollout_path FROM threads WHERE id='thread-1'"
        ).fetchone()
    assert rollout == (str(destination / original.relative_to(codex_home)),)
