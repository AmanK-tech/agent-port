from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from agent_port import __version__
from agent_port.application.backup import BackupService
from agent_port.application.migration import update_migration_for_path
from agent_port.application.registry import AdapterRegistry
from agent_port.domain.errors import RestoreError
from agent_port.domain.models import (
    ArchivePrecondition,
    CountSummary,
    DestinationPrecondition,
    HarnessName,
    JournalEntry,
    MigrationStateValue,
    OperationKind,
    PathMapping,
    PayloadRecord,
    PayloadRole,
    PayloadScope,
    PlannedOperation,
    ProjectMapping,
    ProjectRecord,
    RestoreConflict,
    RestorePlan,
    RestorePlanInfo,
    RestoreResult,
    RollbackJournal,
    RollbackResult,
    Severity,
    SkillConflictPolicy,
    SkillOwner,
    SkillRecord,
    SkillScope,
    VerificationReport,
)
from agent_port.domain.ports import RestorePlanningContext
from agent_port.infrastructure.archive import AgentPackReader
from agent_port.infrastructure.archive.common import canonical_json, describe_path, stage_entries
from agent_port.infrastructure.filesystem.jsonl import inspect_jsonl
from agent_port.infrastructure.filesystem.locking import restore_destination_lock
from agent_port.infrastructure.path_mapping import (
    PathMapper,
    fingerprint_path,
    is_absolute,
    normalize,
    parse_mapping,
)
from agent_port.infrastructure.repositories import discover_repositories


def _is_claude_transcript_member(relative_name: str) -> bool:
    parts = tuple(part for part in relative_name.split("/") if part)
    return (len(parts) == 4 and parts[:2] == ("sessions", "projects")) or "subagents" in parts[3:-1]


class RestorePlanService:
    def __init__(self, registry: AdapterRegistry | None = None) -> None:
        self._registry = registry or AdapterRegistry()
        self._reader = AgentPackReader()

    def execute(
        self,
        archive: Path,
        output: Path,
        destination: Path | None = None,
        destination_home: Path | None = None,
        mapping_values: list[str] | None = None,
        accepted_unmapped: list[str] | None = None,
        skill_conflicts: list[str] | None = None,
    ) -> RestorePlan:
        archive = archive.expanduser().resolve()
        output = output.expanduser().resolve()
        if output.exists():
            raise RestoreError(f"Refusing to overwrite existing restore plan: {output}")
        report = self._reader.inspect(archive)
        manifest = report.manifest
        destination = self._resolve_destination(manifest.harness, destination)
        if not destination.is_dir():
            raise RestoreError(
                f"Restore destination is not an initialized directory: {destination}"
            )
        self._registry.select(destination, manifest.harness.value)
        destination_home = (
            destination_home.expanduser().resolve() if destination_home else Path.home().resolve()
        )
        mappings = [
            parse_mapping(value, manifest.source_home, destination_home)
            for value in (mapping_values or [])
        ]
        mapper = PathMapper(mappings)
        accepted = {
            normalize(self._expand_source_path(value, manifest.source_home))
            for value in (accepted_unmapped or [])
        }
        projects_raw = self._reader.read_inventory(archive, "projects.json").get("projects", [])
        if not isinstance(projects_raw, list):
            raise RestoreError("Invalid projects inventory in archive.")
        repository_fingerprints = self._reader.read_repository_inventory(archive)
        projects_raw = [
            (
                {**value, "repository_fingerprint": repository_fingerprints[value["path"]]}
                if isinstance(value, dict)
                and isinstance(value.get("path"), str)
                and value["path"] in repository_fingerprints
                else value
            )
            for value in projects_raw
        ]
        projects, path_conflicts = self._plan_projects(projects_raw, mapper, accepted)
        policies = self._parse_skill_policies(skill_conflicts or [])
        skills_inventory = self._reader.read_inventory(archive, "skills.json")
        skills = self._included_skills(skills_inventory)

        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="agent-port-plan-", dir=output.parent) as temporary:
            extracted = Path(temporary) / "extracted"
            self._reader.materialize(archive, extracted)
            payloads = (
                self._reader.read_payloads(archive).payloads
                if manifest.format_version == 2
                else self._infer_v1_payloads(extracted, manifest.harness)
            )
            timestamp_document = self._reader.read_timestamps(archive)
            source_mtimes = timestamp_document.entries if timestamp_document is not None else {}
            context = RestorePlanningContext(
                archive=archive,
                extracted=extracted,
                manifest=manifest,
                payloads=payloads,
                destination=destination,
                destination_home=destination_home,
                projects=projects,
                skills=skills,
                skill_policies=policies,
                source_mtimes=source_mtimes,
            )
            adapter_plan = self._registry.for_harness(manifest.harness).plan_restore(context)
            payload_by_member = {payload.member: payload for payload in payloads}
            enriched_operations = [
                operation.model_copy(
                    update={
                        "source_mtime_ns": source_mtimes.get(operation.member or ""),
                        "project_source": (
                            payload_by_member[operation.member].project_path
                            if operation.member in payload_by_member
                            and payload_by_member[operation.member].role is PayloadRole.TRANSCRIPT
                            else None
                        ),
                    }
                )
                for operation in adapter_plan.operations
            ]

        plan_id = uuid.uuid4().hex
        run_directory = destination.parent / ".agent-port-runs" / plan_id
        preconditions = self._destination_preconditions(enriched_operations, projects)
        plan = RestorePlan(
            plan_id=plan_id,
            created_at=datetime.now(UTC),
            archive=ArchivePrecondition(
                path=str(archive),
                sha256=_sha256_file(archive),
                format_version=manifest.format_version,
                harness=manifest.harness,
                counts=manifest.counts,
            ),
            adapter_version=__version__,
            destination=str(destination),
            destination_home=str(destination_home),
            mappings=mapper.mappings,
            accepted_unmapped=sorted(accepted),
            projects=projects,
            operations=enriched_operations,
            conflicts=[*path_conflicts, *adapter_plan.conflicts],
            destination_preconditions=preconditions,
            diagnostics=[
                *[
                    item
                    for item in report.diagnostics
                    if item.code.startswith("source-timestamps-")
                ],
                *adapter_plan.diagnostics,
            ],
            run_directory=str(run_directory),
        )
        plan.plan_digest = _plan_digest(plan)
        _atomic_json(output, plan.model_dump(mode="json", exclude_none=True))
        update_migration_for_path(
            archive,
            current_plan=str(output),
            run_directory=plan.run_directory,
            status=MigrationStateValue.PLANNED,
            content=manifest.counts,
            last_error=None,
        )
        return plan

    @staticmethod
    def _resolve_destination(harness: HarnessName, value: Path | None) -> Path:
        if value is not None:
            return value.expanduser().resolve()
        variable = "CODEX_HOME" if harness is HarnessName.CODEX else "CLAUDE_CONFIG_DIR"
        configured = os.environ.get(variable)
        if configured:
            return Path(configured).expanduser().resolve()
        name = ".codex" if harness is HarnessName.CODEX else ".claude"
        return (Path.home() / name).resolve()

    @staticmethod
    def _expand_source_path(value: str, source_home: str | None) -> str:
        if value == "~" or value.startswith(("~/", "~\\")):
            if source_home is None:
                raise RestoreError("Cannot expand accepted ~ path without archive source_home.")
            return source_home if value == "~" else source_home.rstrip("/\\") + "/" + value[2:]
        return value

    @staticmethod
    def _plan_projects(
        values: list[object], mapper: PathMapper, accepted: set[str]
    ) -> tuple[list[ProjectMapping], list[RestoreConflict]]:
        projects: list[ProjectMapping] = []
        conflicts: list[RestoreConflict] = []
        for value in values:
            try:
                project = ProjectRecord.model_validate(value)
            except ValidationError as error:
                raise RestoreError(f"Invalid archived project record: {error}") from error
            source = project.path
            if source.startswith("encoded:") or not is_absolute(source):
                conflicts.append(
                    RestoreConflict(
                        kind="opaque-project-path",
                        identity=source,
                        destination=source,
                        message="Archived project path cannot be mapped structurally.",
                    )
                )
                projects.append(
                    ProjectMapping(
                        source=source,
                        destination=source,
                        exists=False,
                        conversation_count=project.conversation_count,
                        repository_fingerprint=project.repository_fingerprint,
                    )
                )
                continue
            mapped = mapper.apply(source)
            accepted_path = normalize(source) in accepted
            identity_exists = Path(source).is_dir()
            if not mapped.mapped and not identity_exists and not accepted_path:
                conflicts.append(
                    RestoreConflict(
                        kind="unmapped-project",
                        identity=source,
                        destination=source,
                        message="Project path needs --map or exact --accept-unmapped approval.",
                    )
                )
            destination = mapped.value
            projects.append(
                ProjectMapping(
                    source=source,
                    destination=destination,
                    exists=Path(destination).is_dir(),
                    conversation_count=project.conversation_count,
                    accepted_unmapped=accepted_path,
                    repository_fingerprint=project.repository_fingerprint,
                )
            )
        return projects, conflicts

    @staticmethod
    def _parse_skill_policies(values: list[str]) -> dict[str, SkillConflictPolicy]:
        policies: dict[str, SkillConflictPolicy] = {}
        for value in values:
            if "=" not in value:
                raise RestoreError(
                    f"Invalid skill conflict policy {value!r}; expected NAME=POLICY."
                )
            name, raw_policy = value.split("=", 1)
            try:
                policy = SkillConflictPolicy(raw_policy.strip())
            except ValueError as error:
                raise RestoreError(f"Unknown skill conflict policy: {raw_policy}") from error
            name = name.strip()
            if not name:
                raise RestoreError("Skill conflict policy requires a skill name.")
            existing = policies.get(name)
            if existing is not None and existing is not policy:
                raise RestoreError(f"Conflicting policies supplied for skill: {name}")
            policies[name] = policy
        return policies

    @staticmethod
    def _included_skills(inventory: dict[str, object]) -> list[SkillRecord]:
        raw_skills = inventory.get("skills", [])
        included = inventory.get("included", [])
        if not isinstance(raw_skills, list) or not isinstance(included, list):
            raise RestoreError("Invalid skills inventory in archive.")
        included_paths = {value for value in included if isinstance(value, str)}
        skills: list[SkillRecord] = []
        for value in raw_skills:
            try:
                skill = SkillRecord.model_validate(value)
            except ValidationError as error:
                raise RestoreError(f"Invalid archived skill record: {error}") from error
            if skill.source_path in included_paths:
                if (
                    skill.owner is not SkillOwner.USER
                    or skill.scope is not SkillScope.USER
                    or not skill.eligible
                ):
                    raise RestoreError(
                        f"Archive attempts to restore an ineligible managed skill: {skill.name}"
                    )
                skills.append(skill)
        found = {skill.source_path for skill in skills}
        if found != included_paths:
            raise RestoreError("Included skill inventory references unknown skill records.")
        return skills

    @staticmethod
    def _infer_v1_payloads(extracted: Path, harness: HarnessName) -> list[PayloadRecord]:
        native = extracted / "native"
        databases = list((native / "state").glob("*")) if (native / "state").is_dir() else []
        if len([path for path in databases if path.is_file()]) > 1:
            raise RestoreError(
                "Format v1 archive has ambiguous Codex state; create a format v2 backup."
            )
        payloads: list[PayloadRecord] = []
        project_by_container: dict[str, str] = {}
        if harness is HarnessName.CLAUDE_CODE:
            projects_root = native / "sessions" / "projects"
            if projects_root.is_dir():
                for transcript in projects_root.rglob("*.jsonl"):
                    summary = inspect_jsonl(transcript, harness.value)
                    transcript_relative = transcript.relative_to(projects_root)
                    if transcript_relative.parts and len(summary.project_paths) == 1:
                        project_by_container[transcript_relative.parts[0]] = next(
                            iter(summary.project_paths)
                        )
        for path in stage_entries(native):
            member = path.relative_to(extracted).as_posix()
            relative_name = path.relative_to(native).as_posix()
            description = describe_path(path)
            if relative_name.startswith("state/"):
                role, strategy, original = (
                    PayloadRole.DATABASE,
                    "sqlite-merge",
                    relative_name[6:],
                )
            elif relative_name.startswith("skills/"):
                role, strategy, original = (
                    PayloadRole.SKILL,
                    "skill-directory",
                    relative_name[7:],
                )
            elif relative_name.endswith(("history.jsonl", "session_index.jsonl")):
                role, strategy = PayloadRole.INDEX, "jsonl-merge"
                original = relative_name.removeprefix("sessions/")
            elif relative_name.endswith(".jsonl") and (
                harness is not HarnessName.CLAUDE_CODE
                or _is_claude_transcript_member(relative_name)
            ):
                role, strategy = PayloadRole.TRANSCRIPT, "native-session"
                original = relative_name.removeprefix("sessions/")
            else:
                role, strategy = PayloadRole.ATTACHMENT, "copy-if-absent"
                original = relative_name.removeprefix("sessions/")
            project_path = None
            if role is PayloadRole.TRANSCRIPT:
                summary = inspect_jsonl(path, harness.value)
                if len(summary.project_paths) == 1:
                    project_path = next(iter(summary.project_paths))
            if harness is HarnessName.CLAUDE_CODE and original.startswith("projects/"):
                parts = original.split("/")
                if len(parts) > 1:
                    project_path = project_by_container.get(parts[1], project_path)
            payloads.append(
                PayloadRecord(
                    member=member,
                    role=role,
                    original_path=original,
                    destination_scope=(
                        PayloadScope.USER_HOME
                        if role is PayloadRole.SKILL and harness is HarnessName.CODEX
                        else PayloadScope.HARNESS
                    ),
                    strategy=strategy,
                    project_path=project_path,
                    sha256=description.sha256,
                    mode=description.mode,
                )
            )
        return payloads

    @staticmethod
    def _destination_preconditions(
        operations: list[PlannedOperation], projects: list[ProjectMapping]
    ) -> list[DestinationPrecondition]:
        paths = sorted({operation.destination for operation in operations})
        result = [
            DestinationPrecondition(path=value, fingerprint=fingerprint_path(Path(value)))
            for value in paths
        ]
        for project in projects:
            marker = "exists:directory" if Path(project.destination).is_dir() else "exists:missing"
            result.append(DestinationPrecondition(path=project.destination, fingerprint=marker))
        by_path: dict[str, DestinationPrecondition] = {}
        for item in result:
            by_path.setdefault(item.path, item)
        return list(by_path.values())


def _suggest_repository_mappings(
    plan: RestorePlan, existing: list[PathMapping]
) -> list[PathMapping]:
    """Suggest unique destination clones for otherwise-unmapped project paths."""
    if not any(
        project.repository_fingerprint
        and not project.exists
        and not project.accepted_unmapped
        and project.destination == project.source
        for project in plan.projects
    ):
        return []
    candidates = discover_repositories(Path(plan.destination_home))
    if not candidates:
        return []
    by_fingerprint: dict[str, list[Path]] = {}
    for candidate in candidates:
        by_fingerprint.setdefault(candidate.fingerprint, []).append(candidate.path)
    existing_sources = {normalize(mapping.source) for mapping in existing}
    suggestions: list[PathMapping] = []
    for project in plan.projects:
        if project.exists or project.accepted_unmapped or project.destination != project.source:
            continue
        if not project.repository_fingerprint:
            continue
        matches = by_fingerprint.get(project.repository_fingerprint, [])
        if len(matches) != 1 or normalize(project.source) in existing_sources:
            continue
        mapping = PathMapping(source=project.source, destination=str(matches[0]))
        if mapping not in suggestions:
            suggestions.append(mapping)
    return suggestions


class RestorePlanInfoService:
    def __init__(self, reader: AgentPackReader | None = None) -> None:
        self._reader = reader or AgentPackReader()

    def execute(self, plan_path: Path) -> RestorePlanInfo:
        resolved = plan_path.expanduser().resolve()
        plan = _load_plan(resolved)
        if plan.plan_digest != _plan_digest(plan):
            raise RestoreError("Restore plan digest is invalid; generate a new plan.")
        content = CountSummary(
            conversations=sum(project.conversation_count for project in plan.projects),
            projects=len(plan.projects),
            skills=sum(
                operation.member is not None and operation.member.startswith("native/skills/")
                for operation in plan.operations
            ),
        )
        suggested: list[PathMapping] = []
        archive = Path(plan.archive.path)
        if archive.is_file() and _sha256_file(archive) == plan.archive.sha256:
            report = self._reader.inspect(archive)
            content = report.manifest.counts
            source_home = report.manifest.source_home
            if (
                source_home
                and normalize(source_home) != normalize(plan.destination_home)
                and not any(
                    normalize(mapping.source) == normalize(source_home) for mapping in plan.mappings
                )
            ):
                suggested.append(PathMapping(source=source_home, destination=plan.destination_home))
        suggested.extend(_suggest_repository_mappings(plan, suggested))
        operation_counts = dict(
            sorted(Counter(operation.kind.value for operation in plan.operations).items())
        )
        blockers = [conflict for conflict in plan.conflicts if conflict.blocking]
        notices = [conflict for conflict in plan.conflicts if not conflict.blocking]
        skill_policies: dict[str, SkillConflictPolicy] = {}
        differing_skill_names = {
            conflict.identity
            for conflict in plan.conflicts
            if conflict.kind.startswith("skill-collision")
        }
        for operation in plan.operations:
            if (
                operation.identity
                and operation.member
                and operation.member.startswith("native/skills/")
                and (
                    operation.identity in differing_skill_names
                    or operation.kind is OperationKind.REPLACE
                )
            ):
                skill_policies[operation.identity] = operation.policy or SkillConflictPolicy.ERROR
        for name in differing_skill_names:
            skill_policies.setdefault(name, SkillConflictPolicy.ERROR)
        return RestorePlanInfo(
            status="ready" if plan.ready else "blocked",
            plan_id=plan.plan_id,
            plan_path=str(resolved),
            run_directory=plan.run_directory,
            archive=plan.archive.path,
            destination=plan.destination,
            ready=plan.ready,
            content=content,
            mappings=plan.mappings,
            suggested_mappings=suggested,
            operation_counts=operation_counts,
            blockers=blockers,
            notices=notices,
            skill_policies=dict(sorted(skill_policies.items())),
            diagnostics=plan.diagnostics,
        )


class RestorePreflightService:
    def __init__(self, reader: AgentPackReader | None = None) -> None:
        self._reader = reader or AgentPackReader()

    def execute(self, plan_path: Path) -> RestorePlan:
        plan = _load_plan(plan_path)
        if not plan.ready:
            raise RestoreError("Restore plan contains blocking conflicts or diagnostics.")
        if plan.plan_digest != _plan_digest(plan):
            raise RestoreError("Restore plan digest is invalid; generate a new plan.")
        if plan.adapter_version != __version__:
            raise RestoreError("Agent Port version changed after planning; generate a new plan.")
        archive = Path(plan.archive.path)
        if _sha256_file(archive) != plan.archive.sha256:
            raise RestoreError("Archive changed after planning; generate a new plan.")
        report = self._reader.inspect(archive)
        if (
            report.manifest.harness is not plan.archive.harness
            or report.manifest.format_version != plan.archive.format_version
        ):
            raise RestoreError("Archive identity no longer matches the restore plan.")
        _check_preconditions(plan.destination_preconditions)
        run_directory = Path(plan.run_directory)
        if run_directory.exists():
            raise RestoreError(f"Restore run directory already exists: {run_directory}")
        return plan


class RestoreApplyService:
    def __init__(self, registry: AdapterRegistry | None = None) -> None:
        self._registry = registry or AdapterRegistry()
        self._reader = AgentPackReader()

    def execute(
        self,
        plan_path: Path,
        confirm_harness_closed: bool,
        register_projects: bool = False,
        closure_guard: Callable[[], None] | None = None,
    ) -> RestoreResult:
        if not confirm_harness_closed:
            raise RestoreError("Apply requires --confirm-harness-closed.")
        destination = _load_plan(plan_path).destination
        with restore_destination_lock(Path(destination)):
            return self._execute(plan_path, destination, register_projects, closure_guard)

    def _execute(
        self,
        plan_path: Path,
        locked_destination: str,
        register_projects: bool,
        closure_guard: Callable[[], None] | None,
    ) -> RestoreResult:
        plan = RestorePreflightService(self._reader).execute(plan_path)
        if plan.destination != locked_destination:
            raise RestoreError("Restore destination changed while acquiring its lock.")
        archive = Path(plan.archive.path)
        if closure_guard is not None:
            closure_guard()
        run_directory = Path(plan.run_directory)
        run_directory.mkdir(parents=True)
        _atomic_json(run_directory / "plan.json", plan.model_dump(mode="json", exclude_none=True))
        if plan.archive.format_version == 2:
            payloads = self._reader.read_payloads(archive)
            _atomic_json(
                run_directory / "payloads.json",
                payloads.model_dump(mode="json", exclude_none=True),
            )
        adapter = self._registry.for_harness(plan.archive.harness)
        self._registry.select(Path(plan.destination), plan.archive.harness.value)
        journal = RollbackJournal(plan_id=plan.plan_id, destination=plan.destination)
        journal_path = run_directory / "journal.json"
        _atomic_json(journal_path, journal.model_dump(mode="json"))
        backup = run_directory / "destination-before.agentpack"
        try:
            BackupService(registry=self._registry).execute(
                Path(plan.destination),
                backup,
                requested_harness=plan.archive.harness.value,
                user_home=Path(plan.destination_home),
            )
            if closure_guard is not None:
                closure_guard()
            extracted = run_directory / "staged" / "archive"
            self._reader.materialize(archive, extracted)
            transformed = run_directory / "staged" / "transformed"
            transformed.mkdir(parents=True)
            adapter.stage_restore(plan, extracted, transformed)
            _restore_staged_mtimes(plan, transformed)
            if closure_guard is not None:
                closure_guard()
            _check_preconditions(plan.destination_preconditions)
            self._prepare_journal(plan, run_directory, journal, journal_path)
            self._apply_filesystem_operations(plan, transformed, closure_guard)
            if closure_guard is not None:
                closure_guard()
            adapter.apply_restore(plan, transformed)
            if closure_guard is not None:
                closure_guard()
            verification = adapter.verify_restore(plan)
            if not verification.valid:
                raise RestoreError("Post-restore verification failed; automatic rollback started.")
            for entry in journal.entries:
                entry.post_fingerprint = fingerprint_path(Path(entry.destination))
            journal.completed = True
            _atomic_json(journal_path, journal.model_dump(mode="json", exclude_none=True))
            warnings: list[str] = []
            if register_projects:
                try:
                    warnings = adapter.register_projects(plan.projects)
                except Exception as registration_error:
                    warnings = [f"Project registration failed after restore: {registration_error}"]
            result = RestoreResult(
                plan_id=plan.plan_id,
                run_directory=str(run_directory),
                backup=str(backup),
                applied=sum(
                    operation.kind is not OperationKind.SKIP for operation in plan.operations
                ),
                skipped=sum(operation.kind is OperationKind.SKIP for operation in plan.operations),
                restart_required=any(
                    operation.member is not None
                    and operation.member.startswith("native/skills/")
                    and operation.kind is not OperationKind.SKIP
                    for operation in plan.operations
                ),
                verification=verification,
                registration_warnings=warnings,
            )
            _atomic_json(
                run_directory / "verification.json",
                verification.model_dump(mode="json", exclude_none=True),
            )
            _atomic_json(
                run_directory / "result.json", result.model_dump(mode="json", exclude_none=True)
            )
            from agent_port.application.verification import RestoreVerificationService

            initial_verification = RestoreVerificationService(
                registry=self._registry,
                reader=self._reader,
            ).execute(run_directory, snapshot="initial")
            if closure_guard is not None:
                closure_guard()
            if not initial_verification.success_gate_passed:
                details = "; ".join(
                    diagnostic.message
                    for diagnostic in initial_verification.diagnostics
                    if diagnostic.severity is Severity.ERROR
                )
                raise RestoreError(
                    "Initial closed-harness verification failed; automatic rollback started."
                    + (f" {details}" if details else "")
                )
            if closure_guard is not None:
                closure_guard()
            return result
        except Exception as error:
            try:
                RollbackService()._execute(run_directory, enforce_postconditions=False)
            except Exception as rollback_error:
                _atomic_json(
                    run_directory / "failure.json",
                    {
                        "error": str(error),
                        "rollback_error": str(rollback_error),
                        "rolled_back": False,
                    },
                )
                raise RestoreError(
                    f"Restore failed ({error}); automatic rollback also failed ({rollback_error}). "
                    f"Preserved recovery data at {run_directory}."
                ) from error
            _atomic_json(
                run_directory / "failure.json",
                {"error": str(error), "rolled_back": True},
            )
            if isinstance(error, RestoreError):
                raise
            raise RestoreError(f"Restore failed and was rolled back: {error}") from error

    @staticmethod
    def _prepare_journal(
        plan: RestorePlan,
        run_directory: Path,
        journal: RollbackJournal,
        journal_path: Path,
    ) -> None:
        seen: set[str] = set()
        rollback_root = run_directory / "rollback"
        for operation in plan.operations:
            if operation.kind is OperationKind.SKIP or operation.destination in seen:
                continue
            seen.add(operation.destination)
            destination = Path(operation.destination)
            backup_path = rollback_root / f"{len(journal.entries):06d}"
            created = not destination.exists() and not destination.is_symlink()
            backup_value: str | None = None
            if not created:
                _copy_path(destination, backup_path)
                backup_value = str(backup_path)
            journal.entries.append(
                JournalEntry(
                    destination=str(destination),
                    backup=backup_value,
                    pre_fingerprint=fingerprint_path(destination),
                    created=created,
                )
            )
            _atomic_json(journal_path, journal.model_dump(mode="json", exclude_none=True))

    @staticmethod
    def _apply_filesystem_operations(
        plan: RestorePlan,
        staging: Path,
        closure_guard: Callable[[], None] | None = None,
    ) -> None:
        for operation in plan.operations:
            if operation.kind not in {OperationKind.COPY, OperationKind.REPLACE}:
                continue
            if closure_guard is not None:
                closure_guard()
            if operation.member is None:
                raise RestoreError("Filesystem operation has no archive member.")
            source = staging / Path(*operation.member.split("/"))
            destination = Path(operation.destination)
            if operation.kind is OperationKind.COPY and (
                destination.exists() or destination.is_symlink()
            ):
                raise RestoreError(f"Destination appeared after planning: {destination}")
            _atomic_install(source, destination, replace=operation.kind is OperationKind.REPLACE)


class RollbackService:
    def execute(self, run_directory: Path, confirm_harness_closed: bool) -> RollbackResult:
        if not confirm_harness_closed:
            raise RestoreError("Rollback requires --confirm-harness-closed.")
        run_directory = run_directory.expanduser().resolve()
        journal = self._load_journal(run_directory)
        with restore_destination_lock(Path(journal.destination)):
            return self._execute(
                run_directory, enforce_postconditions=True, locked_destination=journal.destination
            )

    @staticmethod
    def _load_journal(run_directory: Path) -> RollbackJournal:
        journal_path = run_directory / "journal.json"
        if not journal_path.is_file():
            raise RestoreError(f"Rollback journal does not exist: {journal_path}")
        try:
            return RollbackJournal.model_validate_json(journal_path.read_bytes())
        except (OSError, ValidationError) as error:
            raise RestoreError(f"Invalid rollback journal: {error}") from error

    def _execute(
        self,
        run_directory: Path,
        enforce_postconditions: bool,
        locked_destination: str | None = None,
    ) -> RollbackResult:
        # Automatic rollback runs inside the apply service's destination lock.
        journal_path = run_directory / "journal.json"
        journal = self._load_journal(run_directory)
        if locked_destination is not None and journal.destination != locked_destination:
            raise RestoreError("Rollback destination changed while acquiring its lock.")
        if journal.rolled_back:
            return RollbackResult(
                run_directory=str(run_directory), restored=0, already_rolled_back=True
            )
        if enforce_postconditions and not journal.completed:
            raise RestoreError("Cannot manually roll back an incomplete restore run.")
        if enforce_postconditions:
            for entry in journal.entries:
                if entry.post_fingerprint is None:
                    raise RestoreError("Rollback journal lacks post-restore fingerprints.")
                if fingerprint_path(Path(entry.destination)) != entry.post_fingerprint:
                    raise RestoreError(
                        f"Destination changed after restore; refusing rollback: {entry.destination}"
                    )
        restored = 0
        for entry in reversed(journal.entries):
            destination = Path(entry.destination)
            _remove_path(destination)
            if entry.backup:
                _copy_path(Path(entry.backup), destination)
            if fingerprint_path(destination) != entry.pre_fingerprint:
                raise RestoreError(
                    f"Rollback could not reproduce the original destination: {destination}"
                )
            restored += 1
        journal.rolled_back = True
        _atomic_json(journal_path, journal.model_dump(mode="json", exclude_none=True))
        return RollbackResult(run_directory=str(run_directory), restored=restored)


class DoctorService:
    def __init__(self, registry: AdapterRegistry | None = None) -> None:
        self._registry = registry or AdapterRegistry()

    def execute(self, destination: Path, requested_harness: str = "auto") -> VerificationReport:
        destination = destination.expanduser().resolve()
        adapter = self._registry.select(destination, requested_harness)
        report = adapter.inspect(destination, Path.home())
        errors = [item for item in report.diagnostics if item.severity is Severity.ERROR]
        checks = [
            f"Detected {report.harness.value}",
            f"Validated {report.counts.transcript_files} transcript files",
            f"Inspected {report.counts.skills} skills",
        ]
        return VerificationReport(valid=not errors, checks=checks, diagnostics=report.diagnostics)


def _load_plan(path: Path) -> RestorePlan:
    try:
        return RestorePlan.model_validate_json(path.expanduser().read_bytes())
    except (OSError, ValidationError) as error:
        raise RestoreError(f"Cannot load restore plan {path}: {error}") from error


def _plan_digest(plan: RestorePlan) -> str:
    value = plan.model_dump(mode="json", exclude_none=True)
    value["plan_digest"] = ""
    return f"sha256:{hashlib.sha256(canonical_json(value)).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise RestoreError(f"Cannot hash {path}: {error}") from error
    return f"sha256:{digest.hexdigest()}"


def _check_preconditions(preconditions: list[DestinationPrecondition]) -> None:
    for precondition in preconditions:
        path = Path(precondition.path)
        if precondition.fingerprint in {"exists:directory", "exists:missing"}:
            actual = "exists:directory" if path.is_dir() else "exists:missing"
        else:
            actual = fingerprint_path(path)
        if actual != precondition.fingerprint:
            raise RestoreError(f"Destination changed after planning; generate a new plan: {path}")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = canonical_json(value)
    with temporary.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        os.symlink(os.readlink(source), destination)
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _atomic_install(source: Path, destination: Path, replace: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.agent-port"
    _copy_path(source, temporary)
    try:
        if replace:
            _remove_path(destination)
        os.replace(temporary, destination)
    except Exception:
        _remove_path(temporary)
        raise


def _restore_staged_mtimes(plan: RestorePlan, staging: Path) -> None:
    seen: set[str] = set()
    for operation in plan.operations:
        if (
            operation.member is None
            or operation.source_mtime_ns is None
            or operation.member in seen
        ):
            continue
        seen.add(operation.member)
        staged = staging / Path(*operation.member.split("/"))
        if staged.is_file() and not staged.is_symlink():
            os.utime(staged, ns=(operation.source_mtime_ns, operation.source_mtime_ns))
