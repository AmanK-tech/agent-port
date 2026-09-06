from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_port.application.transfer import TransferReceiveService
from agent_port.domain.models import TransferReceiveResult
from agent_port.infrastructure.transfer.protocol import encode_pairing_code
from agent_port.presentation.cli import app

runner = CliRunner()


def test_cli_inspect_json_and_redaction(codex_home: Path) -> None:
    result = runner.invoke(app, ["inspect", str(codex_home), "--format", "json", "--redact-paths"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["harness"] == "codex"
    assert payload["source"].startswith("<redacted>/")


def test_cli_text_redacts_paths(codex_home: Path) -> None:
    result = runner.invoke(app, ["inspect", str(codex_home), "--redact-paths"])
    assert result.exit_code == 0, result.output
    assert str(codex_home) not in result.output
    assert "<redacted>/.codex" in result.output


def test_cli_rejects_unknown_include(claude_home: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "backup",
            str(claude_home),
            "--output",
            str(tmp_path / "backup.agentpack"),
            "--include",
            "credentials",
        ],
    )
    assert result.exit_code == 1
    assert "Invalid --include" in result.output


def test_transfer_receive_uses_hidden_pairing_prompt(tmp_path: Path) -> None:
    code = encode_pairing_code(bytes(range(16)))
    result = runner.invoke(
        app,
        [
            "transfer",
            "receive",
            "--output",
            str(tmp_path / "received.agentpack"),
            "--host",
            "127.0.0.1",
        ],
        input=f"{code}\n",
    )

    assert result.exit_code == 1
    assert "Pairing code" in result.output
    assert code not in result.output
    assert "--host and --port" in result.output


def test_transfer_receive_accepts_pairing_code_from_plugin_without_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = encode_pairing_code(bytes(range(16)))
    output = tmp_path / "received.agentpack"
    captured: dict[str, object] = {}

    def execute(
        _self: TransferReceiveService,
        output: Path,
        pairing_code: str,
        host: str | None = None,
        port: int | None = None,
        discovery_timeout: float = 8.0,
    ) -> TransferReceiveResult:
        captured.update(
            output=output,
            pairing_code=pairing_code,
            host=host,
            port=port,
            discovery_timeout=discovery_timeout,
        )
        return TransferReceiveResult(
            output=str(output.resolve()),
            archived_bytes=123,
            archive_sha256=f"sha256:{'0' * 64}",
            harness="claude-code",
        )

    monkeypatch.setattr(TransferReceiveService, "execute", execute)
    result = runner.invoke(
        app,
        [
            "transfer",
            "receive",
            "--output",
            str(output),
            "--pairing-code",
            code,
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["pairing_code"] == code
    assert captured["output"] == output
    assert "Pairing code:" not in result.output
    assert code not in result.output
    assert "Received and verified" in result.output


def test_transfer_send_reuses_backup_selection_validation(
    claude_home: Path, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        ["transfer", "send", str(claude_home), "--include", "credentials"],
    )

    assert result.exit_code == 1
    assert "Invalid --include" in result.output


def test_restore_handoff_requires_explicit_authorization(tmp_path: Path) -> None:
    result = runner.invoke(app, ["restore", "handoff", str(tmp_path / "plan.json")])

    assert result.exit_code == 1
    assert "--confirm-quit-to-apply" in result.output
