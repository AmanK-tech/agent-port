from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

import agent_port.application.migration as migration_module
from agent_port.application.migration import (
    MigrationWorkspaceService,
    consume_pairing_code_file,
    load_migration_document,
)
from agent_port.application.transfer import TransferReceiveService
from agent_port.domain.errors import TransferError
from agent_port.domain.models import CountSummary, MigrationStateValue, TransferReceiveResult
from agent_port.infrastructure.transfer.protocol import encode_pairing_code
from agent_port.presentation.cli import app


def test_prepare_creates_persistent_private_workspace_and_retry(tmp_path: Path) -> None:
    prepared = MigrationWorkspaceService().execute(root=tmp_path / "Migrations")
    workspace = Path(prepared.workspace)
    code_file = Path(prepared.pairing_code_file)

    assert workspace.parent == (tmp_path / "Migrations").resolve()
    assert Path(prepared.archive) == workspace / "source.agentpack"
    assert Path(prepared.state_file) == workspace / "migration.json"
    assert not code_file.exists()
    if os.name != "nt":
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o700

    retried = MigrationWorkspaceService().execute(workspace=workspace)
    assert retried.workspace == prepared.workspace
    assert retried.migration_id == prepared.migration_id
    assert retried.pairing_code_file != prepared.pairing_code_file
    assert not code_file.exists()


def test_pairing_code_file_is_consumed_once_and_restricted(tmp_path: Path) -> None:
    prepared = MigrationWorkspaceService().execute(root=tmp_path)
    code_file = Path(prepared.pairing_code_file)
    code = encode_pairing_code(bytes(range(16)))
    code_file.write_text(code + "\n", encoding="utf-8")

    assert consume_pairing_code_file(code_file, private_workspace=Path(prepared.workspace)) == code
    assert not code_file.exists()

    permissive = Path(prepared.workspace) / ".permissive-code"
    permissive.write_text(code, encoding="utf-8")
    if os.name != "nt":
        permissive.chmod(0o644)
        with pytest.raises(TransferError, match="permissions"):
            consume_pairing_code_file(permissive)
        assert not permissive.exists()


def test_pairing_code_file_rejects_symlink_and_removes_only_link(tmp_path: Path) -> None:
    prepared = MigrationWorkspaceService().execute(root=tmp_path)
    target = Path(prepared.workspace) / "target"
    target.write_text(encode_pairing_code(bytes(range(16))), encoding="utf-8")
    link = Path(prepared.workspace) / ".pairing-link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(TransferError, match="regular file"):
        consume_pairing_code_file(link, private_workspace=Path(prepared.workspace))
    assert not link.exists()
    assert target.exists()


@pytest.mark.parametrize("content", ["", "x" * 257])
def test_private_workspace_rejects_empty_or_oversized_pairing_file(
    tmp_path: Path, content: str
) -> None:
    prepared = MigrationWorkspaceService().execute(root=tmp_path)
    code_file = Path(prepared.pairing_code_file)
    code_file.write_text(content, encoding="utf-8")

    with pytest.raises(TransferError, match=r"invalid size|empty"):
        consume_pairing_code_file(code_file, private_workspace=Path(prepared.workspace))

    assert not code_file.exists()


def test_private_workspace_rejects_missing_pairing_file(tmp_path: Path) -> None:
    prepared = MigrationWorkspaceService().execute(root=tmp_path)
    code_file = Path(prepared.pairing_code_file)

    with pytest.raises(TransferError, match="Cannot inspect"):
        consume_pairing_code_file(code_file, private_workspace=Path(prepared.workspace))

    assert not code_file.exists()


def test_pairing_code_consumer_requires_existing_private_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "missing-workspace"

    with pytest.raises(TransferError, match="Cannot inspect private migration workspace"):
        consume_pairing_code_file(workspace / ".pairing-code", private_workspace=workspace)


def test_pairing_code_file_replacement_is_detected_without_deleting_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = MigrationWorkspaceService().execute(root=tmp_path)
    workspace = Path(prepared.workspace)
    code_file = Path(prepared.pairing_code_file)
    code_file.write_text(encode_pairing_code(bytes(range(16))), encoding="utf-8")
    replacement = workspace / ".replacement"
    replacement.write_text(encode_pairing_code(bytes(reversed(range(16)))), encoding="utf-8")
    real_open = migration_module.os.open

    def replacing_open(path: object, flags: int, *args: object) -> int:
        if Path(path) == code_file:
            code_file.unlink()
            replacement.replace(code_file)
        return real_open(path, flags, *args)

    monkeypatch.setattr(migration_module.os, "open", replacing_open)

    with pytest.raises(TransferError, match="changed while"):
        consume_pairing_code_file(code_file, private_workspace=workspace)

    assert code_file.exists()


def test_plugin_receive_uses_workspace_file_json_and_updates_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    prepared_result = runner.invoke(
        app,
        ["transfer", "prepare", "--root", str(tmp_path), "--format", "json"],
    )
    assert prepared_result.exit_code == 0, prepared_result.output
    prepared = json.loads(prepared_result.output)
    code = encode_pairing_code(bytes(range(16)))
    code_path = Path(prepared["pairing_code_file"])
    code_path.write_text(code, encoding="utf-8")
    captured: dict[str, object] = {}

    def execute(
        _self: TransferReceiveService,
        output: Path,
        pairing_code: str,
        host: str | None = None,
        port: int | None = None,
        discovery_timeout: float = 8.0,
    ) -> TransferReceiveResult:
        captured.update(output=output, pairing_code=pairing_code)
        output.write_bytes(b"synthetic")
        return TransferReceiveResult(
            output=str(output),
            archived_bytes=9,
            archive_sha256=f"sha256:{'0' * 64}",
            harness="claude-code",
            content=CountSummary(
                conversations=15,
                subagent_transcripts=5,
                transcript_files=20,
                projects=8,
                attachments=11,
                skills=17,
            ),
        )

    monkeypatch.setattr(TransferReceiveService, "execute", execute)
    received = runner.invoke(
        app,
        [
            "transfer",
            "receive",
            "--workspace",
            prepared["workspace"],
            "--format",
            "json",
        ],
    )

    assert received.exit_code == 0, received.output
    payload = json.loads(received.output)
    assert captured["pairing_code"] == code
    assert Path(captured["output"]) == Path(prepared["archive"])
    assert code not in received.output
    assert not code_path.exists()
    assert payload["content"]["conversations"] == 15
    assert payload["content"]["subagent_transcripts"] == 5
    document = load_migration_document(Path(prepared["workspace"]))
    assert document.status is MigrationStateValue.RECEIVED
    assert document.pairing_code_file is None
    assert document.content.transcript_files == 20


def test_receive_rejects_duplicate_pairing_code_sources(tmp_path: Path) -> None:
    prepared = MigrationWorkspaceService().execute(root=tmp_path)
    code = encode_pairing_code(bytes(range(16)))
    code_path = Path(prepared.pairing_code_file)
    code_path.write_text(code, encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "transfer",
            "receive",
            "--workspace",
            prepared.workspace,
            "--pairing-code",
            code,
            "--pairing-code-file",
            str(code_path),
        ],
    )
    assert result.exit_code == 1
    assert "Use only one" in result.output
    assert code not in result.output


def test_receive_consumes_invalid_base32_file_without_disclosure(tmp_path: Path) -> None:
    prepared = MigrationWorkspaceService().execute(root=tmp_path)
    code_path = Path(prepared.pairing_code_file)
    code_path.write_text("not-a-valid-code", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "transfer",
            "receive",
            "--workspace",
            prepared.workspace,
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    assert "not-a-valid-code" not in result.output
    assert not code_path.exists()
    assert load_migration_document(Path(prepared.workspace)).status is MigrationStateValue.FAILED


def test_receive_retry_rotates_to_a_fresh_uncreated_inbox(tmp_path: Path) -> None:
    prepared = MigrationWorkspaceService().execute(root=tmp_path)
    first_path = Path(prepared.pairing_code_file)
    first_path.write_text("not-a-valid-code", encoding="utf-8")
    failed = CliRunner().invoke(
        app,
        ["transfer", "receive", "--workspace", prepared.workspace, "--format", "json"],
    )
    assert failed.exit_code == 1
    assert not first_path.exists()

    retried = MigrationWorkspaceService().execute(workspace=Path(prepared.workspace))
    second_path = Path(retried.pairing_code_file)

    assert second_path != first_path
    assert not second_path.exists()
    assert load_migration_document(Path(prepared.workspace)).status is MigrationStateValue.PREPARED


def test_receive_rejects_non_authorized_file_inside_workspace(tmp_path: Path) -> None:
    prepared = MigrationWorkspaceService().execute(root=tmp_path)
    unauthorized = Path(prepared.workspace) / ".other-code"
    unauthorized.write_text(encode_pairing_code(bytes(range(16))), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "transfer",
            "receive",
            "--workspace",
            prepared.workspace,
            "--pairing-code-file",
            str(unauthorized),
        ],
    )

    assert result.exit_code == 1
    assert "does not match the active migration workspace" in result.output
    assert unauthorized.exists()
