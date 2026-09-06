from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from agent_port.domain.models.common import CountSummary, StrictModel


class MigrationStateValue(StrEnum):
    PREPARED = "prepared"
    RECEIVED = "received"
    PLANNED = "planned"
    HANDOFF_ARMED = "handoff-armed"
    RESTORED = "restored"
    VERIFIED = "verified"
    FAILED = "failed"


class MigrationWorkspace(StrictModel):
    migration_id: str
    workspace: str
    archive: str
    pairing_code_file: str
    state_file: str


class MigrationWorkspaceDocument(StrictModel):
    migration_version: Literal[1] = 1
    migration_id: str
    created_at: datetime
    updated_at: datetime
    workspace: str
    archive: str
    pairing_code_file: str | None = None
    current_plan: str | None = None
    handoff_status: str | None = None
    run_directory: str | None = None
    verification_result: str | None = None
    archive_sha256: str | None = None
    harness: str | None = None
    status: MigrationStateValue = MigrationStateValue.PREPARED
    content: CountSummary = Field(default_factory=CountSummary)
    last_error: str | None = None


class TransferHeader(StrictModel):
    protocol_version: Literal[1] = 1
    cli_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
    archive_size: int = Field(ge=0)
    archive_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class TransferAcknowledgement(StrictModel):
    protocol_version: Literal[1] = 1
    cli_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
    archive_size: int = Field(ge=0)
    archive_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class DiscoveryRecord(StrictModel):
    protocol_version: Literal[1] = 1
    session_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    expires_at: int = Field(gt=0)
    host_key_fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]+={0,2}$")
    authentication_tag: str = Field(pattern=r"^[0-9a-f]{64}$")


class TransferOffer(StrictModel):
    pairing_code: str
    hosts: list[str]
    port: int = Field(ge=1, le=65535)
    expires_at: int = Field(gt=0)
    discovery_available: bool


class TransferSendResult(StrictModel):
    archived_bytes: int = Field(ge=0)
    archive_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content: CountSummary = Field(default_factory=CountSummary)


class TransferReceiveResult(StrictModel):
    output: str
    archived_bytes: int = Field(ge=0)
    archive_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    harness: str
    workspace: str | None = None
    state_file: str | None = None
    content: CountSummary = Field(default_factory=CountSummary)
