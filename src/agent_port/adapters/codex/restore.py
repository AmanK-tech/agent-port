from __future__ import annotations

import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from agent_port.adapters.restore_support import (
    destination_session_ids,
    jsonl_merge_conflicts,
    merge_jsonl,
    merge_sqlite,
    plan_skills,
    project_mapper,
    sha256_bytes,
    sha256_file,
    sqlite_migration_version,
    stage_payload_tree,
    transform_jsonl,
    verify_expected_payloads,
)
from agent_port.domain.models import (
    Diagnostic,
    OperationKind,
    PlannedOperation,
    ProjectMapping,
    RestoreConflict,
    RestorePlan,
    Severity,
    VerificationReport,
)
from agent_port.domain.ports import AdapterRestorePlan, RestorePlanningContext
from agent_port.infrastructure.archive.common import validate_member_name
from agent_port.infrastructure.filesystem.jsonl import inspect_jsonl
from agent_port.infrastructure.path_mapping import PathMapper

SUPPORTED_MIGRATIONS = {(39, 39), (39, 40), (40, 40)}


def plan_restore(context: RestorePlanningContext) -> AdapterRestorePlan:
    mapper = project_mapper(context)
    operations: list[PlannedOperation] = []
    conflicts: list[RestoreConflict] = []
    diagnostics: list[Diagnostic] = []
    destination_ids = destination_session_ids(context.destination, "codex")
    database_payloads = []

    for payload in context.payloads:
        validate_member_name(payload.member)
        source = context.extracted / Path(*payload.member.split("/"))
        if payload.role.value == "skill":
            continue
        if payload.role.value == "database":
            database_payloads.append(payload)
            continue
        validate_member_name(payload.original_path)
        destination = context.destination / Path(*payload.original_path.split("/"))
        if payload.role.value == "index":
            transformed = transform_jsonl(source, "codex", mapper)
            merge_conflicts = jsonl_merge_conflicts(transformed, destination)
            if merge_conflicts:
                conflicts.append(
                    RestoreConflict(
                        kind="jsonl-index-collision",
                        identity=", ".join(merge_conflicts),
                        destination=str(destination),
                        message="Destination index contains different records for existing IDs.",
                    )
                )
                continue
            operations.append(
                PlannedOperation(
                    kind=OperationKind.MERGE_JSONL,
                    member=payload.member,
                    destination=str(destination),
                )
            )
            continue
        if payload.role.value == "transcript":
            transformed = transform_jsonl(source, "codex", mapper)
            summary = inspect_jsonl(source, "codex")
            if len(summary.session_ids) != 1:
                conflicts.append(
                    RestoreConflict(
                        kind="ambiguous-session-id",
                        identity=payload.original_path,
                        destination=str(destination),
                        message="Codex transcript must contain exactly one thread ID.",
                    )
                )
                continue
            identity = next(iter(summary.session_ids))
            existing = destination_ids.get(identity)
            if existing is not None:
                if existing.read_bytes() == transformed:
                    operations.append(
                        PlannedOperation(
                            kind=OperationKind.SKIP,
                            member=payload.member,
                            source=payload.original_path,
                            destination=str(existing),
                            identity=identity,
                            expected_sha256=sha256_bytes(transformed),
                        )
                    )
                else:
                    conflicts.append(
                        RestoreConflict(
                            kind="session-id-collision",
                            identity=identity,
                            destination=str(existing),
                            message="Destination contains different transcript data for this ID.",
                        )
                    )
                continue
            expected = sha256_bytes(transformed)
            if destination.exists() and sha256_file(destination) != expected:
                conflicts.append(
                    RestoreConflict(
                        kind="session-path-collision",
                        identity=identity,
                        destination=str(destination),
                        message="Destination transcript path already contains different data.",
                    )
                )
                continue
            operations.append(
                PlannedOperation(
                    kind=OperationKind.COPY,
                    member=payload.member,
                    source=payload.original_path,
                    destination=str(destination),
                    identity=identity,
                    expected_sha256=expected,
                )
            )
            continue
        if destination.exists() or destination.is_symlink():
            source_hash = sha256_file(source) if source.is_file() else payload.sha256
            destination_hash = sha256_file(destination) if destination.is_file() else None
            if source_hash == destination_hash:
                operations.append(
                    PlannedOperation(
                        kind=OperationKind.SKIP,
                        member=payload.member,
                        destination=str(destination),
                        expected_sha256=source_hash,
                    )
                )
            else:
                conflicts.append(
                    RestoreConflict(
                        kind="payload-path-collision",
                        identity=payload.original_path,
                        destination=str(destination),
                        message="Destination contains different attachment or sidecar data.",
                    )
                )
        else:
            operations.append(
                PlannedOperation(
                    kind=OperationKind.COPY,
                    member=payload.member,
                    destination=str(destination),
                    expected_sha256=payload.sha256,
                )
            )

    if len(database_payloads) > 1:
        conflicts.append(
            RestoreConflict(
                kind="ambiguous-codex-database",
                identity="database",
                destination=str(context.destination),
                message="Codex restore supports exactly one state database per archive.",
            )
        )
    elif database_payloads:
        payload = database_payloads[0]
        source = context.extracted / Path(*payload.member.split("/"))
        candidates = sorted(context.destination.glob("state*.sqlite"))
        exact = context.destination / Path(payload.original_path).name
        database_destination = (
            exact if exact.is_file() else (candidates[0] if len(candidates) == 1 else None)
        )
        if database_destination is None:
            conflicts.append(
                RestoreConflict(
                    kind="missing-codex-database",
                    identity="database",
                    destination=str(context.destination),
                    message="Initialize Codex once so a destination state database exists.",
                )
            )
        else:
            source_version = sqlite_migration_version(source)
            destination_version = sqlite_migration_version(database_destination)
            if (source_version, destination_version) not in SUPPORTED_MIGRATIONS:
                conflicts.append(
                    RestoreConflict(
                        kind="unsupported-codex-schema",
                        identity=f"{source_version}->{destination_version}",
                        destination=str(database_destination),
                        message="Supported Codex migrations are 39→39, 39→40, and 40→40.",
                    )
                )
            else:
                database_conflicts = _database_conflicts(
                    source,
                    database_destination,
                    mapper,
                    _rollout_destinations(source, operations),
                )
                conflicts.extend(database_conflicts)
                operations.append(
                    PlannedOperation(
                        kind=OperationKind.MERGE_DATABASE,
                        member=payload.member,
                        destination=str(database_destination),
                        identity=f"{source_version}->{destination_version}",
                    )
                )
                diagnostics.append(
                    Diagnostic(
                        code="experimental-codex-database-restore",
                        severity=Severity.WARNING,
                        message="Codex SQLite restoration is experimental and schema-gated.",
                        path=str(database_destination),
                    )
                )

    skills = plan_skills(context)
    operations.extend(skills.operations)
    conflicts.extend(skills.conflicts)
    diagnostics.extend(skills.diagnostics)
    return AdapterRestorePlan(operations=operations, conflicts=conflicts, diagnostics=diagnostics)


def stage_restore(plan: RestorePlan, extracted: Path, staging: Path) -> None:
    mapper = PathMapper(plan.mappings)
    staged_members: set[str] = set()
    for operation in plan.operations:
        if operation.kind is OperationKind.SKIP or operation.member is None:
            continue
        if operation.member in staged_members:
            continue
        staged_members.add(operation.member)
        source = extracted / Path(*operation.member.split("/"))
        destination = staging / Path(*operation.member.split("/"))
        if operation.member.endswith(".jsonl") and source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(transform_jsonl(source, "codex", mapper))
        else:
            stage_payload_tree(source, destination)


def apply_restore(plan: RestorePlan, staging: Path) -> None:
    mapper = PathMapper(plan.mappings)
    for operation in plan.operations:
        if operation.member is None:
            continue
        source = staging / Path(*operation.member.split("/"))
        destination = Path(operation.destination)
        if operation.kind is OperationKind.MERGE_JSONL:
            merge_jsonl(source, destination)
        elif operation.kind is OperationKind.MERGE_DATABASE:
            rollout_paths = _rollout_destinations(source, plan.operations)
            merge_sqlite(
                source,
                destination,
                lambda value: mapper.apply(value).value,
                rollout_paths,
            )


def verify_restore(adapter: object, plan: RestorePlan) -> VerificationReport:
    payload_verification = verify_expected_payloads(plan.operations, "codex")
    checks = list(payload_verification.checks)
    diagnostics = list(payload_verification.diagnostics)
    valid = payload_verification.valid
    for operation in plan.operations:
        if operation.kind is OperationKind.SKIP:
            continue
        destination = Path(operation.destination)
        if not destination.exists() and not destination.is_symlink():
            valid = False
            diagnostics.append(
                Diagnostic(
                    code="missing-restored-path",
                    severity=Severity.ERROR,
                    message="Planned restored path is missing.",
                    path=str(destination),
                )
            )
            continue
        if operation.member and operation.member.endswith(".jsonl"):
            summary = inspect_jsonl(destination, "codex")
            if any(item.severity is Severity.ERROR for item in summary.diagnostics):
                valid = False
                diagnostics.extend(summary.diagnostics)
    for database in Path(plan.destination).glob("state*.sqlite"):
        try:
            with closing(sqlite3.connect(database)) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            if not integrity or integrity[0] != "ok" or foreign:
                valid = False
                diagnostics.append(
                    Diagnostic(
                        code="database-verification-failed",
                        severity=Severity.ERROR,
                        message="Codex database integrity or foreign-key verification failed.",
                        path=str(database),
                    )
                )
            else:
                checks.append(f"SQLite integrity: {database.name}")
                if any(
                    operation.kind is OperationKind.MERGE_DATABASE
                    and Path(operation.destination) == database
                    for operation in plan.operations
                ):
                    with closing(sqlite3.connect(database)) as connection:
                        thread_ids = {
                            row[0] for row in connection.execute("SELECT id FROM threads")
                        }
                        rollout_paths = {
                            row[0]: row[1]
                            for row in connection.execute("SELECT id, rollout_path FROM threads")
                        }
                    expected_ids = {
                        operation.identity
                        for operation in plan.operations
                        if operation.identity
                        and operation.member
                        and operation.member.endswith(".jsonl")
                    }
                    missing_ids = expected_ids.difference(thread_ids)
                    missing_rollouts = [
                        identity
                        for identity in expected_ids
                        if identity in rollout_paths and not Path(rollout_paths[identity]).is_file()
                    ]
                    if missing_ids or missing_rollouts:
                        valid = False
                        diagnostics.append(
                            Diagnostic(
                                code="codex-thread-verification-failed",
                                severity=Severity.ERROR,
                                message="Restored thread rows or rollout paths are incomplete.",
                                path=str(database),
                            )
                        )
        except sqlite3.DatabaseError as error:
            valid = False
            diagnostics.append(
                Diagnostic(
                    code="database-verification-failed",
                    severity=Severity.ERROR,
                    message=str(error),
                    path=str(database),
                )
            )
    checks.append("Codex transcript JSONL validated")
    return VerificationReport(valid=valid, checks=checks, diagnostics=diagnostics)


def register_projects(projects: list[ProjectMapping]) -> list[str]:
    warnings: list[str] = []
    for project in projects:
        destination = Path(str(project.destination))
        if not bool(project.exists):
            warnings.append(f"Project does not exist and was not registered: {destination}")
            continue
        try:
            completed = subprocess.run(
                ["codex", "app", str(destination)],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            warnings.append(f"Could not register {destination}: {error}")
            continue
        if completed.returncode:
            warnings.append(f"Could not register {destination}: {completed.stderr.strip()}")
    return warnings


def _rollout_destinations(
    source_database: Path, operations: list[PlannedOperation]
) -> dict[str, str]:
    by_identity: dict[str, list[PlannedOperation]] = {}
    for operation in operations:
        if operation.identity and operation.member and operation.member.endswith(".jsonl"):
            by_identity.setdefault(operation.identity, []).append(operation)
    result = {identity: candidates[-1].destination for identity, candidates in by_identity.items()}
    try:
        with closing(sqlite3.connect(source_database)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(threads)").fetchall()
            }
            if not {"id", "rollout_path"}.issubset(columns):
                return result
            for identity, rollout_path in connection.execute(
                "SELECT id, rollout_path FROM threads"
            ):
                if not isinstance(identity, str) or not isinstance(rollout_path, str):
                    continue
                normalized_rollout = rollout_path.replace("\\", "/")
                for candidate in by_identity.get(identity, []):
                    if candidate.source and normalized_rollout.endswith(
                        candidate.source.replace("\\", "/")
                    ):
                        result[identity] = candidate.destination
                        break
    except sqlite3.DatabaseError:
        return result
    return result


def _database_conflicts(
    source: Path,
    destination: Path,
    mapper: PathMapper,
    rollout_paths: dict[str, str],
) -> list[RestoreConflict]:
    conflicts: list[RestoreConflict] = []
    try:
        with (
            closing(sqlite3.connect(source)) as source_db,
            closing(sqlite3.connect(destination)) as destination_db,
        ):
            source_columns = {
                row[1] for row in source_db.execute("PRAGMA table_info(threads)").fetchall()
            }
            destination_columns = {
                row[1] for row in destination_db.execute("PRAGMA table_info(threads)").fetchall()
            }
            common = sorted(source_columns.intersection(destination_columns))
            if "id" not in common:
                return [
                    RestoreConflict(
                        kind="unsupported-codex-schema",
                        identity="threads.id",
                        destination=str(destination),
                        message="Codex threads table has no compatible ID column.",
                    )
                ]
            quoted = ",".join(f'"{column}"' for column in common)
            destination_rows = {
                row[common.index("id")]: row
                for row in destination_db.execute(f"SELECT {quoted} FROM threads").fetchall()
            }
            for row in source_db.execute(f"SELECT {quoted} FROM threads"):
                values = dict(zip(common, row, strict=True))
                identity = values["id"]
                if not isinstance(identity, str):
                    continue
                if identity not in rollout_paths:
                    conflicts.append(
                        RestoreConflict(
                            kind="missing-thread-transcript",
                            identity=identity,
                            destination=str(destination),
                            message="Database thread has no restorable transcript payload.",
                        )
                    )
                    continue
                if isinstance(values.get("cwd"), str):
                    values["cwd"] = mapper.apply(values["cwd"]).value
                if "rollout_path" in values:
                    values["rollout_path"] = rollout_paths[identity]
                existing = destination_rows.get(identity)
                if existing is not None and tuple(values[column] for column in common) != existing:
                    conflicts.append(
                        RestoreConflict(
                            kind="database-thread-collision",
                            identity=identity,
                            destination=str(destination),
                            message="Destination contains different metadata for this thread ID.",
                        )
                    )
    except sqlite3.DatabaseError as error:
        conflicts.append(
            RestoreConflict(
                kind="codex-database-read-error",
                identity="database",
                destination=str(destination),
                message=f"Cannot compare Codex databases safely: {error}",
            )
        )
    return conflicts
