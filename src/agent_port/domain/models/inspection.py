from __future__ import annotations

from pydantic import Field

from agent_port import __version__
from agent_port.domain.models.common import (
    CapabilitySet,
    CountSummary,
    Diagnostic,
    HarnessName,
    NativeSchema,
    SkillOwner,
    SkillScope,
    SourceKind,
    StrictModel,
)


class ProjectRecord(StrictModel):
    path: str
    exists: bool
    conversation_count: int = 0
    repository_fingerprint: str | None = None


class SkillRecord(StrictModel):
    name: str
    scope: SkillScope
    owner: SkillOwner
    source_path: str
    manifest_path: str = "SKILL.md"
    files: int = 0
    checksum: str
    license: str = "unknown"
    eligible: bool = False
    warnings: list[Diagnostic] = Field(default_factory=list)


class ExcludedPathRecord(StrictModel):
    path: str
    reason: str


class InspectionReport(StrictModel):
    source: str
    source_kind: SourceKind = SourceKind.HARNESS_HOME
    harness: HarnessName
    harness_version: str | None = None
    harness_versions: list[str] = Field(default_factory=list)
    compatibility_profiles: list[str] = Field(default_factory=list)
    adapter_version: str = __version__
    capabilities: CapabilitySet = Field(default_factory=CapabilitySet)
    counts: CountSummary = Field(default_factory=CountSummary)
    native_schema: list[NativeSchema] = Field(default_factory=list)
    projects: list[ProjectRecord] = Field(default_factory=list)
    skills: list[SkillRecord] = Field(default_factory=list)
    exclusions: list[ExcludedPathRecord] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
