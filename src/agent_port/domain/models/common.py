from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HarnessName(StrEnum):
    CODEX = "codex"
    CLAUDE_CODE = "claude-code"


class DetectionStatus(StrEnum):
    DETECTED = "detected"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


class SourceKind(StrEnum):
    HARNESS_HOME = "harness-home"
    ARCHIVE = "archive"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class SkillOwner(StrEnum):
    USER = "user"
    PROJECT = "project"
    SYSTEM = "system"
    PLUGIN = "plugin"
    CURATED_CACHE = "curated-cache"
    UNKNOWN = "unknown"


class SkillScope(StrEnum):
    USER = "user"
    PROJECT = "project"
    SYSTEM = "system"
    PLUGIN = "plugin"
    CACHE = "cache"
    UNKNOWN = "unknown"


class Diagnostic(StrictModel):
    code: str
    severity: Severity
    message: str
    path: str | None = None


class CapabilitySet(StrictModel):
    native_backup: bool = True
    native_restore: bool = True
    skills_backup: bool = True
    skills_restore: bool = True
    project_registration: bool = False
    human_readable_export: bool = False


class CountSummary(StrictModel):
    conversations: int | None = None
    subagent_transcripts: int = 0
    transcript_files: int = 0
    projects: int = 0
    attachments: int = 0
    skills: int = 0


class NativeSchema(StrictModel):
    kind: str
    value: str
    recognized: bool = False


class DetectionResult(StrictModel):
    status: DetectionStatus
    harness: HarnessName | None = None
    score: int = Field(default=0, ge=0)
    evidence: list[str] = Field(default_factory=list)
