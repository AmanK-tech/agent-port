from __future__ import annotations

import tomllib
from pathlib import Path

from agent_port.adapters.codex.database import inspect_database, snapshot_database
from agent_port.domain.errors import BackupError, InspectionError
from agent_port.domain.models import (
    CapabilitySet,
    CountSummary,
    DetectionResult,
    DetectionStatus,
    Diagnostic,
    ExcludedPathRecord,
    HarnessName,
    InspectionReport,
    ProjectMapping,
    ProjectRecord,
    RestorePlan,
    Severity,
    SkillOwner,
    SkillRecord,
    SkillScope,
    VerificationReport,
)
from agent_port.domain.ports import AdapterRestorePlan, BackupCollection, RestorePlanningContext
from agent_port.infrastructure.filesystem.copying import copy_tree_safely, stable_copy
from agent_port.infrastructure.filesystem.jsonl import inspect_jsonl
from agent_port.infrastructure.filesystem.skills import copy_skill, inspect_skill, scan_skill_root
from agent_port.infrastructure.repositories import repository_fingerprint
from agent_port.infrastructure.versions import compatibility_profiles


class CodexAdapter:
    name = HarnessName.CODEX

    def plan_restore(self, context: RestorePlanningContext) -> AdapterRestorePlan:
        from agent_port.adapters.codex.restore import plan_restore

        return plan_restore(context)

    def stage_restore(self, plan: RestorePlan, extracted: Path, staging: Path) -> None:
        from agent_port.adapters.codex.restore import stage_restore

        stage_restore(plan, extracted, staging)

    def apply_restore(self, plan: RestorePlan, staging: Path) -> None:
        from agent_port.adapters.codex.restore import apply_restore

        apply_restore(plan, staging)

    def verify_restore(self, plan: RestorePlan) -> VerificationReport:
        from agent_port.adapters.codex.restore import verify_restore

        return verify_restore(self, plan)

    def register_projects(self, projects: list[ProjectMapping]) -> list[str]:
        from agent_port.adapters.codex.restore import register_projects

        return register_projects(projects)

    def detect(self, source: Path) -> DetectionResult:
        evidence: list[str] = []
        score = 0
        if source.name == ".codex":
            evidence.append("directory is named .codex")
            score += 2
        indicators = {
            "config.toml": 2,
            "sessions": 4,
            "history.jsonl": 2,
            "skills": 1,
        }
        for name, weight in indicators.items():
            if (source / name).exists():
                evidence.append(f"found {name}")
                score += weight
        if any(source.glob("*.sqlite*")):
            evidence.append("found SQLite state")
            score += 3
        return DetectionResult(
            status=DetectionStatus.DETECTED if score else DetectionStatus.UNSUPPORTED,
            harness=self.name if score else None,
            score=score,
            evidence=evidence,
        )

    def inspect(self, source: Path, user_home: Path | None = None) -> InspectionReport:
        source = source.resolve()
        if not source.is_dir():
            raise InspectionError(f"Codex home is not a directory: {source}")
        diagnostics: list[Diagnostic] = []
        transcript_files = self._transcript_files(source)
        thread_ids: set[str] = set()
        thread_projects: dict[str, str] = {}
        project_paths: set[str] = set()
        versions: set[str] = set()
        for transcript in transcript_files:
            summary = inspect_jsonl(transcript, self.name.value)
            thread_ids.update(summary.session_ids)
            project_paths.update(summary.project_paths)
            versions.update(summary.versions)
            diagnostics.extend(summary.diagnostics)
            if len(summary.project_paths) == 1:
                project = next(iter(summary.project_paths))
                for thread_id in summary.session_ids:
                    thread_projects[thread_id] = project

        schemas = []
        rollout_paths: set[str] = set()
        database_paths, database_diagnostics = self._database_paths(source, user_home)
        diagnostics.extend(database_diagnostics)
        for database in database_paths:
            inspected = inspect_database(database)
            schemas.append(inspected.schema)
            thread_ids.update(inspected.thread_ids)
            project_paths.update(inspected.project_paths)
            thread_projects.update(inspected.thread_projects)
            rollout_paths.update(inspected.rollout_paths)
            diagnostics.extend(inspected.diagnostics)

        for rollout in sorted(rollout_paths):
            rollout_path = Path(rollout)
            if not rollout_path.exists():
                diagnostics.append(
                    Diagnostic(
                        code="missing-rollout",
                        severity=Severity.WARNING,
                        message="Database references a rollout file that does not exist.",
                        path=rollout,
                    )
                )

        if transcript_files and not thread_ids:
            diagnostics.append(
                Diagnostic(
                    code="unknown-conversation-count",
                    severity=Severity.WARNING,
                    message=(
                        "Transcript files exist, but unique thread IDs could not be established."
                    ),
                )
            )
        projects = [
            ProjectRecord(
                path=path,
                exists=Path(path).is_dir(),
                conversation_count=sum(
                    1 for project_path in thread_projects.values() if project_path == path
                ),
                repository_fingerprint=repository_fingerprint(Path(path)),
            )
            for path in sorted(project_paths)
        ]
        skills = self._discover_skills(source, user_home, projects)
        attachments = self._attachment_files(source)
        exclusions = self._exclusions(source)
        version = next(iter(versions)) if len(versions) == 1 else None
        if len(versions) > 1:
            diagnostics.append(
                Diagnostic(
                    code="mixed-harness-versions",
                    severity=Severity.WARNING,
                    message="Transcripts were created by multiple Codex versions.",
                )
            )
        return InspectionReport(
            source=str(source),
            harness=self.name,
            capabilities=CapabilitySet(project_registration=True),
            harness_version=version,
            harness_versions=sorted(versions),
            compatibility_profiles=compatibility_profiles(versions),
            counts=CountSummary(
                conversations=(
                    len(thread_ids) if thread_ids else (0 if not transcript_files else None)
                ),
                transcript_files=len(transcript_files),
                projects=len(projects),
                attachments=len(attachments),
                skills=len(skills),
            ),
            native_schema=schemas,
            projects=projects,
            skills=skills,
            exclusions=exclusions,
            diagnostics=diagnostics,
        )

    def collect_backup(
        self,
        source: Path,
        staging: Path,
        include: frozenset[str],
        report: InspectionReport,
        user_home: Path | None = None,
    ) -> BackupCollection:
        if "sessions" in include:
            for transcript in self._transcript_files(source):
                relative = transcript.relative_to(source)
                stable_copy(transcript, staging / "native" / "sessions" / relative)
            for name in ("history.jsonl", "session_index.jsonl"):
                candidate = source / name
                if candidate.is_file():
                    stable_copy(candidate, staging / "native" / "sessions" / name)
            attachments = source / "attachments"
            if attachments.is_dir():
                copy_tree_safely(attachments, staging / "native" / "sessions" / "attachments")
            database_paths, _diagnostics = self._database_paths(source, user_home)
            for index, database in enumerate(database_paths):
                name = database.name
                destination = staging / "native" / "state" / name
                if destination.exists():
                    destination = staging / "native" / "state" / f"{index}-{name}"
                snapshot_database(database, destination)

        included_skills: list[SkillRecord] = []
        if "skills" in include:
            eligible = [skill for skill in report.skills if skill.eligible]
            unsafe_user_skills = [
                skill
                for skill in report.skills
                if skill.owner is SkillOwner.USER and not skill.eligible
            ]
            if unsafe_user_skills:
                names = ", ".join(sorted(skill.name for skill in unsafe_user_skills))
                raise BackupError(f"Unsafe user skills must be resolved before backup: {names}")
            destinations: set[str] = set()
            for skill in eligible:
                directory_name = Path(skill.source_path).name
                if directory_name in destinations:
                    raise BackupError(f"Duplicate user skill directory name: {directory_name}")
                destinations.add(directory_name)
                copy_skill(skill, staging / "native" / "skills" / directory_name)
                included_skills.append(skill)
        return BackupCollection(included_skills=included_skills)

    @staticmethod
    def _transcript_files(source: Path) -> list[Path]:
        files: set[Path] = set()
        for root_name in ("sessions", "archived_sessions"):
            root = source / root_name
            if root.is_dir():
                files.update(path for path in root.rglob("*.jsonl") if path.is_file())
        return sorted(files)

    @staticmethod
    def _attachment_files(source: Path) -> list[Path]:
        root = source / "attachments"
        return sorted(path for path in root.rglob("*") if path.is_file()) if root.is_dir() else []

    def _discover_skills(
        self, source: Path, user_home: Path | None, projects: list[ProjectRecord]
    ) -> list[SkillRecord]:
        effective_home = user_home.resolve() if user_home else None
        if effective_home is None and source.name == ".codex":
            effective_home = source.parent
        records: list[SkillRecord] = []
        if effective_home:
            records.extend(
                scan_skill_root(
                    effective_home / ".agents" / "skills", SkillOwner.USER, SkillScope.USER
                )
            )
        records.extend(self._configured_skills(source, effective_home, projects))
        legacy = source / "skills"
        records.extend(scan_skill_root(legacy, SkillOwner.USER, SkillScope.USER))
        records.extend(scan_skill_root(legacy / ".system", SkillOwner.SYSTEM, SkillScope.SYSTEM))
        for cache_name in (".curated", ".cache", "curated-cache"):
            records.extend(
                scan_skill_root(legacy / cache_name, SkillOwner.CURATED_CACHE, SkillScope.CACHE)
            )
        plugin_root = source / "plugins"
        if plugin_root.is_dir():
            for root in sorted(plugin_root.rglob("skills")):
                records.extend(scan_skill_root(root, SkillOwner.PLUGIN, SkillScope.PLUGIN))
        for project in projects:
            if project.exists:
                records.extend(
                    scan_skill_root(
                        Path(project.path) / ".agents" / "skills",
                        SkillOwner.PROJECT,
                        SkillScope.PROJECT,
                    )
                )
        return self._deduplicate_skills(records)

    @staticmethod
    def _configured_skills(
        source: Path,
        effective_home: Path | None,
        projects: list[ProjectRecord],
    ) -> list[SkillRecord]:
        config = source / "config.toml"
        if not config.is_file():
            return []
        try:
            values = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return []
        skills_value = values.get("skills")
        if not isinstance(skills_value, dict):
            return []
        entries = skills_value.get("config")
        if not isinstance(entries, list):
            return []
        records: list[SkillRecord] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            configured = entry["path"]
            if configured == "~" and effective_home:
                manifest = effective_home
            elif configured.startswith("~/") and effective_home:
                manifest = effective_home / configured[2:]
            elif configured.startswith("~"):
                continue
            else:
                manifest = Path(configured)
            if not manifest.is_absolute():
                manifest = source / manifest
            manifest = manifest.resolve()
            skill_dir = manifest if manifest.is_dir() else manifest.parent
            if not (skill_dir / "SKILL.md").is_file():
                continue
            owner, scope = CodexAdapter._classify_configured_skill(
                skill_dir, source, effective_home, projects
            )
            records.append(inspect_skill(skill_dir, owner, scope))
        return records

    @staticmethod
    def _classify_configured_skill(
        skill_dir: Path,
        source: Path,
        effective_home: Path | None,
        projects: list[ProjectRecord],
    ) -> tuple[SkillOwner, SkillScope]:
        if effective_home and skill_dir.is_relative_to(effective_home / ".agents" / "skills"):
            return SkillOwner.USER, SkillScope.USER
        if skill_dir.is_relative_to(source / "skills" / ".system"):
            return SkillOwner.SYSTEM, SkillScope.SYSTEM
        if skill_dir.is_relative_to(source / "plugins"):
            return SkillOwner.PLUGIN, SkillScope.PLUGIN
        for project in projects:
            if project.exists and skill_dir.is_relative_to(
                Path(project.path) / ".agents" / "skills"
            ):
                return SkillOwner.PROJECT, SkillScope.PROJECT
        return SkillOwner.UNKNOWN, SkillScope.UNKNOWN

    @staticmethod
    def _deduplicate_skills(records: list[SkillRecord]) -> list[SkillRecord]:
        by_path: dict[str, SkillRecord] = {}
        for record in records:
            by_path.setdefault(record.source_path, record)
        return sorted(
            by_path.values(),
            key=lambda record: (record.owner.value, record.name, record.source_path),
        )

    @staticmethod
    def _exclusions(source: Path) -> list[ExcludedPathRecord]:
        reasons = {
            "auth.json": "authentication credentials are never archived",
            "config.toml": "configuration may contain machine-specific or sensitive values",
            "logs": "runtime logs are not migration data",
            "cache": "cache data is recreated by Codex",
            "plugins": "plugins must be reinstalled through their owner",
            "tmp": "temporary runtime data is not migration data",
        }
        return [
            ExcludedPathRecord(path=str(source / name), reason=reason)
            for name, reason in reasons.items()
            if (source / name).exists()
        ]

    @staticmethod
    def _database_paths(
        source: Path, user_home: Path | None
    ) -> tuple[list[Path], list[Diagnostic]]:
        roots = [source]
        diagnostics: list[Diagnostic] = []
        effective_home = user_home.resolve() if user_home else None
        if effective_home is None and source.name == ".codex":
            effective_home = source.parent.resolve()
        config = source / "config.toml"
        if config.is_file():
            try:
                values = tomllib.loads(config.read_text(encoding="utf-8"))
                configured = values.get("sqlite_home")
                if isinstance(configured, str) and configured:
                    if configured == "~" and effective_home:
                        configured_path = effective_home
                    elif configured.startswith("~/") and effective_home:
                        configured_path = effective_home / configured[2:]
                    elif configured.startswith("~"):
                        diagnostics.append(
                            Diagnostic(
                                code="unresolved-sqlite-home",
                                severity=Severity.WARNING,
                                message=(
                                    "Configured sqlite_home uses ~ but no portable user home "
                                    "was authorized."
                                ),
                                path=configured,
                            )
                        )
                        configured_path = source / "__unresolved_sqlite_home__"
                    else:
                        configured_path = Path(configured)
                    if not configured_path.is_absolute():
                        configured_path = source / configured_path
                    configured_path = configured_path.resolve()
                    allowed_roots = [source.resolve()]
                    if effective_home:
                        allowed_roots.append(effective_home)
                    if any(configured_path.is_relative_to(root) for root in allowed_roots):
                        roots.append(configured_path)
                    else:
                        diagnostics.append(
                            Diagnostic(
                                code="external-sqlite-home",
                                severity=Severity.WARNING,
                                message=(
                                    "Configured sqlite_home is outside authorized source roots."
                                ),
                                path=str(configured_path),
                            )
                        )
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
                diagnostics.append(
                    Diagnostic(
                        code="invalid-codex-config",
                        severity=Severity.WARNING,
                        message=f"Could not read sqlite_home from config.toml: {error}",
                        path=str(config),
                    )
                )
        paths: set[Path] = set()
        for root in roots:
            if not root.is_dir():
                continue
            for pattern in ("*.sqlite", "*.sqlite3", "*.db"):
                paths.update(path for path in root.glob(pattern) if path.is_file())
        return sorted(paths), diagnostics
