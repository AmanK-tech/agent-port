from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_port.application.backup import BackupService
from agent_port.application.migration import MigrationWorkspaceService, load_migration_document
from agent_port.application.transfer import TransferSendService
from agent_port.domain.models import CountSummary, TransferOffer, TransferSendResult
from agent_port.presentation.cli import app


def unstyle(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_blocked_plan_is_saved_with_status_and_final_plan_info_is_canonical(
    claude_home: Path, tmp_path: Path
) -> None:
    source_project = tmp_path / "old-home" / "project"
    _write_jsonl(
        claude_home / "projects/-synthetic-project/session-1.jsonl",
        [
            {
                "type": "system",
                "sessionId": "session-1",
                "cwd": str(source_project),
                "version": "2.3.4",
            },
            {
                "type": "assistant",
                "sessionId": "session-1",
                "message": {"content": "hello"},
            },
        ],
    )
    prepared = MigrationWorkspaceService().execute(root=tmp_path / "Migrations")
    archive = Path(prepared.archive)
    BackupService().execute(claude_home, archive)
    destination_home = tmp_path / "destination-home"
    destination = destination_home / ".claude"
    destination_project = tmp_path / "destination-project"
    destination_project.mkdir()
    _write_jsonl(
        destination / "projects/-seed/seed.jsonl",
        [
            {
                "type": "system",
                "sessionId": "seed",
                "cwd": str(destination_project),
                "version": "2.3.9",
            }
        ],
    )
    runner = CliRunner()
    blocked = runner.invoke(
        app,
        [
            "restore",
            "plan",
            str(archive),
            "--destination",
            str(destination),
            "--destination-home",
            str(destination_home),
            "--format",
            "json",
        ],
    )
    assert blocked.exit_code == 1
    blocked_payload = json.loads(blocked.output)
    assert blocked_payload["status"] == "blocked"
    assert blocked_payload["ready"] is False
    assert Path(blocked_payload["plan_path"]).name == "restore-plan-001.json"
    assert blocked_payload["suggested_mappings"] == [
        {"source": str(claude_home.parent), "destination": str(destination_home)}
    ]
    assert any(item["kind"] == "unmapped-project" for item in blocked_payload["blockers"])

    loaded = runner.invoke(
        app,
        ["restore", "plan-info", blocked_payload["plan_path"], "--format", "json"],
    )
    assert loaded.exit_code == 1
    assert json.loads(loaded.output)["plan_id"] == blocked_payload["plan_id"]

    ready = runner.invoke(
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
            f"{claude_home.parent}={destination_home}",
            "--format",
            "json",
        ],
    )
    assert ready.exit_code == 0, ready.output
    ready_payload = json.loads(ready.output)
    assert ready_payload["status"] == "ready"
    assert Path(ready_payload["plan_path"]).name == "restore-plan-002.json"
    assert ready_payload["content"]["conversations"] == 1
    assert ready_payload["content"]["subagent_transcripts"] == 0
    assert "project_registration" not in ready.output
    document = load_migration_document(Path(prepared.workspace))
    assert document.current_plan == ready_payload["plan_path"]
    assert document.run_directory == ready_payload["run_directory"]


def test_canonical_plugin_commands_have_the_documented_options() -> None:
    runner = CliRunner()
    prepare = runner.invoke(app, ["transfer", "prepare", "--help"])
    send = runner.invoke(app, ["transfer", "send", "--help"])
    receive = runner.invoke(app, ["transfer", "receive", "--help"])
    plan = runner.invoke(app, ["restore", "plan", "--help"])
    plan_info = runner.invoke(app, ["restore", "plan-info", "--help"])
    inspect = runner.invoke(app, ["inspect", "--help"])
    handoff = runner.invoke(app, ["restore", "handoff", "--help"])
    verify = runner.invoke(app, ["restore", "verify", "--help"])

    for result in (prepare, send, receive, inspect, plan, plan_info, handoff, verify):
        assert result.exit_code == 0, result.output
    assert "--format" not in unstyle(send.output)
    for result, options in (
        (prepare, ("--workspace", "--format")),
        (receive, ("--workspace", "--pairing-code-file", "--format")),
        (plan, ("--destination-home", "--map")),
        (plan_info, ("--format",)),
        (inspect, ("--format",)),
        (handoff, ("--confirm-quit-to-apply", "--format")),
        (verify, ("--plan", "--format")),
    ):
        for option in options:
            assert option in unstyle(result.output)


def test_transfer_send_rejects_format_and_prominently_prints_pairing_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = "VISIBLE-PAIRING-CODE"

    def execute(
        _self: TransferSendService,
        *args: object,
        on_ready: object,
        **kwargs: object,
    ) -> TransferSendResult:
        del args, kwargs
        assert callable(on_ready)
        on_ready(
            TransferOffer(
                pairing_code=code,
                hosts=["192.168.1.10"],
                port=32100,
                expires_at=1_800_000_000,
                discovery_available=True,
            )
        )
        return TransferSendResult(
            archived_bytes=10,
            archive_sha256=f"sha256:{'0' * 64}",
            content=CountSummary(conversations=1, transcript_files=1, projects=1),
        )

    monkeypatch.setattr(TransferSendService, "execute", execute)
    runner = CliRunner()

    unsupported = runner.invoke(app, ["transfer", "send", str(tmp_path), "--format", "json"])
    visible = runner.invoke(app, ["transfer", "send", str(tmp_path)])

    assert unsupported.exit_code != 0
    assert "No such option: --format" in unstyle(unsupported.output)
    assert visible.exit_code == 0, visible.output
    assert f"Pairing code: {code}" in visible.output
