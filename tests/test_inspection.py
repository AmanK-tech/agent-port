from __future__ import annotations

import json
from pathlib import Path

from agent_port.application.inspection import InspectService
from agent_port.domain.models import HarnessName, InspectionReport, SkillOwner


def test_inspects_codex_without_printing_content(codex_home: Path) -> None:
    result = InspectService().execute(codex_home)
    assert isinstance(result, InspectionReport)
    assert result.harness is HarnessName.CODEX
    assert result.counts.conversations == 1
    assert result.counts.transcript_files == 1
    assert {skill.owner for skill in result.skills} == {
        SkillOwner.USER,
        SkillOwner.SYSTEM,
        SkillOwner.PROJECT,
        SkillOwner.PLUGIN,
        SkillOwner.CURATED_CACHE,
        SkillOwner.UNKNOWN,
    }
    assert result.projects[0].conversation_count == 1
    assert "synthetic secret text" not in result.model_dump_json()


def test_inspects_claude_code(claude_home: Path) -> None:
    result = InspectService().execute(claude_home)
    assert isinstance(result, InspectionReport)
    assert result.harness is HarnessName.CLAUDE_CODE
    assert result.counts.conversations == 1
    assert result.counts.projects == 1
    assert {skill.owner for skill in result.skills} == {SkillOwner.USER, SkillOwner.PROJECT}
    assert sum(skill.eligible for skill in result.skills) == 1


def test_empty_named_home_can_be_explicitly_inspected(tmp_path: Path) -> None:
    source = tmp_path / ".codex"
    source.mkdir()
    result = InspectService().execute(source, "codex")
    assert isinstance(result, InspectionReport)
    assert result.counts.conversations == 0


def test_claude_counts_only_top_level_transcripts_as_conversations(
    claude_home: Path,
) -> None:
    active = claude_home / "projects" / "-synthetic-project"
    subagent = active / "session-1" / "subagents" / "agent-1.jsonl"
    subagent.parent.mkdir(parents=True)
    subagent.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "subagent-session",
                "cwd": str(claude_home.parent / "project"),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (claude_home / "projects" / "-stale-registry-entry").mkdir()

    result = InspectService().execute(claude_home)

    assert isinstance(result, InspectionReport)
    assert result.counts.conversations == 1
    assert result.counts.subagent_transcripts == 1
    assert result.counts.transcript_files == 2
    assert result.counts.projects == 1
    assert result.projects[0].conversation_count == 1
    assert any(item.code == "inactive-project-containers" for item in result.diagnostics)
