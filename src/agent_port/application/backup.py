from __future__ import annotations

import platform
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from agent_port.application.registry import AdapterRegistry
from agent_port.domain.errors import BackupError
from agent_port.domain.models import (
    ArchiveManifest,
    BackupResult,
    CountSummary,
    PayloadRecord,
    PayloadRole,
    PayloadScope,
    PayloadsDocument,
    Severity,
    TimestampsDocument,
)
from agent_port.infrastructure.archive import AgentPackWriter
from agent_port.infrastructure.archive.common import canonical_json, describe_path, stage_entries
from agent_port.infrastructure.filesystem.jsonl import inspect_jsonl


def _is_claude_transcript_member(relative_name: str) -> bool:
    parts = tuple(part for part in relative_name.split("/") if part)
    return (len(parts) == 4 and parts[:2] == ("sessions", "projects")) or "subagents" in parts[3:-1]


class BackupService:
    def __init__(
        self,
        registry: AdapterRegistry | None = None,
        archive_writer: AgentPackWriter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry or AdapterRegistry()
        self._archive_writer = archive_writer or AgentPackWriter()
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(
        self,
        source: Path,
        output: Path,
        requested_harness: str = "auto",
        include: frozenset[str] = frozenset({"sessions", "skills"}),
        user_home: Path | None = None,
    ) -> BackupResult:
        unknown = include.difference({"sessions", "skills"})
        if unknown or not include:
            detail = ", ".join(sorted(unknown)) if unknown else "nothing selected"
            raise BackupError(f"Invalid --include selection: {detail}")
        source = source.expanduser().resolve()
        output = output.expanduser().resolve()
        if output.suffix != ".agentpack":
            raise BackupError("Backup output must use the .agentpack extension.")
        adapter = self._registry.select(source, requested_harness)
        resolved_home = user_home.expanduser().resolve() if user_home else None
        report = adapter.inspect(source, resolved_home)
        errors = [item for item in report.diagnostics if item.severity is Severity.ERROR]
        if errors:
            raise BackupError(
                f"Inspection found {len(errors)} blocking error(s); fix them before backup."
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="agent-port-stage-", dir=output.parent
        ) as temporary:
            staging = Path(temporary)
            collection = adapter.collect_backup(source, staging, include, report, resolved_home)
            counts = self._archive_counts(report.counts, include, len(collection.included_skills))
            source_home = resolved_home
            if source_home is None and source.name in {".codex", ".claude"}:
                source_home = source.parent
            manifest = ArchiveManifest(
                harness=report.harness,
                harness_version=report.harness_version,
                harness_versions=report.harness_versions,
                compatibility_profiles=report.compatibility_profiles,
                created_at=self._clock(),
                source_platform=platform.system().lower(),
                source_root=str(source),
                source_home=str(source_home) if source_home else None,
                counts=counts,
                capabilities=report.capabilities,
                native_schema=report.native_schema,
            )
            (staging / "manifest.json").write_bytes(
                canonical_json(manifest.model_dump(mode="json", exclude_none=True))
            )
            project_values = report.projects if "sessions" in include else []
            (staging / "projects.json").write_bytes(
                canonical_json(
                    {"projects": [item.model_dump(mode="json") for item in project_values]}
                )
            )
            (staging / "skills.json").write_bytes(
                canonical_json(
                    {
                        "skills": [item.model_dump(mode="json") for item in report.skills],
                        "included": [item.source_path for item in collection.included_skills],
                    }
                )
            )
            payloads = self._payload_inventory(staging, report.harness.value)
            (staging / "payloads.json").write_bytes(
                canonical_json(PayloadsDocument(payloads=payloads).model_dump(mode="json"))
            )
            timestamps = {
                path.relative_to(staging).as_posix(): path.stat().st_mtime_ns
                for path in stage_entries(staging / "native")
                if path.is_file() and not path.is_symlink()
            }
            (staging / "timestamps.json").write_bytes(
                canonical_json(TimestampsDocument(entries=timestamps).model_dump(mode="json"))
            )
            archived_files, archived_bytes = self._archive_writer.write(staging, output)
        return BackupResult(
            output=str(output),
            manifest=manifest,
            archived_files=archived_files,
            archived_bytes=archived_bytes,
        )

    @staticmethod
    def _archive_counts(
        source: CountSummary, include: frozenset[str], included_skills: int
    ) -> CountSummary:
        return CountSummary(
            conversations=source.conversations if "sessions" in include else 0,
            subagent_transcripts=(source.subagent_transcripts if "sessions" in include else 0),
            transcript_files=source.transcript_files if "sessions" in include else 0,
            projects=source.projects if "sessions" in include else 0,
            attachments=source.attachments if "sessions" in include else 0,
            skills=included_skills if "skills" in include else 0,
        )

    @staticmethod
    def _payload_inventory(staging: Path, harness: str) -> list[PayloadRecord]:
        records: list[PayloadRecord] = []
        native = staging / "native"
        if not native.exists():
            return records
        project_by_container: dict[str, str] = {}
        if harness == "claude-code":
            projects_root = native / "sessions" / "projects"
            if projects_root.is_dir():
                for transcript in projects_root.rglob("*.jsonl"):
                    summary = inspect_jsonl(transcript, harness)
                    transcript_relative = transcript.relative_to(projects_root)
                    if transcript_relative.parts and len(summary.project_paths) == 1:
                        project_by_container[transcript_relative.parts[0]] = next(
                            iter(summary.project_paths)
                        )
        for path in stage_entries(native):
            member = path.relative_to(staging).as_posix()
            relative_name = path.relative_to(native).as_posix()
            description = describe_path(path)
            if relative_name.startswith("state/"):
                role = PayloadRole.DATABASE
                strategy = "sqlite-merge"
                original = relative_name.removeprefix("state/")
            elif relative_name.startswith("skills/"):
                role = PayloadRole.SKILL
                strategy = "skill-directory"
                original = relative_name.removeprefix("skills/")
            elif relative_name.endswith(("history.jsonl", "session_index.jsonl")):
                role = PayloadRole.INDEX
                strategy = "jsonl-merge"
                original = relative_name.removeprefix("sessions/")
            elif not relative_name.endswith(".jsonl") or (
                harness == "claude-code" and not _is_claude_transcript_member(relative_name)
            ):
                role = PayloadRole.ATTACHMENT
                strategy = "copy-if-absent"
                original = relative_name.removeprefix("sessions/")
            else:
                role = PayloadRole.TRANSCRIPT
                strategy = "native-session"
                original = relative_name.removeprefix("sessions/")
            project_path = None
            if role is PayloadRole.TRANSCRIPT:
                summary = inspect_jsonl(path, harness)
                if len(summary.project_paths) == 1:
                    project_path = next(iter(summary.project_paths))
            if harness == "claude-code" and original.startswith("projects/"):
                parts = original.split("/")
                if len(parts) > 1:
                    project_path = project_by_container.get(parts[1], project_path)
            records.append(
                PayloadRecord(
                    member=member,
                    role=role,
                    original_path=original,
                    destination_scope=(
                        PayloadScope.USER_HOME
                        if role is PayloadRole.SKILL and harness == "codex"
                        else PayloadScope.HARNESS
                    ),
                    strategy=strategy,
                    project_path=project_path,
                    sha256=description.sha256,
                    mode=description.mode,
                )
            )
        return records
