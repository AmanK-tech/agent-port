from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from agent_port import __version__
from agent_port.domain.models.common import (
    CapabilitySet,
    CountSummary,
    Diagnostic,
    HarnessName,
    NativeSchema,
    StrictModel,
)


class ArchiveEntry(StrictModel):
    sha256: str
    size: int = Field(ge=0)
    type: Literal["file", "symlink"]
    mode: int = Field(ge=0)


class ChecksumsDocument(StrictModel):
    algorithm: Literal["sha256"] = "sha256"
    entries: dict[str, ArchiveEntry]


class TimestampsDocument(StrictModel):
    version: Literal[1] = 1
    entries: dict[str, Annotated[int, Field(strict=True, ge=0)]]


class PayloadRole(StrEnum):
    TRANSCRIPT = "transcript"
    ATTACHMENT = "attachment"
    INDEX = "index"
    DATABASE = "database"
    SKILL = "skill"


class PayloadScope(StrEnum):
    HARNESS = "harness"
    USER_HOME = "user-home"


class PayloadRecord(StrictModel):
    member: str
    role: PayloadRole
    original_path: str
    destination_scope: PayloadScope = PayloadScope.HARNESS
    strategy: str
    project_path: str | None = None
    sha256: str | None = None
    mode: int | None = Field(default=None, ge=0)


class PayloadsDocument(StrictModel):
    payloads: list[PayloadRecord]


class ArchiveManifest(StrictModel):
    format_version: Literal[1, 2] = 2
    harness: HarnessName
    harness_version: str | None = None
    harness_versions: list[str] = Field(default_factory=list)
    compatibility_profiles: list[str] = Field(default_factory=list)
    adapter_version: str = __version__
    created_at: datetime
    source_platform: str
    source_root: str
    source_home: str | None = None
    counts: CountSummary
    capabilities: CapabilitySet = Field(default_factory=CapabilitySet)
    native_schema: list[NativeSchema] = Field(default_factory=list)


class BackupResult(StrictModel):
    output: str
    manifest: ArchiveManifest
    archived_files: int = Field(ge=0)
    archived_bytes: int = Field(ge=0)


class ArchiveInspectionReport(StrictModel):
    source: str
    manifest: ArchiveManifest
    checksums_valid: bool
    archived_files: int = Field(ge=0)
    archived_bytes: int = Field(ge=0)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
