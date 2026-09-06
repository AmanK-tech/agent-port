from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from agent_port.domain.models.common import CountSummary, Diagnostic, HarnessName, StrictModel


class SkillConflictPolicy(StrEnum):
    ERROR = "error"
    SKIP = "skip"
    REPLACE = "replace"


class OperationKind(StrEnum):
    COPY = "copy"
    SKIP = "skip"
    REPLACE = "replace"
    MERGE_DATABASE = "merge-database"
    MERGE_JSONL = "merge-jsonl"


class PathMapping(StrictModel):
    source: str
    destination: str


class ArchivePrecondition(StrictModel):
    path: str
    sha256: str
    format_version: int
    harness: HarnessName
    counts: CountSummary | None = None


class DestinationPrecondition(StrictModel):
    path: str
    fingerprint: str


class PlannedOperation(StrictModel):
    kind: OperationKind
    member: str | None = None
    source: str | None = None
    destination: str
    identity: str | None = None
    expected_sha256: str | None = None
    policy: SkillConflictPolicy | None = None
    source_mtime_ns: int | None = Field(default=None, ge=0)
    project_source: str | None = None


class RestoreConflict(StrictModel):
    kind: str
    identity: str
    destination: str
    message: str
    blocking: bool = True


class ProjectMapping(StrictModel):
    source: str
    destination: str
    exists: bool
    conversation_count: int = Field(default=0, ge=0)
    accepted_unmapped: bool = False
    repository_fingerprint: str | None = None


class RestorePlan(StrictModel):
    plan_version: Literal[1] = 1
    plan_id: str
    created_at: datetime
    archive: ArchivePrecondition
    adapter_version: str
    destination: str
    destination_home: str
    mappings: list[PathMapping] = Field(default_factory=list)
    accepted_unmapped: list[str] = Field(default_factory=list)
    projects: list[ProjectMapping] = Field(default_factory=list)
    operations: list[PlannedOperation] = Field(default_factory=list)
    conflicts: list[RestoreConflict] = Field(default_factory=list)
    destination_preconditions: list[DestinationPrecondition] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    run_directory: str
    plan_digest: str = ""

    @property
    def ready(self) -> bool:
        return not any(conflict.blocking for conflict in self.conflicts) and not any(
            diagnostic.severity.value == "error" for diagnostic in self.diagnostics
        )


class RestorePlanInfo(StrictModel):
    status: Literal["ready", "blocked"]
    plan_id: str
    plan_path: str
    run_directory: str
    archive: str
    destination: str
    ready: bool
    content: CountSummary = Field(default_factory=CountSummary)
    mappings: list[PathMapping] = Field(default_factory=list)
    suggested_mappings: list[PathMapping] = Field(default_factory=list)
    operation_counts: dict[str, int] = Field(default_factory=dict)
    blockers: list[RestoreConflict] = Field(default_factory=list)
    notices: list[RestoreConflict] = Field(default_factory=list)
    skill_policies: dict[str, SkillConflictPolicy] = Field(default_factory=dict)
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class VerificationReport(StrictModel):
    valid: bool
    checks: list[str] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class RestoreResult(StrictModel):
    plan_id: str
    run_directory: str
    backup: str
    applied: int = Field(ge=0)
    skipped: int = Field(ge=0)
    restart_required: bool = False
    verification: VerificationReport
    registration_warnings: list[str] = Field(default_factory=list)


class RestoreVerificationState(StrEnum):
    VERIFIED = "verified"
    PENDING = "pending"
    CHANGED = "changed"
    FAILED = "failed"
    ROLLED_BACK = "rolled-back"


class HandoffState(StrEnum):
    ARMED = "armed"
    WAITING = "waiting"
    APPLYING = "applying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"


class HarnessProcessRecord(StrictModel):
    pid: int = Field(gt=0)
    created_at: float = Field(gt=0)
    name: str


class RestoreHandoffStatus(StrictModel):
    handoff_version: Literal[1] = 1
    plan_id: str
    plan_path: str
    run_directory: str
    status_path: str
    harness: HarnessName
    state: HandoffState
    armed_at: datetime
    updated_at: datetime
    expires_at: datetime
    observed_processes: list[HarnessProcessRecord] = Field(default_factory=list)
    content: CountSummary = Field(default_factory=CountSummary)
    initial_verification_status: RestoreVerificationState | None = None
    initial_verification_path: str | None = None
    rolled_back: bool = False
    error: str | None = None


class RestoreVerificationCounts(StrictModel):
    planned_operations: int = Field(default=0, ge=0)
    applied_operations: int = Field(default=0, ge=0)
    skipped_operations: int = Field(default=0, ge=0)
    expected_conversations: int = Field(default=0, ge=0)
    verified_conversations: int = Field(default=0, ge=0)
    expected_subagent_transcripts: int = Field(default=0, ge=0)
    verified_subagent_transcripts: int = Field(default=0, ge=0)
    expected_transcript_files: int = Field(default=0, ge=0)
    verified_transcript_files: int = Field(default=0, ge=0)
    expected_projects: int = Field(default=0, ge=0)
    verified_projects: int = Field(default=0, ge=0)
    expected_skills: int = Field(default=0, ge=0)
    verified_skills: int = Field(default=0, ge=0)
    expected_attachments: int = Field(default=0, ge=0)
    verified_attachments: int = Field(default=0, ge=0)

    @property
    def all_expected_verified(self) -> bool:
        return (
            self.verified_conversations == self.expected_conversations
            and self.verified_subagent_transcripts == self.expected_subagent_transcripts
            and self.verified_transcript_files == self.expected_transcript_files
            and self.verified_projects == self.expected_projects
            and self.verified_skills == self.expected_skills
            and self.verified_attachments == self.expected_attachments
        )


class RestoreVerificationResult(StrictModel):
    plan_id: str
    run_directory: str
    status: RestoreVerificationState
    valid: bool
    restore_completed_safely: bool
    current_data_intact: bool
    destination_valid: bool
    initial_verification_status: RestoreVerificationState | None = None
    initial_verification_path: str | None = None
    restart_required: bool = False
    counts: RestoreVerificationCounts = Field(default_factory=RestoreVerificationCounts)
    checks: list[str] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    @property
    def success_gate_passed(self) -> bool:
        return (
            self.status is RestoreVerificationState.VERIFIED
            and self.restore_completed_safely
            and self.current_data_intact
            and self.destination_valid
            and self.counts.all_expected_verified
        )


class JournalEntry(StrictModel):
    destination: str
    backup: str | None = None
    pre_fingerprint: str
    post_fingerprint: str | None = None
    created: bool = False


class RollbackJournal(StrictModel):
    journal_version: Literal[1] = 1
    plan_id: str
    destination: str
    completed: bool = False
    rolled_back: bool = False
    entries: list[JournalEntry] = Field(default_factory=list)


class RollbackResult(StrictModel):
    run_directory: str
    restored: int = Field(ge=0)
    already_rolled_back: bool = False
