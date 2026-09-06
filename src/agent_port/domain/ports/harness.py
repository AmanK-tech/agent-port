from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from agent_port.domain.models import (
    ArchiveManifest,
    DetectionResult,
    Diagnostic,
    HarnessName,
    InspectionReport,
    PayloadRecord,
    PlannedOperation,
    ProjectMapping,
    RestoreConflict,
    RestorePlan,
    SkillConflictPolicy,
    SkillRecord,
    VerificationReport,
)


@dataclass(frozen=True)
class BackupCollection:
    included_skills: list[SkillRecord] = field(default_factory=list)


@dataclass(frozen=True)
class RestorePlanningContext:
    archive: Path
    extracted: Path
    manifest: ArchiveManifest
    payloads: list[PayloadRecord]
    destination: Path
    destination_home: Path
    projects: list[ProjectMapping]
    skills: list[SkillRecord]
    skill_policies: dict[str, SkillConflictPolicy]
    source_mtimes: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterRestorePlan:
    operations: list[PlannedOperation] = field(default_factory=list)
    conflicts: list[RestoreConflict] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


class HarnessAdapter(Protocol):
    name: HarnessName

    def detect(self, source: Path) -> DetectionResult: ...

    def inspect(self, source: Path, user_home: Path | None = None) -> InspectionReport: ...

    def collect_backup(
        self,
        source: Path,
        staging: Path,
        include: frozenset[str],
        report: InspectionReport,
        user_home: Path | None = None,
    ) -> BackupCollection: ...

    def plan_restore(self, context: RestorePlanningContext) -> AdapterRestorePlan: ...

    def stage_restore(self, plan: RestorePlan, extracted: Path, staging: Path) -> None: ...

    def apply_restore(self, plan: RestorePlan, staging: Path) -> None: ...

    def verify_restore(self, plan: RestorePlan) -> VerificationReport: ...

    def register_projects(self, projects: list[ProjectMapping]) -> list[str]: ...
