from __future__ import annotations

from pathlib import Path

import pytest

from agent_port.application.backup import BackupService
from agent_port.domain.errors import ArchiveError, BackupError
from agent_port.infrastructure.archive.common import resolve_symlink_member, validate_member_name


@pytest.mark.parametrize("name", ["../secret", "/absolute", "C:/users/secret", "a\\b"])
def test_unsafe_archive_member_names_are_rejected(name: str) -> None:
    with pytest.raises(ArchiveError):
        validate_member_name(name)


def test_escaping_archive_symlink_target_is_rejected() -> None:
    with pytest.raises(ArchiveError, match="escapes"):
        resolve_symlink_member("native/link", "../../outside")


def test_pretty_printed_json_is_not_accepted_as_jsonl(tmp_path: Path) -> None:
    source = tmp_path / ".claude"
    transcript = source / "projects" / "-project" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{\n  "type": "system"\n}\n', encoding="utf-8")
    with pytest.raises(BackupError, match="blocking error"):
        BackupService().execute(
            source,
            tmp_path / "invalid.agentpack",
            requested_harness="claude-code",
        )
