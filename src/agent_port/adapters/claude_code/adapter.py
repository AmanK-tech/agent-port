from __future__ import annotations

from pathlib import Path

from agent_port.domain.errors import BackupError, InspectionError
from agent_port.domain.models import (
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
from agent_port.infrastructure.filesystem.copying import copy_tree_safely
from agent_port.infrastructure.filesystem.jsonl import JsonlSummary, inspect_jsonl
from agent_port.infrastructure.filesystem.skills import copy_skill, scan_skill_root
from agent_port.infrastructure.repositories import repository_fingerprint
from agent_port.infrastructure.versions import compatibility_profiles

CLAUDE_NOISE_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})


class ClaudeCodeAdapter:
    name = HarnessName.CLAUDE_CODE

    def plan_restore(self, context: RestorePlanningContext) -> AdapterRestorePlan:
        from agent_port.adapters.claude_code.restore import plan_restore

        return plan_restore(context)

    def stage_restore(self, plan: RestorePlan, extracted: Path, staging: Path) -> None:
        from agent_port.adapters.claude_code.restore import stage_restore

        stage_restore(plan, extracted, staging)

    def apply_restore(self, plan: RestorePlan, staging: Path) -> None:
        from agent_port.adapters.claude_code.restore import apply_restore

        apply_restore(plan, staging)

    def verify_restore(self, plan: RestorePlan) -> VerificationReport:
        from agent_port.adapters.claude_code.restore import verify_restore

        return verify_restore(plan)

    def register_projects(self, projects: list[ProjectMapping]) -> list[str]:
        del projects
        return []

    def detect(self, source: Path) -> DetectionResult:
        evidence: list[str] = []
        score = 0
        if source.name == ".claude":
            evidence.append("directory is named .claude")
            score += 2
        indicators = {
            "projects": 5,
            "settings.json": 2,
            "skills": 1,
        }
        for name, weight in indicators.items():
            if (source / name).exists():
                evidence.append(f"found {name}")
                score += weight
        return DetectionResult(
            status=DetectionStatus.DETECTED if score else DetectionStatus.UNSUPPORTED,
            harness=self.name if score else None,
            score=score,
            evidence=evidence,
        )

    def inspect(self, source: Path, user_home: Path | None = None) -> InspectionReport:
        del user_home
        source = source.resolve()
        if not source.is_dir():
            raise InspectionError(f"Claude Code config directory is not a directory: {source}")
        projects_root = source / "projects"
        candidate_jsonl = (
            sorted(
                path
                for path in projects_root.rglob("*.jsonl")
                if path.is_file() and not _is_noise_path(path, projects_root)
            )
            if projects_root.is_dir()
            else []
        )
        transcript_files = [
            path for path in candidate_jsonl if _is_claude_transcript(path, projects_root)
        ]
        summaries: dict[Path, JsonlSummary] = {}
        project_paths_by_key: dict[str, set[str]] = {}
        main_transcripts_by_key: dict[str, int] = {}
        for transcript in transcript_files:
            summary = inspect_jsonl(transcript, self.name.value)
            summaries[transcript] = summary
            try:
                relative = transcript.relative_to(projects_root)
            except ValueError:
                continue
            if relative.parts:
                project_key = relative.parts[0]
                if len(relative.parts) == 2:
                    main_transcripts_by_key[project_key] = (
                        main_transcripts_by_key.get(project_key, 0) + 1
                    )
                    project_paths_by_key.setdefault(project_key, set()).update(
                        summary.project_paths
                    )

        active_project_keys = {
            key for key, conversation_count in main_transcripts_by_key.items() if conversation_count
        }
        included_transcripts = [
            transcript
            for transcript in transcript_files
            if transcript.relative_to(projects_root).parts[0] in active_project_keys
        ]
        versions = {
            version
            for transcript in included_transcripts
            for version in summaries[transcript].versions
        }
        diagnostics = [
            diagnostic
            for transcript in included_transcripts
            for diagnostic in summaries[transcript].diagnostics
        ]
        projects: list[ProjectRecord] = []
        inactive_project_containers = 0
        if projects_root.is_dir():
            for encoded in sorted(path for path in projects_root.iterdir() if path.is_dir()):
                conversation_count = main_transcripts_by_key.get(encoded.name, 0)
                if conversation_count == 0:
                    inactive_project_containers += 1
                    continue
                paths = project_paths_by_key.get(encoded.name, set())
                if paths:
                    projects.extend(
                        ProjectRecord(
                            path=path,
                            exists=Path(path).is_dir(),
                            conversation_count=conversation_count,
                            repository_fingerprint=repository_fingerprint(Path(path)),
                        )
                        for path in sorted(paths)
                    )
                else:
                    projects.append(
                        ProjectRecord(
                            path=f"encoded:{encoded.name}",
                            exists=False,
                            conversation_count=conversation_count,
                        )
                    )
                    diagnostics.append(
                        Diagnostic(
                            code="opaque-project-path",
                            severity=Severity.WARNING,
                            message=(
                                "Project directory could not be recovered from structural metadata."
                            ),
                            path=str(encoded),
                        )
                    )
        if inactive_project_containers:
            diagnostics.append(
                Diagnostic(
                    code="inactive-project-containers",
                    severity=Severity.INFO,
                    message=(
                        f"Ignored {inactive_project_containers} project container(s) with no "
                        "top-level conversation transcript."
                    ),
                )
            )

        skills = self._discover_skills(source, projects)
        included_transcript_set = set(included_transcripts)
        sidecars = [
            path
            for path in projects_root.rglob("*")
            if projects_root.is_dir()
            and path.is_file()
            and path not in included_transcript_set
            and not _is_noise_path(path, projects_root)
            and path.relative_to(projects_root).parts[0] in active_project_keys
        ]
        version = next(iter(versions)) if len(versions) == 1 else None
        profiles = compatibility_profiles(versions)
        if len(profiles) > 1:
            diagnostics.append(
                Diagnostic(
                    code="mixed-harness-versions",
                    severity=Severity.WARNING,
                    message="Transcripts require multiple Claude Code compatibility profiles.",
                )
            )
        elif len(versions) > 1:
            diagnostics.append(
                Diagnostic(
                    code="mixed-harness-patch-versions",
                    severity=Severity.INFO,
                    message="Transcript patch versions share one compatible major/minor profile.",
                )
            )
        return InspectionReport(
            source=str(source),
            harness=self.name,
            harness_version=version,
            harness_versions=sorted(versions),
            compatibility_profiles=profiles,
            counts=CountSummary(
                conversations=sum(main_transcripts_by_key.values()),
                subagent_transcripts=(
                    len(included_transcripts) - sum(main_transcripts_by_key.values())
                ),
                transcript_files=len(included_transcripts),
                projects=len(active_project_keys),
                attachments=len(sidecars),
                skills=len(skills),
            ),
            projects=projects,
            skills=skills,
            exclusions=self._exclusions(source),
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
        del user_home
        if "sessions" in include and (source / "projects").is_dir():
            projects_root = source / "projects"
            for container in sorted(path for path in projects_root.iterdir() if path.is_dir()):
                if not any(
                    path.is_file() and not _is_noise_path(path, projects_root)
                    for path in container.glob("*.jsonl")
                ):
                    continue
                copy_tree_safely(
                    container,
                    staging / "native" / "sessions" / "projects" / container.name,
                    excluded_names=CLAUDE_NOISE_NAMES,
                )

        included_skills: list[SkillRecord] = []
        if "skills" in include:
            unsafe_user_skills = [
                skill
                for skill in report.skills
                if skill.owner is SkillOwner.USER and not skill.eligible
            ]
            if unsafe_user_skills:
                names = ", ".join(sorted(skill.name for skill in unsafe_user_skills))
                raise BackupError(f"Unsafe user skills must be resolved before backup: {names}")
            destinations: set[str] = set()
            for skill in (skill for skill in report.skills if skill.eligible):
                directory_name = Path(skill.source_path).name
                if directory_name in destinations:
                    raise BackupError(f"Duplicate user skill directory name: {directory_name}")
                destinations.add(directory_name)
                copy_skill(skill, staging / "native" / "skills" / directory_name)
                included_skills.append(skill)
        return BackupCollection(included_skills=included_skills)

    def _discover_skills(self, source: Path, projects: list[ProjectRecord]) -> list[SkillRecord]:
        records = scan_skill_root(source / "skills", SkillOwner.USER, SkillScope.USER)
        plugin_root = source / "plugins"
        if plugin_root.is_dir():
            for root in sorted(plugin_root.rglob("skills")):
                records.extend(scan_skill_root(root, SkillOwner.PLUGIN, SkillScope.PLUGIN))
        for project in projects:
            if project.exists and not project.path.startswith("encoded:"):
                records.extend(
                    scan_skill_root(
                        Path(project.path) / ".claude" / "skills",
                        SkillOwner.PROJECT,
                        SkillScope.PROJECT,
                    )
                )
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
            ".credentials.json": "authentication credentials are never archived",
            "settings.json": "configuration is outside native session migration",
            "statsig": "telemetry state is not migration data",
            "debug": "debug logs are not migration data",
            "cache": "cache data is recreated by Claude Code",
            "plugins": "plugins must be reinstalled through their owner",
        }
        return [
            ExcludedPathRecord(path=str(source / name), reason=reason)
            for name, reason in reasons.items()
            if (source / name).exists()
        ]


def _is_noise_path(path: Path, projects_root: Path) -> bool:
    try:
        parts = path.relative_to(projects_root).parts
    except ValueError:
        parts = (path.name,)
    return any(part.startswith("._") or part.casefold() in CLAUDE_NOISE_NAMES for part in parts)


def _is_claude_transcript(path: Path, projects_root: Path) -> bool:
    relative = path.relative_to(projects_root)
    return len(relative.parts) == 2 or "subagents" in relative.parts[1:-1]
