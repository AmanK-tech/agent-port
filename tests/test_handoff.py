from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_port.application.backup import BackupService
from agent_port.application.handoff import (
    RestoreHandoffService,
    RestoreHandoffWorker,
    write_handoff_status,
)
from agent_port.application.restore import RestoreApplyService, RestorePlanService
from agent_port.application.verification import RestoreVerificationService
from agent_port.domain.errors import RestoreError
from agent_port.domain.models import (
    HandoffState,
    HarnessName,
    HarnessProcessRecord,
    RestoreHandoffStatus,
)
from agent_port.infrastructure.processes import _matches_harness


class FakeInspector:
    def __init__(self, sequences: list[list[HarnessProcessRecord]]) -> None:
        self.sequences = list(sequences)
        self.current = self.sequences[-1] if self.sequences else []

    def active(self, _harness: object) -> list[HarnessProcessRecord]:
        if self.sequences:
            self.current = self.sequences.pop(0)
        return self.current


def _process(pid: int = 101) -> HarnessProcessRecord:
    return HarnessProcessRecord(pid=pid, created_at=1.0, name="claude")


def test_process_matching_covers_native_and_node_launchers() -> None:
    assert _matches_harness(HarnessName.CLAUDE_CODE, "claude", "/bin/claude", [])
    assert _matches_harness(
        HarnessName.CLAUDE_CODE,
        "node",
        "/usr/bin/node",
        ["node", "/packages/@anthropic-ai/claude-code/cli.js"],
    )
    assert _matches_harness(
        HarnessName.CODEX,
        "Codex",
        "/Applications/Codex.app/Contents/MacOS/Codex",
        [],
    )
    assert not _matches_harness(
        HarnessName.CLAUDE_CODE,
        "python",
        "/usr/bin/python",
        ["python", "-m", "agent_port.infrastructure.handoff_worker"],
    )


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


def _claude_plan(claude_home: Path, tmp_path: Path) -> Path:
    archive = tmp_path / "source.agentpack"
    BackupService().execute(claude_home, archive)
    source_record = json.loads(
        (claude_home / "projects/-synthetic-project/session-1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
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
    return plan_path


def test_handoff_requires_confirmation_and_detected_harness(
    claude_home: Path, tmp_path: Path
) -> None:
    plan = _claude_plan(claude_home, tmp_path)
    service = RestoreHandoffService(inspector=FakeInspector([]), launcher=lambda _command: None)

    with pytest.raises(RestoreError, match="confirm-quit-to-apply"):
        service.execute(plan, confirm_quit_to_apply=False)
    with pytest.raises(RestoreError, match="no active destination harness"):
        service.execute(plan, confirm_quit_to_apply=True)


def test_handoff_refuses_destination_changed_after_planning(
    claude_home: Path, tmp_path: Path
) -> None:
    plan_path = _claude_plan(claude_home, tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    transcript_operation = next(
        item for item in plan["operations"] if item.get("member", "").endswith("session-1.jsonl")
    )
    destination = Path(transcript_operation["destination"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text('{"changed":true}\n', encoding="utf-8")
    launched: list[list[str]] = []

    with pytest.raises(RestoreError, match="changed after planning"):
        RestoreHandoffService(
            inspector=FakeInspector([[_process()]]),
            launcher=lambda command: launched.append(command),
        ).execute(plan_path, confirm_quit_to_apply=True)

    assert launched == []


def test_handoff_arms_restricted_status_and_worker_applies_after_closure(
    claude_home: Path, tmp_path: Path
) -> None:
    plan_path = _claude_plan(claude_home, tmp_path)
    launched: list[list[str]] = []
    inspector = FakeInspector([[_process(), _process(102)]])
    service = RestoreHandoffService(
        inspector=inspector,
        launcher=lambda command: launched.append(command),
    )

    armed = service.execute(plan_path, confirm_quit_to_apply=True)

    assert armed.state is HandoffState.ARMED
    assert len(armed.observed_processes) == 2
    assert launched and launched[0][-1] == armed.status_path
    if os.name != "nt":
        assert stat.S_IMODE(Path(armed.status_path).stat().st_mode) == 0o600

    worker_inspector = FakeInspector([[_process()], [], []])
    notifications: list[tuple[str, str]] = []
    completed = RestoreHandoffWorker(
        inspector=worker_inspector,
        sleep=lambda _seconds: None,
        notifier=lambda title, message: notifications.append((title, message)),
        poll_seconds=0.001,
    ).execute(Path(armed.status_path))

    assert completed.state is HandoffState.SUCCEEDED
    assert (Path(completed.run_directory) / "result.json").is_file()
    assert completed.initial_verification_status is not None
    assert completed.initial_verification_path == str(
        Path(completed.run_directory) / "initial-verification.json"
    )
    assert notifications[-1] == (
        "Restore verified",
        "Restore verified; safe to reopen the destination harness.",
    )


def test_handoff_expires_without_mutating_while_harness_remains_open(
    claude_home: Path, tmp_path: Path
) -> None:
    plan_path = _claude_plan(claude_home, tmp_path)
    inspector = FakeInspector([[_process()]])
    armed = RestoreHandoffService(inspector=inspector, launcher=lambda _command: None).execute(
        plan_path, confirm_quit_to_apply=True
    )
    now = datetime.now(UTC)
    expired_status = RestoreHandoffStatus.model_validate(
        {
            **armed.model_dump(mode="python"),
            "expires_at": now,
        }
    )
    write_handoff_status(Path(armed.status_path), expired_status)

    expired = RestoreHandoffWorker(
        inspector=inspector,
        clock=lambda: now,
        sleep=lambda _seconds: None,
    ).execute(Path(armed.status_path))

    assert expired.state is HandoffState.EXPIRED
    assert not Path(expired.run_directory).exists()


def test_handoff_fails_if_harness_reopens_during_apply(claude_home: Path, tmp_path: Path) -> None:
    plan_path = _claude_plan(claude_home, tmp_path)
    armed = RestoreHandoffService(
        inspector=FakeInspector([[_process()]]), launcher=lambda _command: None
    ).execute(plan_path, confirm_quit_to_apply=True)
    inspector = FakeInspector([[], []])

    class ReopeningApply:
        def execute(self, *_args: object, closure_guard: object, **_kwargs: object) -> None:
            inspector.current = [_process()]
            assert callable(closure_guard)
            closure_guard()

    failed = RestoreHandoffWorker(
        inspector=inspector,
        sleep=lambda _seconds: None,
        apply_service=ReopeningApply(),  # type: ignore[arg-type]
        notifier=lambda _title, _message: (_ for _ in ()).throw(RuntimeError("ignored")),
        poll_seconds=0.001,
    ).execute(Path(armed.status_path))

    assert failed.state is HandoffState.FAILED
    assert "reopened" in (failed.error or "")


def test_closure_guard_failure_after_mutation_automatically_rolls_back(
    claude_home: Path, tmp_path: Path
) -> None:
    plan_path = _claude_plan(claude_home, tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise RestoreError("Destination harness reopened during restore handoff.")

    with pytest.raises(RestoreError, match="reopened"):
        RestoreApplyService().execute(
            plan_path,
            confirm_harness_closed=True,
            closure_guard=guard,
        )

    destination = Path(plan["destination"])
    assert not list(destination.rglob("session-1.jsonl"))
    failure = json.loads((Path(plan["run_directory"]) / "failure.json").read_text())
    assert failure["rolled_back"] is True


def test_closure_guard_remains_active_through_initial_verification(
    claude_home: Path, tmp_path: Path
) -> None:
    plan_path = _claude_plan(claude_home, tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    run_directory = Path(plan["run_directory"])

    def guard() -> None:
        if (run_directory / "initial-verification.json").is_file():
            raise RestoreError("Destination harness reopened before success notification.")

    with pytest.raises(RestoreError, match="reopened before success"):
        RestoreApplyService().execute(
            plan_path,
            confirm_harness_closed=True,
            closure_guard=guard,
        )

    assert (run_directory / "initial-verification.json").is_file()
    failure = json.loads((run_directory / "failure.json").read_text(encoding="utf-8"))
    journal = json.loads((run_directory / "journal.json").read_text(encoding="utf-8"))
    assert failure["rolled_back"] is True
    assert journal["rolled_back"] is True
    assert not list(Path(plan["destination"]).rglob("session-1.jsonl"))


def test_handoff_initial_verification_failure_rolls_back_and_never_notifies_success(
    claude_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = _claude_plan(claude_home, tmp_path)
    original = RestoreVerificationService._verify_current_data

    def incomplete(self: RestoreVerificationService, *args: object, **kwargs: object):
        intact, counts = original(self, *args, **kwargs)  # type: ignore[arg-type]
        return intact, counts.model_copy(update={"verified_conversations": 0})

    monkeypatch.setattr(RestoreVerificationService, "_verify_current_data", incomplete)
    armed = RestoreHandoffService(
        inspector=FakeInspector([[_process()]]), launcher=lambda _command: None
    ).execute(plan_path, confirm_quit_to_apply=True)
    notifications: list[tuple[str, str]] = []

    failed = RestoreHandoffWorker(
        inspector=FakeInspector([[], []]),
        sleep=lambda _seconds: None,
        notifier=lambda title, message: notifications.append((title, message)),
        poll_seconds=0.001,
    ).execute(Path(armed.status_path))

    assert failed.state is HandoffState.FAILED
    assert failed.rolled_back is True
    assert failed.initial_verification_status is not None
    assert failed.initial_verification_status.value == "changed"
    assert failed.initial_verification_path == str(
        Path(failed.run_directory) / "initial-verification.json"
    )
    assert notifications == [("Restore failed", "Agent Port kept or restored the previous state.")]
