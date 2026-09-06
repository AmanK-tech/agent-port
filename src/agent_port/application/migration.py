from __future__ import annotations

import os
import stat
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent_port.domain.errors import TransferError
from agent_port.domain.models import (
    MigrationStateValue,
    MigrationWorkspace,
    MigrationWorkspaceDocument,
)
from agent_port.infrastructure.archive.common import canonical_json

DEFAULT_MIGRATION_ROOT = Path.home() / "Agent-Port" / "Migrations"
MIGRATION_STATE_NAME = "migration.json"
MAX_PAIRING_CODE_BYTES = 256


class MigrationWorkspaceService:
    def execute(
        self,
        root: Path | None = None,
        workspace: Path | None = None,
    ) -> MigrationWorkspace:
        now = datetime.now(UTC)
        if workspace is None:
            root_path = (root or DEFAULT_MIGRATION_ROOT).expanduser().resolve()
            _mkdir_private(root_path)
            migration_id = f"{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
            workspace_path = root_path / migration_id
            workspace_path.mkdir(mode=0o700)
            _restrict_directory(workspace_path)
            document = MigrationWorkspaceDocument(
                migration_id=migration_id,
                created_at=now,
                updated_at=now,
                workspace=str(workspace_path),
                archive=str(workspace_path / "source.agentpack"),
            )
        else:
            if root is not None:
                raise TransferError("--root and --workspace cannot be used together.")
            workspace_path = workspace.expanduser().resolve()
            document = load_migration_document(workspace_path)
            _validate_workspace(workspace_path, document)

        _remove_previous_pairing_file(workspace_path, document.pairing_code_file)

        pairing_path = workspace_path / f".pairing-code-{uuid.uuid4().hex[:8]}"
        document = document.model_copy(
            update={
                "updated_at": now,
                "pairing_code_file": str(pairing_path),
                "status": MigrationStateValue.PREPARED,
                "last_error": None,
            }
        )
        write_migration_document(document)
        return MigrationWorkspace(
            migration_id=document.migration_id,
            workspace=document.workspace,
            archive=document.archive,
            pairing_code_file=str(pairing_path),
            state_file=str(workspace_path / MIGRATION_STATE_NAME),
        )


def consume_pairing_code_file(path: Path, private_workspace: Path | None = None) -> str:
    resolved = path.expanduser().absolute()
    trusted_workspace = (
        private_workspace.expanduser().resolve() if private_workspace is not None else None
    )
    workspace_protected = False
    if trusted_workspace is not None:
        try:
            workspace_info = trusted_workspace.stat()
        except OSError as error:
            raise TransferError(f"Cannot inspect private migration workspace: {error}") from error
        workspace_protected = (
            stat.S_ISDIR(workspace_info.st_mode) and resolved.parent == trusted_workspace
        )
        if hasattr(os, "getuid"):
            workspace_protected = workspace_protected and workspace_info.st_uid == os.getuid()
        if os.name != "nt":
            workspace_protected = workspace_protected and not (
                stat.S_IMODE(workspace_info.st_mode) & 0o077
            )
        if not workspace_protected:
            raise TransferError(
                "Pairing-code file is not inside the current user's private migration workspace."
            )
    descriptor: int | None = None
    initial: os.stat_result | None = None
    try:
        initial = resolved.lstat()
        if not stat.S_ISREG(initial.st_mode) or stat.S_ISLNK(initial.st_mode):
            raise TransferError("Pairing-code file must be a regular file, not a symlink.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved, flags)
        info = os.fstat(descriptor)
        if (initial.st_dev, initial.st_ino) != (info.st_dev, info.st_ino):
            raise TransferError("Pairing-code file changed while it was being opened.")
        if not stat.S_ISREG(info.st_mode):
            raise TransferError("Pairing-code file must be a regular file, not a symlink.")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise TransferError("Pairing-code file must be owned by the current user.")
        if os.name != "nt" and not workspace_protected and stat.S_IMODE(info.st_mode) & 0o077:
            raise TransferError("Pairing-code file permissions must be 0600 or stricter.")
        if sys.platform != "win32" and workspace_protected:
            os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
        if info.st_size <= 0 or info.st_size > MAX_PAIRING_CODE_BYTES:
            raise TransferError("Pairing-code file has an invalid size.")
        try:
            with os.fdopen(descriptor, "rb") as source:
                descriptor = None
                value = source.read(MAX_PAIRING_CODE_BYTES + 1).decode("utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise TransferError(f"Cannot read pairing-code file: {error}") from error
        if not value:
            raise TransferError("Pairing-code file is empty.")
        return value
    except OSError as error:
        raise TransferError(f"Cannot inspect pairing-code file: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _unlink_if_unchanged(resolved, initial)


def load_migration_document(workspace: Path) -> MigrationWorkspaceDocument:
    workspace_path = workspace.expanduser().resolve()
    state_path = workspace_path / MIGRATION_STATE_NAME
    try:
        document = MigrationWorkspaceDocument.model_validate_json(state_path.read_bytes())
    except (OSError, ValidationError) as error:
        raise TransferError(f"Invalid migration workspace {workspace}: {error}") from error
    _validate_workspace(workspace_path, document)
    return document


def find_migration_document(path: Path) -> MigrationWorkspaceDocument | None:
    workspace = path.expanduser().resolve()
    if workspace.is_file() or workspace.suffix:
        workspace = workspace.parent
    state_path = workspace / MIGRATION_STATE_NAME
    if not state_path.is_file():
        return None
    try:
        document = MigrationWorkspaceDocument.model_validate_json(state_path.read_bytes())
    except (OSError, ValidationError):
        return None
    return document if Path(document.workspace) == workspace else None


def update_migration_document(
    document: MigrationWorkspaceDocument,
    **updates: Any,
) -> MigrationWorkspaceDocument:
    updated = document.model_copy(
        update={"updated_at": datetime.now(UTC), **updates},
    )
    write_migration_document(updated)
    return updated


def update_migration_for_path(path: Path, **updates: Any) -> MigrationWorkspaceDocument | None:
    document = find_migration_document(path)
    return update_migration_document(document, **updates) if document else None


def next_restore_plan_path(archive: Path) -> Path | None:
    document = find_migration_document(archive)
    if document is None:
        return None
    workspace = Path(document.workspace)
    used: set[int] = set()
    for candidate in workspace.glob("restore-plan-*.json"):
        suffix = candidate.stem.removeprefix("restore-plan-")
        if suffix.isdigit():
            used.add(int(suffix))
    number = 1
    while number in used:
        number += 1
    return workspace / f"restore-plan-{number:03d}.json"


def write_migration_document(document: MigrationWorkspaceDocument) -> None:
    state_path = Path(document.workspace) / MIGRATION_STATE_NAME
    temporary = state_path.with_name(f".{state_path.name}.{uuid.uuid4().hex}.tmp")
    data = canonical_json(document.model_dump(mode="json", exclude_none=True))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, state_path)
        _restrict_file(state_path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_workspace(path: Path, document: MigrationWorkspaceDocument) -> None:
    if not path.is_dir() or Path(document.workspace) != path:
        raise TransferError("Migration workspace does not match its migration.json document.")
    _restrict_directory(path)


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _restrict_directory(path)


def _restrict_directory(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)


def _restrict_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def _remove_previous_pairing_file(workspace: Path, value: str | None) -> None:
    if value is None:
        return
    candidate = Path(value).expanduser().absolute()
    if candidate.parent == workspace and candidate.name.startswith(".pairing-code-"):
        candidate.unlink(missing_ok=True)


def _unlink_if_unchanged(path: Path, initial: os.stat_result | None) -> None:
    if initial is None:
        return
    try:
        current = path.lstat()
    except OSError:
        return
    if (initial.st_dev, initial.st_ino) == (current.st_dev, current.st_ino):
        path.unlink(missing_ok=True)
