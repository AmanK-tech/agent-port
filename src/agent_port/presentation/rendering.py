from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent_port.domain.models import ArchiveInspectionReport, InspectionReport


def render_json(model: BaseModel, redact_paths: bool = False) -> str:
    value = model.model_dump(mode="json", exclude_none=True)
    if redact_paths:
        value = _redact(value)
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def render_inspection(
    report: InspectionReport, skills_only: bool = False, redact_paths: bool = False
) -> str:
    lines = [
        f"Harness: {report.harness.value}",
        f"Source: {_display_path(report.source, redact_paths)}",
    ]
    if report.harness_version:
        lines.append(f"Harness version: {report.harness_version}")
    if not skills_only:
        conversation_count = (
            str(report.counts.conversations)
            if report.counts.conversations is not None
            else "unknown"
        )
        lines.extend(
            [
                f"Top-level conversations: {conversation_count}",
                f"Associated subagent transcripts: {report.counts.subagent_transcripts}",
                f"Total transcript files: {report.counts.transcript_files}",
                f"Active projects: {report.counts.projects}",
                f"Attachments: {report.counts.attachments}",
            ]
        )
    eligible_skills = [skill for skill in report.skills if skill.eligible]
    excluded_skills = [skill for skill in report.skills if not skill.eligible]
    lines.append(f"Eligible user-owned skills: {len(eligible_skills)}")
    lines.append(f"Plugin-managed or otherwise excluded skills: {len(excluded_skills)}")
    for skill in eligible_skills:
        lines.append(
            f"  - {skill.name} [{skill.owner.value}, eligible] "
            f"{_display_path(skill.source_path, redact_paths)}"
        )
    if report.exclusions and not skills_only:
        lines.append("Excluded paths:")
        lines.extend(
            f"  - {_display_path(item.path, redact_paths)}: {item.reason}"
            for item in report.exclusions
        )
    if report.diagnostics:
        lines.append("Diagnostics:")
        lines.extend(
            f"  - {item.severity.value}: {item.message}"
            + (f" ({_display_path(item.path, redact_paths)})" if item.path else "")
            for item in report.diagnostics
        )
    return "\n".join(lines)


def render_archive(report: ArchiveInspectionReport, redact_paths: bool = False) -> str:
    counts = report.manifest.counts
    conversations = counts.conversations if counts.conversations is not None else "unknown"
    return "\n".join(
        [
            f"Archive: {_display_path(report.source, redact_paths)}",
            f"Harness: {report.manifest.harness.value}",
            f"Format version: {report.manifest.format_version}",
            f"Checksums valid: {'yes' if report.checksums_valid else 'no'}",
            f"Archive members: {report.archived_files}",
            f"Payload bytes: {report.archived_bytes}",
            f"Top-level conversations: {conversations}",
            f"Associated subagent transcripts: {counts.subagent_transcripts}",
            f"Total transcript files: {counts.transcript_files}",
            f"Active projects: {counts.projects}",
            f"User-owned skills: {counts.skills}",
            f"Attachments: {counts.attachments}",
        ]
    )


def _redact(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {item_key: _redact(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, str) and key in {
        "path",
        "backup",
        "destination",
        "destination_home",
        "output",
        "plan_path",
        "run_directory",
        "source",
        "source_path",
        "source_root",
        "source_home",
        "status_path",
        "accepted_unmapped",
        "archive",
        "current_plan",
        "handoff_status",
        "initial_verification_path",
        "pairing_code_file",
        "state_file",
        "verification_result",
        "workspace",
    }:
        return f"<redacted>/{Path(value).name}" if Path(value).name else "<redacted>"
    return value


def _display_path(value: str, redact: bool) -> str:
    if not redact:
        return value
    return f"<redacted>/{Path(value).name}" if Path(value).name else "<redacted>"
