from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_port.domain.models import Diagnostic, Severity


@dataclass
class JsonlSummary:
    records: int = 0
    session_ids: set[str] = field(default_factory=set)
    project_paths: set[str] = field(default_factory=set)
    versions: set[str] = field(default_factory=set)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def inspect_jsonl(path: Path, harness: str) -> JsonlSummary:
    """Validate JSONL and retain only structural metadata used by inspection."""
    summary = JsonlSummary()
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    value: Any = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    detail = error.msg if isinstance(error, json.JSONDecodeError) else str(error)
                    summary.diagnostics.append(
                        Diagnostic(
                            code="malformed-jsonl",
                            severity=Severity.ERROR,
                            message=f"Invalid JSON object on line {line_number}: {detail}",
                            path=str(path),
                        )
                    )
                    return summary
                if not isinstance(value, dict):
                    summary.diagnostics.append(
                        Diagnostic(
                            code="invalid-jsonl-record",
                            severity=Severity.ERROR,
                            message=f"Line {line_number} is not a JSON object.",
                            path=str(path),
                        )
                    )
                    return summary
                summary.records += 1
                _collect_structural_metadata(value, harness, summary)
    except OSError as error:
        summary.diagnostics.append(
            Diagnostic(
                code="unreadable-transcript",
                severity=Severity.ERROR,
                message=str(error),
                path=str(path),
            )
        )
    if summary.records == 0 and not summary.diagnostics:
        summary.diagnostics.append(
            Diagnostic(
                code="empty-transcript",
                severity=Severity.WARNING,
                message="Transcript contains no JSON records.",
                path=str(path),
            )
        )
    return summary


def _collect_structural_metadata(
    value: dict[str, Any], harness: str, summary: JsonlSummary
) -> None:
    record_type = value.get("type")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    if harness == "codex" and record_type in {"session_meta", "thread_meta", "session_start"}:
        _add_string(summary.session_ids, payload.get("id"))
        _add_string(summary.project_paths, payload.get("cwd"))
        _add_string(summary.versions, payload.get("cli_version"))
        return

    if harness == "claude-code":
        if record_type in {"system", "summary", "user", "assistant"}:
            _add_string(summary.session_ids, value.get("sessionId"))
            _add_string(summary.project_paths, value.get("cwd"))
        if record_type == "system":
            _add_string(summary.versions, value.get("version"))


def _add_string(target: set[str], value: Any) -> None:
    if isinstance(value, str) and value:
        target.add(value)
