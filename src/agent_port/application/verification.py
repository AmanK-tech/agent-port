from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from agent_port.adapters.codex.restore import _rollout_destinations
from agent_port.adapters.restore_support import (
    jsonl_identity,
    transform_jsonl,
    verify_expected_payloads,
)
from agent_port.application.handoff import handoff_status_path, load_handoff_status
from agent_port.application.migration import update_migration_for_path
from agent_port.application.registry import AdapterRegistry
from agent_port.application.restore import (
    DoctorService,
    _atomic_json,
    _load_plan,
    _plan_digest,
    _sha256_file,
)
from agent_port.domain.errors import AgentPortError, RestoreError
from agent_port.domain.models import (
    Diagnostic,
    HandoffState,
    MigrationStateValue,
    OperationKind,
    PayloadRole,
    PayloadsDocument,
    PlannedOperation,
    RestorePlan,
    RestoreResult,
    RestoreVerificationCounts,
    RestoreVerificationResult,
    RestoreVerificationState,
    RollbackJournal,
    Severity,
    VerificationReport,
)
from agent_port.infrastructure.archive import AgentPackReader
from agent_port.infrastructure.filesystem.jsonl import inspect_jsonl
from agent_port.infrastructure.path_mapping import PathMapper


class RestoreVerificationService:
    def __init__(
        self,
        registry: AdapterRegistry | None = None,
        reader: AgentPackReader | None = None,
    ) -> None:
        self._registry = registry or AdapterRegistry()
        self._reader = reader or AgentPackReader()

    def execute(
        self,
        run_directory: Path,
        legacy_plan_path: Path | None = None,
        snapshot: Literal["initial", "current"] = "current",
    ) -> RestoreVerificationResult:
        run_directory = run_directory.expanduser().resolve()
        if not run_directory.is_dir():
            return self._without_run(run_directory)

        diagnostics: list[Diagnostic] = []
        checks: list[str] = []
        plan_path = run_directory / "plan.json"
        if not plan_path.is_file():
            if legacy_plan_path is None:
                raise RestoreError(
                    "Legacy restore run has no plan snapshot; provide --plan RESTORE_PLAN.json."
                )
            plan_path = legacy_plan_path.expanduser().resolve()
        try:
            plan = _load_plan(plan_path)
            result = RestoreResult.model_validate_json((run_directory / "result.json").read_bytes())
            journal = RollbackJournal.model_validate_json(
                (run_directory / "journal.json").read_bytes()
            )
            recorded_verification = VerificationReport.model_validate_json(
                (run_directory / "verification.json").read_bytes()
            )
        except (OSError, ValidationError, RestoreError) as error:
            diagnostics.append(
                _error("invalid-restore-run", f"Restore evidence is invalid: {error}")
            )
            return self._result(
                run_directory,
                run_directory.name,
                RestoreVerificationState.FAILED,
                False,
                False,
                False,
                diagnostics=diagnostics,
            )

        historical_valid = self._verify_recorded_run(
            run_directory, plan, result, journal, recorded_verification, checks, diagnostics
        )
        counts = RestoreVerificationCounts(
            planned_operations=len(plan.operations),
            applied_operations=result.applied,
            skipped_operations=result.skipped,
            expected_conversations=sum(project.conversation_count for project in plan.projects),
        )
        archive_counts = plan.archive.counts
        if archive_counts is None:
            try:
                archive = Path(plan.archive.path)
                if _sha256_file(archive) == plan.archive.sha256:
                    archive_counts = self._reader.inspect(archive).manifest.counts
            except AgentPortError:
                pass
        if archive_counts is not None:
            counts = counts.model_copy(
                update={
                    "expected_conversations": archive_counts.conversations or 0,
                    "expected_subagent_transcripts": archive_counts.subagent_transcripts,
                    "expected_transcript_files": archive_counts.transcript_files,
                    "expected_projects": archive_counts.projects,
                }
            )
        if journal.rolled_back:
            counts = RestoreVerificationCounts(
                planned_operations=counts.planned_operations,
                applied_operations=counts.applied_operations,
                skipped_operations=counts.skipped_operations,
            )
            diagnostics.append(
                Diagnostic(
                    code="restore-rolled-back",
                    severity=Severity.INFO,
                    message="This restore was rolled back; migrated data is no longer expected.",
                )
            )
            return self._record_result(
                self._result(
                    run_directory,
                    plan.plan_id,
                    RestoreVerificationState.ROLLED_BACK,
                    historical_valid,
                    False,
                    self._doctor(plan, checks, diagnostics),
                    restart_required=False,
                    counts=counts,
                    checks=checks,
                    diagnostics=diagnostics,
                ),
                plan,
                snapshot,
            )
        if not historical_valid:
            return self._record_result(
                self._result(
                    run_directory,
                    plan.plan_id,
                    RestoreVerificationState.FAILED,
                    False,
                    False,
                    False,
                    restart_required=result.restart_required,
                    counts=counts,
                    checks=checks,
                    diagnostics=diagnostics,
                ),
                plan,
                snapshot,
            )

        current_intact, counts = self._verify_current_data(
            run_directory, plan, result, counts, checks, diagnostics
        )
        if not counts.all_expected_verified:
            current_intact = False
            mismatches = "; ".join(
                f"{name.removeprefix('expected_')}: expected {expected}, "
                f"verified {getattr(counts, name.replace('expected_', 'verified_', 1))}"
                for name, expected in counts.model_dump().items()
                if name.startswith("expected_")
                and expected != getattr(counts, name.replace("expected_", "verified_", 1))
            )
            diagnostics.append(
                _error(
                    "incomplete-verification-counts",
                    f"Restore content counts do not match ({mismatches}).",
                )
            )
        destination_valid = self._doctor(plan, checks, diagnostics)
        state = (
            RestoreVerificationState.VERIFIED
            if current_intact and destination_valid
            else RestoreVerificationState.CHANGED
        )
        return self._record_result(
            self._result(
                run_directory,
                plan.plan_id,
                state,
                True,
                current_intact,
                destination_valid,
                restart_required=result.restart_required,
                counts=counts,
                checks=checks,
                diagnostics=diagnostics,
            ),
            plan,
            snapshot,
        )

    def _without_run(self, run_directory: Path) -> RestoreVerificationResult:
        status_path = handoff_status_path(run_directory)
        if not status_path.is_file():
            raise RestoreError(f"Restore run directory does not exist: {run_directory}")
        status = load_handoff_status(status_path)
        if status.state in {HandoffState.ARMED, HandoffState.WAITING, HandoffState.APPLYING}:
            state = RestoreVerificationState.PENDING
            severity = Severity.INFO
        else:
            state = RestoreVerificationState.FAILED
            severity = Severity.ERROR
        message = f"Restore handoff is {status.state.value}."
        if status.error:
            message = f"{message} {status.error}"
        return self._result(
            run_directory,
            status.plan_id,
            state,
            False,
            False,
            False,
            diagnostics=[
                Diagnostic(code=f"handoff-{status.state.value}", severity=severity, message=message)
            ],
        )

    def _verify_recorded_run(
        self,
        run_directory: Path,
        plan: RestorePlan,
        result: RestoreResult,
        journal: RollbackJournal,
        recorded_verification: VerificationReport,
        checks: list[str],
        diagnostics: list[Diagnostic],
    ) -> bool:
        valid = True

        def require(condition: bool, code: str, message: str) -> None:
            nonlocal valid
            if condition:
                checks.append(message)
            else:
                valid = False
                diagnostics.append(_error(code, message))

        require(
            plan.plan_digest == _plan_digest(plan),
            "plan-digest-invalid",
            "Plan digest verified",
        )
        require(plan.plan_id == result.plan_id, "result-plan-mismatch", "Result matches plan ID")
        require(plan.plan_id == journal.plan_id, "journal-plan-mismatch", "Journal matches plan ID")
        require(
            Path(plan.run_directory).resolve() == run_directory,
            "run-directory-mismatch",
            "Run directory matches the saved plan",
        )
        require(
            Path(result.run_directory).resolve() == run_directory,
            "result-directory-mismatch",
            "Result references this run directory",
        )
        require(
            journal.destination == plan.destination,
            "journal-destination-mismatch",
            "Journal destination matches the plan",
        )
        require(journal.completed, "incomplete-restore-journal", "Restore journal is complete")
        require(
            recorded_verification == result.verification,
            "verification-record-mismatch",
            "Recorded verification matches the apply result",
        )
        require(
            result.verification.valid,
            "apply-verification-failed",
            "Apply-time verification passed",
        )
        expected_applied = sum(
            operation.kind is not OperationKind.SKIP for operation in plan.operations
        )
        expected_skipped = len(plan.operations) - expected_applied
        require(
            result.applied == expected_applied and result.skipped == expected_skipped,
            "operation-count-mismatch",
            "Applied and skipped operation counts match the plan",
        )
        expected_destinations = {
            operation.destination
            for operation in plan.operations
            if operation.kind is not OperationKind.SKIP
        }
        journal_destinations = {entry.destination for entry in journal.entries}
        require(
            expected_destinations == journal_destinations,
            "journal-operation-mismatch",
            "Journal covers every mutated destination",
        )
        require(
            all(entry.post_fingerprint is not None for entry in journal.entries)
            if journal.completed
            else True,
            "missing-post-restore-fingerprint",
            "Journal contains post-restore fingerprints",
        )
        backup = run_directory / "destination-before.agentpack"
        require(
            Path(result.backup).resolve() == backup,
            "backup-path-mismatch",
            "Destination backup path matches the run",
        )
        try:
            backup_report = self._reader.inspect(backup)
            require(
                backup_report.checksums_valid
                and backup_report.manifest.harness is plan.archive.harness,
                "destination-backup-invalid",
                "Pre-restore destination backup is verified",
            )
        except AgentPortError as error:
            valid = False
            diagnostics.append(_error("destination-backup-invalid", str(error)))
        archive = Path(plan.archive.path)
        if archive.is_file():
            if _sha256_file(archive) == plan.archive.sha256:
                checks.append("Original migration archive still matches the plan")
            else:
                diagnostics.append(
                    Diagnostic(
                        code="original-archive-changed",
                        severity=Severity.WARNING,
                        message=(
                            "The original archive changed after restore; retained run evidence "
                            "was used."
                        ),
                        path=str(archive),
                    )
                )
        return valid

    def _verify_current_data(
        self,
        run_directory: Path,
        plan: RestorePlan,
        result: RestoreResult,
        counts: RestoreVerificationCounts,
        checks: list[str],
        diagnostics: list[Diagnostic],
    ) -> tuple[bool, RestoreVerificationCounts]:
        payload_path = run_directory / "payloads.json"
        try:
            if payload_path.is_file():
                payloads = PayloadsDocument.model_validate_json(payload_path.read_bytes())
            elif plan.archive.format_version == 2 and Path(plan.archive.path).is_file():
                payloads = self._reader.read_payloads(Path(plan.archive.path))
            else:
                payloads = PayloadsDocument(payloads=[])
        except (AgentPortError, OSError, ValidationError) as error:
            diagnostics.append(
                _error("invalid-payload-inventory", f"Cannot verify payload inventory: {error}")
            )
            return False, counts
        roles = {payload.member: payload.role for payload in payloads.payloads}
        for operation in plan.operations:
            if operation.member is not None:
                roles.setdefault(operation.member, _infer_payload_role(operation))
        transcript_paths: set[str] = set()
        transcript_ok: dict[str, bool] = {}
        for operation in plan.operations:
            if (
                operation.member is None
                or roles.get(operation.member) is not PayloadRole.TRANSCRIPT
            ):
                continue
            destination = str(Path(operation.destination))
            transcript_paths.add(destination)
            retention = _classify_transcript_retention(run_directory, plan, operation)
            retained = retention == "retained"
            transcript_ok[destination] = retained
            if retained:
                checks.append(f"Restored transcript retained: {Path(destination).name}")
            else:
                diagnostics.append(
                    _error(
                        f"restored-transcript-{retention}",
                        _transcript_retention_message(retention),
                        destination,
                    )
                )

        adapter = self._registry.for_harness(plan.archive.harness)
        try:
            adapter_report = adapter.verify_restore(plan)
        except (AgentPortError, OSError, ValueError) as error:
            adapter_report = VerificationReport(
                valid=False,
                diagnostics=[_error("destination-verification-error", str(error))],
            )
        for diagnostic in adapter_report.diagnostics:
            if (
                diagnostic.code == "restored-checksum-mismatch"
                and diagnostic.path in transcript_paths
                and transcript_ok.get(diagnostic.path, False)
            ):
                continue
            diagnostics.append(diagnostic)
        checks.extend(
            check for check in adapter_report.checks if not check.startswith("Checksum verified:")
        )

        for operation in plan.operations:
            if operation.kind is not OperationKind.SKIP or operation.expected_sha256 is None:
                continue
            if operation.member and roles.get(operation.member) is PayloadRole.TRANSCRIPT:
                continue
            forced = operation.model_copy(update={"kind": OperationKind.COPY})
            report = verify_expected_payloads([forced], plan.archive.harness.value)
            diagnostics.extend(report.diagnostics)
            checks.extend(report.checks)

        for operation in plan.operations:
            if operation.kind is not OperationKind.MERGE_JSONL or operation.member is None:
                continue
            if roles.get(operation.member) is PayloadRole.TRANSCRIPT:
                continue
            if not _merged_jsonl_retained(run_directory, plan, operation):
                diagnostics.append(
                    _error(
                        "restored-index-record-missing",
                        "A record from the restored JSONL index is missing or changed.",
                        operation.destination,
                    )
                )

        for operation in plan.operations:
            if operation.kind is not OperationKind.MERGE_DATABASE or operation.member is None:
                continue
            if not _codex_database_records_retained(run_directory, plan, operation):
                diagnostics.append(
                    _error(
                        "codex-database-record-missing",
                        "A restored Codex database record is missing or changed.",
                        operation.destination,
                    )
                )

        error_paths = {
            diagnostic.path
            for diagnostic in diagnostics
            if diagnostic.severity is Severity.ERROR and diagnostic.path is not None
        }
        main_transcript_paths = {
            operation.destination
            for operation in plan.operations
            if _is_main_transcript(operation, roles, plan)
        }
        transcript_paths = {
            operation.destination
            for operation in plan.operations
            if operation.member and roles.get(operation.member) is PayloadRole.TRANSCRIPT
        }
        subagent_transcript_paths = transcript_paths.difference(main_transcript_paths)
        project_transcript_paths: dict[str, set[str]] = {}
        claude = plan.archive.harness.value == "claude-code"
        for operation in plan.operations:
            if not operation.member or roles.get(operation.member) is not PayloadRole.TRANSCRIPT:
                continue
            if claude:
                # Claude archives count native project containers, not distinct cwd
                # values. A container may contain several working directories, and
                # the same cwd may appear in more than one source container.
                container = _claude_project_container(operation)
                project_sources = {container} if container else set()
            else:
                project_sources = {operation.project_source} if operation.project_source else set()
            if not project_sources and not claude:
                retained_path = run_directory / "staged" / "archive" / operation.member
                if retained_path.is_file():
                    project_sources = inspect_jsonl(
                        retained_path, plan.archive.harness.value
                    ).project_paths
            for project_source in project_sources:
                project_transcript_paths.setdefault(project_source, set()).add(
                    operation.destination
                )
        expected_project_keys = (
            {
                container
                for operation in plan.operations
                if operation.destination in main_transcript_paths
                and (container := _claude_project_container(operation)) is not None
            }
            if claude
            else {project.source for project in plan.projects}
        )
        expected_skills = [
            operation
            for operation in plan.operations
            if operation.member and roles.get(operation.member) is PayloadRole.SKILL
        ]
        expected_attachments = [
            operation
            for operation in plan.operations
            if operation.member and roles.get(operation.member) is PayloadRole.ATTACHMENT
        ]
        conversation_paths: dict[str, set[str]] = {}
        for operation in plan.operations:
            if operation.destination in main_transcript_paths:
                identity = operation.identity or operation.destination
                conversation_paths.setdefault(identity, set()).add(operation.destination)
        if plan.archive.counts is None and counts.expected_transcript_files == 0:
            # Older saved plans do not retain archive counts. Their complete operation
            # inventory remains available even after the source archive is moved.
            counts = counts.model_copy(
                update={
                    "expected_conversations": len(conversation_paths),
                    "expected_transcript_files": len(transcript_paths),
                    "expected_subagent_transcripts": len(subagent_transcript_paths),
                    "expected_projects": len(expected_project_keys),
                }
            )
        verified_conversations = sum(
            all(path not in error_paths and Path(path).is_file() for path in paths)
            for paths in conversation_paths.values()
        )
        counts = counts.model_copy(
            update={
                "verified_conversations": verified_conversations,
                "verified_subagent_transcripts": sum(
                    path not in error_paths and Path(path).is_file()
                    for path in subagent_transcript_paths
                ),
                "verified_transcript_files": sum(
                    path not in error_paths and Path(path).is_file() for path in transcript_paths
                ),
                "verified_projects": (
                    sum(
                        bool(project_transcript_paths.get(project))
                        and all(
                            path not in error_paths and Path(path).is_file()
                            for path in project_transcript_paths[project]
                        )
                        for project in expected_project_keys
                    )
                    if project_transcript_paths
                    else (
                        counts.expected_projects
                        if all(
                            path not in error_paths and Path(path).is_file()
                            for path in transcript_paths
                        )
                        else 0
                    )
                ),
                "expected_skills": len(expected_skills),
                "verified_skills": sum(
                    Path(item.destination).exists() and item.destination not in error_paths
                    for item in expected_skills
                ),
                "expected_attachments": len(expected_attachments),
                "verified_attachments": sum(
                    Path(item.destination).exists() and item.destination not in error_paths
                    for item in expected_attachments
                ),
                "applied_operations": result.applied,
                "skipped_operations": result.skipped,
            }
        )
        current_errors = [item for item in diagnostics if item.severity is Severity.ERROR]
        return not current_errors, counts

    def _doctor(self, plan: RestorePlan, checks: list[str], diagnostics: list[Diagnostic]) -> bool:
        try:
            report = DoctorService(self._registry).execute(
                Path(plan.destination), plan.archive.harness.value
            )
        except AgentPortError as error:
            diagnostics.append(_error("doctor-failed", str(error)))
            return False
        checks.extend(report.checks)
        diagnostics.extend(report.diagnostics)
        return report.valid

    @staticmethod
    def _result(
        run_directory: Path,
        plan_id: str,
        status: RestoreVerificationState,
        restore_completed_safely: bool,
        current_data_intact: bool,
        destination_valid: bool,
        *,
        restart_required: bool = False,
        counts: RestoreVerificationCounts | None = None,
        checks: list[str] | None = None,
        diagnostics: list[Diagnostic] | None = None,
    ) -> RestoreVerificationResult:
        return RestoreVerificationResult(
            plan_id=plan_id,
            run_directory=str(run_directory),
            status=status,
            valid=status is RestoreVerificationState.VERIFIED,
            restore_completed_safely=restore_completed_safely,
            current_data_intact=current_data_intact,
            destination_valid=destination_valid,
            restart_required=restart_required,
            counts=counts or RestoreVerificationCounts(),
            checks=checks or [],
            diagnostics=diagnostics or [],
        )

    @staticmethod
    def _record_result(
        result: RestoreVerificationResult,
        plan: RestorePlan,
        snapshot: Literal["initial", "current"],
    ) -> RestoreVerificationResult:
        run_directory = Path(result.run_directory)
        initial_path = run_directory / "initial-verification.json"
        result_path = (
            initial_path if snapshot == "initial" else run_directory / "current-verification.json"
        )
        initial_status: RestoreVerificationState | None = None
        if snapshot == "initial":
            initial_status = result.status
        elif initial_path.is_file():
            try:
                initial_status = RestoreVerificationResult.model_validate_json(
                    initial_path.read_bytes()
                ).status
            except (OSError, ValidationError):
                initial_status = RestoreVerificationState.FAILED
        else:
            result = result.model_copy(
                update={
                    "diagnostics": [
                        *result.diagnostics,
                        Diagnostic(
                            code="initial-verification-unavailable",
                            severity=Severity.WARNING,
                            message=(
                                "No closed-harness initial verification snapshot is retained for "
                                "this run. Recorded apply checks cannot establish when later "
                                "changes occurred."
                            ),
                        ),
                    ]
                }
            )
        result = result.model_copy(
            update={
                "initial_verification_status": initial_status,
                "initial_verification_path": (
                    str(initial_path) if initial_path.is_file() or snapshot == "initial" else None
                ),
            }
        )
        if snapshot == "initial" and result_path.exists():
            try:
                existing = RestoreVerificationResult.model_validate_json(result_path.read_bytes())
            except (OSError, ValidationError) as error:
                raise RestoreError(f"Initial verification evidence is invalid: {error}") from error
            if existing != result:
                raise RestoreError("Initial verification evidence is immutable and already exists.")
            return existing
        _atomic_json(result_path, result.model_dump(mode="json", exclude_none=True))
        update_migration_for_path(
            Path(plan.archive.path),
            verification_result=str(result_path),
            status=(
                MigrationStateValue.VERIFIED
                if result.status is RestoreVerificationState.VERIFIED
                else MigrationStateValue.FAILED
            ),
            last_error=(None if result.valid else f"Verification status: {result.status.value}"),
        )
        return result


def _classify_transcript_retention(
    run_directory: Path, plan: RestorePlan, operation: PlannedOperation
) -> str:
    if operation.member is None:
        return "malformed-evidence"
    expected = _expected_jsonl(run_directory, plan, operation.member)
    destination = Path(operation.destination)
    if expected is None:
        return "malformed-evidence"
    if not destination.is_file():
        return "missing"
    actual = _stable_read(destination)
    if actual is None:
        return "unstable-read"
    expected_records = _jsonl_values(expected)
    actual_records = _jsonl_values(actual)
    if expected_records is None:
        return "malformed-evidence"
    if actual_records is None:
        return "malformed-current"
    shared = min(len(expected_records), len(actual_records))
    if actual_records[:shared] != expected_records[:shared]:
        return "prefix-changed"
    if len(actual_records) < len(expected_records):
        return "truncated"
    return "retained"


def _transcript_retention_message(retention: str) -> str:
    messages = {
        "missing": "A restored transcript is missing.",
        "unstable-read": "A restored transcript changed repeatedly while it was being checked.",
        "malformed-evidence": (
            "The retained expected transcript evidence is unavailable or malformed."
        ),
        "malformed-current": "A restored transcript is no longer valid JSONL.",
        "truncated": (
            "A restored transcript contains only a truncated prefix of the restored history."
        ),
        "prefix-changed": "A restored transcript changed before any appended records.",
    }
    return messages.get(retention, "A restored transcript no longer matches retained evidence.")


def _merged_jsonl_retained(
    run_directory: Path, plan: RestorePlan, operation: PlannedOperation
) -> bool:
    if operation.member is None:
        return False
    expected = _expected_jsonl(run_directory, plan, operation.member)
    actual = _stable_read(Path(operation.destination))
    expected_records = _jsonl_mapping(expected) if expected is not None else None
    actual_records = _jsonl_mapping(actual) if actual is not None else None
    if expected_records is None or actual_records is None:
        return False
    return all(actual_records.get(key) == value for key, value in expected_records.items())


def _codex_database_records_retained(
    run_directory: Path, plan: RestorePlan, operation: PlannedOperation
) -> bool:
    if operation.member is None:
        return False
    source = run_directory / "staged" / "archive" / Path(*operation.member.split("/"))
    destination = Path(operation.destination)
    if not source.is_file() or not destination.is_file():
        return False
    rollout_paths = _rollout_destinations(source, plan.operations)
    mapper = PathMapper(plan.mappings)
    try:
        with (
            closing(
                sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
            ) as source_db,
            closing(
                sqlite3.connect(f"file:{destination.resolve().as_posix()}?mode=ro", uri=True)
            ) as destination_db,
        ):
            for table in ("threads", "thread_dynamic_tools", "thread_spawn_edges"):
                source_columns = _database_columns(source_db, table)
                if not source_columns:
                    continue
                destination_columns = _database_columns(destination_db, table)
                common = [column for column in source_columns if column in destination_columns]
                primary = [column for column, details in source_columns.items() if details]
                if not common or not primary:
                    return False
                quoted = ",".join(f'"{column}"' for column in common)
                where = " AND ".join(f'"{column}"=?' for column in primary)
                for row in source_db.execute(f'SELECT {quoted} FROM "{table}"'):
                    values = dict(zip(common, row, strict=True))
                    if table == "threads":
                        identity = values.get("id")
                        if isinstance(values.get("cwd"), str):
                            values["cwd"] = mapper.apply(values["cwd"]).value
                        if isinstance(identity, str) and identity in rollout_paths:
                            values["rollout_path"] = rollout_paths[identity]
                    key_values = [values[column] for column in primary]
                    actual = destination_db.execute(
                        f'SELECT {quoted} FROM "{table}" WHERE {where}', key_values
                    ).fetchone()
                    if actual is None or tuple(values[column] for column in common) != tuple(
                        actual
                    ):
                        return False
    except (OSError, sqlite3.DatabaseError, ValueError):
        return False
    return True


def _database_columns(connection: sqlite3.Connection, table: str) -> dict[str, bool]:
    try:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {str(row[1]): bool(row[5]) for row in rows}


def _expected_jsonl(run_directory: Path, plan: RestorePlan, member: str) -> bytes | None:
    transformed = run_directory / "staged" / "transformed" / Path(*member.split("/"))
    if transformed.is_file():
        try:
            return transformed.read_bytes()
        except OSError:
            return None
    archived = run_directory / "staged" / "archive" / Path(*member.split("/"))
    if not archived.is_file():
        return None
    try:
        return transform_jsonl(archived, plan.archive.harness.value, PathMapper(plan.mappings))
    except AgentPortError:
        return None


def _stable_read(path: Path) -> bytes | None:
    if not path.is_file():
        return None
    for _attempt in range(3):
        try:
            before = path.stat()
            value = path.read_bytes()
            after = path.stat()
        except OSError:
            return None
        if (before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return value
    return None


def _jsonl_values(data: bytes) -> list[dict[str, object]] | None:
    try:
        values = [json.loads(line) for line in data.decode("utf-8").splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return values if all(isinstance(value, dict) for value in values) else None


def _jsonl_mapping(data: bytes) -> dict[str, dict[str, object]] | None:
    values = _jsonl_values(data)
    if values is None:
        return None
    records: dict[str, dict[str, object]] = {}
    for value in values:
        key = jsonl_identity(value)
        if key in records and records[key] != value:
            return None
        records[key] = value
    return records


def _is_main_transcript(
    operation: PlannedOperation,
    roles: dict[str, PayloadRole],
    plan: RestorePlan,
) -> bool:
    if operation.member is None or roles.get(operation.member) is not PayloadRole.TRANSCRIPT:
        return False
    if plan.archive.harness.value == "codex":
        return operation.identity is not None
    if operation.source is None:
        return False
    parts = Path(operation.source).parts
    return len(parts) == 3 and parts[0] == "projects"


def _claude_project_container(operation: PlannedOperation) -> str | None:
    source = operation.source or (operation.member or "").removeprefix("native/sessions/")
    parts = source.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == "projects" else None


def _infer_payload_role(operation: PlannedOperation) -> PayloadRole:
    member = operation.member or ""
    if member.startswith("native/skills/"):
        return PayloadRole.SKILL
    if operation.kind is OperationKind.MERGE_DATABASE or member.startswith("native/state/"):
        return PayloadRole.DATABASE
    if operation.kind is OperationKind.MERGE_JSONL or member.endswith(
        ("history.jsonl", "session_index.jsonl")
    ):
        return PayloadRole.INDEX
    if member.endswith(".jsonl"):
        return PayloadRole.TRANSCRIPT
    return PayloadRole.ATTACHMENT


def _error(code: str, message: str, path: str | None = None) -> Diagnostic:
    return Diagnostic(code=code, severity=Severity.ERROR, message=message, path=path)
