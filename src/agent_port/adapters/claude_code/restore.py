from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from agent_port.adapters.restore_support import (
    apply_append_only_jsonl,
    classify_append_only_jsonl,
    claude_transcript_candidates,
    destination_session_candidates,
    plan_skills,
    project_mapper,
    sha256_bytes,
    sha256_file,
    stage_payload_tree,
    transform_jsonl,
    verify_expected_payloads,
)
from agent_port.domain.errors import RestoreError
from agent_port.domain.models import (
    Diagnostic,
    OperationKind,
    PayloadRecord,
    PlannedOperation,
    RestoreConflict,
    RestorePlan,
    Severity,
    VerificationReport,
)
from agent_port.domain.ports import AdapterRestorePlan, RestorePlanningContext
from agent_port.infrastructure.archive.common import validate_member_name
from agent_port.infrastructure.filesystem.jsonl import inspect_jsonl
from agent_port.infrastructure.path_mapping import PathMapper
from agent_port.infrastructure.versions import compatibility_profile, compatibility_profiles

CLAUDE_TRANSCRIPT_PROFILE_V1 = {
    "session_id_fields": ("sessionId", "session_id"),
    "project_path_field": "cwd",
    "version_field": "version",
}


def plan_restore(context: RestorePlanningContext) -> AdapterRestorePlan:
    mapper = project_mapper(context)
    operations: list[PlannedOperation] = []
    conflicts: list[RestoreConflict] = []
    diagnostics: list[Diagnostic] = []
    session_payloads = [
        payload for payload in context.payloads if payload.role.value == "transcript"
    ]
    if session_payloads:
        source_profiles = _source_compatibility_profiles(context, session_payloads)
        destination_profiles = _destination_compatibility_profiles(context.destination)
        if not source_profiles or not destination_profiles:
            conflicts.append(
                RestoreConflict(
                    kind="unknown-claude-version",
                    identity="version",
                    destination=str(context.destination),
                    message=(
                        "Claude Code session restore requires known source and destination "
                        "versions."
                    ),
                )
            )
        elif len(source_profiles) > 1:
            conflicts.append(
                RestoreConflict(
                    kind="incompatible-claude-source-versions",
                    identity="source-versions",
                    destination=str(context.destination),
                    message=(
                        "Source transcripts require multiple Claude Code compatibility profiles."
                    ),
                )
            )
        elif len(destination_profiles) > 1:
            conflicts.append(
                RestoreConflict(
                    kind="incompatible-claude-destination-versions",
                    identity="destination-versions",
                    destination=str(context.destination),
                    message=(
                        "Destination transcripts require multiple Claude Code compatibility "
                        "profiles."
                    ),
                )
            )
        elif source_profiles != destination_profiles:
            source_version = next(iter(source_profiles))
            destination_version = next(iter(destination_profiles))
            conflicts.append(
                RestoreConflict(
                    kind="incompatible-claude-version",
                    identity=f"{source_version}->{destination_version}",
                    destination=str(context.destination),
                    message="Claude Code session restore requires the same major/minor version.",
                )
            )
        else:
            source_version = next(iter(source_profiles))
            diagnostics.append(
                Diagnostic(
                    code="claude-version-compatible",
                    severity=Severity.INFO,
                    message=f"Claude Code compatibility profile {source_version} selected.",
                )
            )

    destination_ids = destination_session_candidates(context.destination, "claude-code")
    project_destinations = {project.source: project.destination for project in context.projects}
    for payload in context.payloads:
        if payload.role.value in {"skill", "database"}:
            continue
        validate_member_name(payload.member)
        validate_member_name(payload.original_path)
        source = context.extracted / Path(*payload.member.split("/"))
        original_parts = payload.original_path.split("/")
        destination_relative = payload.original_path
        if len(original_parts) >= 3 and original_parts[0] == "projects":
            mapped_project = (
                project_destinations.get(payload.project_path, payload.project_path)
                if payload.project_path
                else None
            )
            if mapped_project:
                original_parts[1] = encode_project_path(mapped_project)
                destination_relative = "/".join(original_parts)
        destination = context.destination / Path(*destination_relative.split("/"))
        expected: str | None
        if payload.role.value == "transcript":
            transformed = transform_jsonl(source, "claude-code", mapper)
            summary = inspect_jsonl(source, "claude-code")
            identities = summary.session_ids
            if len(identities) != 1:
                conflicts.append(
                    RestoreConflict(
                        kind="ambiguous-session-id",
                        identity=payload.original_path,
                        destination=str(destination),
                        message=(
                            "Claude transcript does not match compatibility profile v1: "
                            "exactly one structural session ID is required."
                        ),
                    )
                )
                continue
            identity = next(iter(identities))
            candidates = destination_ids.get(identity, [])
            if len(candidates) > 1:
                conflicts.append(
                    RestoreConflict(
                        kind="ambiguous-destination-session-id",
                        identity=identity,
                        destination=str(context.destination),
                        message="Destination contains multiple transcripts for this session ID.",
                    )
                )
                continue
            existing = candidates[0] if candidates else None
            if existing is not None:
                existing_summary = inspect_jsonl(existing, "claude-code")
                if existing_summary.session_ids != {identity}:
                    conflicts.append(
                        RestoreConflict(
                            kind="ambiguous-destination-session-id",
                            identity=identity,
                            destination=str(existing),
                            message=(
                                "Destination transcript does not have exactly one matching "
                                "structural session ID."
                            ),
                        )
                    )
                    continue
                try:
                    relation, merged = classify_append_only_jsonl(transformed, existing)
                except RestoreError as error:
                    conflicts.append(
                        RestoreConflict(
                            kind="malformed-session-collision",
                            identity=identity,
                            destination=str(existing),
                            message=f"Session history cannot be compared safely: {error}",
                        )
                    )
                    continue
                if relation in {"identical", "destination-ahead"}:
                    operations.append(
                        PlannedOperation(
                            kind=OperationKind.SKIP,
                            member=payload.member,
                            source=payload.original_path,
                            destination=str(existing),
                            identity=identity,
                            expected_sha256=sha256_file(existing),
                        )
                    )
                    if relation == "destination-ahead":
                        diagnostics.append(
                            Diagnostic(
                                code="destination-session-appended",
                                severity=Severity.INFO,
                                message=(
                                    "Destination has a valid append-only continuation; "
                                    "preserved it."
                                ),
                                path=str(existing),
                            )
                        )
                elif relation == "source-ahead":
                    operations.append(
                        PlannedOperation(
                            kind=OperationKind.MERGE_JSONL,
                            member=payload.member,
                            source=payload.original_path,
                            destination=str(existing),
                            identity=identity,
                            expected_sha256=sha256_bytes(merged),
                        )
                    )
                else:
                    conflicts.append(
                        RestoreConflict(
                            kind="session-id-collision",
                            identity=identity,
                            destination=str(existing),
                            message="Source and destination session histories genuinely diverge.",
                        )
                    )
                continue
            expected = sha256_bytes(transformed)
        else:
            expected = payload.sha256 or (sha256_file(source) if source.is_file() else None)
        if destination.exists() or destination.is_symlink():
            actual = sha256_file(destination) if destination.is_file() else None
            if expected == actual:
                operations.append(
                    PlannedOperation(
                        kind=OperationKind.SKIP,
                        member=payload.member,
                        destination=str(destination),
                        expected_sha256=expected,
                    )
                )
            else:
                conflicts.append(
                    RestoreConflict(
                        kind="payload-path-collision",
                        identity=payload.original_path,
                        destination=str(destination),
                        message="Destination contains different session or sidecar data.",
                    )
                )
        else:
            operations.append(
                PlannedOperation(
                    kind=OperationKind.COPY,
                    member=payload.member,
                    source=payload.original_path,
                    destination=str(destination),
                    identity=(identity if payload.role.value == "transcript" else None),
                    expected_sha256=expected,
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
            destination.write_bytes(transform_jsonl(source, "claude-code", mapper))
        else:
            stage_payload_tree(source, destination)


def apply_restore(plan: RestorePlan, staging: Path) -> None:
    for operation in plan.operations:
        if operation.kind is not OperationKind.MERGE_JSONL or operation.member is None:
            continue
        source = staging / Path(*operation.member.split("/"))
        apply_append_only_jsonl(source, Path(operation.destination))


def verify_restore(plan: RestorePlan) -> VerificationReport:
    payload_verification = verify_expected_payloads(plan.operations, "claude-code")
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
        elif operation.member and operation.member.endswith(".jsonl"):
            summary = inspect_jsonl(destination, "claude-code")
            diagnostics.extend(summary.diagnostics)
            valid = valid and not any(
                item.severity is Severity.ERROR for item in summary.diagnostics
            )
    checks.append("Claude Code transcript JSONL validated")
    return VerificationReport(valid=valid, checks=checks, diagnostics=diagnostics)


def encode_project_path(value: str) -> str:
    return value.replace("\\", "-").replace("/", "-").replace(":", "-")


def _source_compatibility_profiles(
    context: RestorePlanningContext,
    session_payloads: list[PayloadRecord],
) -> set[str]:
    values = set(context.manifest.harness_versions)
    if context.manifest.harness_version:
        values.add(context.manifest.harness_version)
    profiles = set(context.manifest.compatibility_profiles)
    for payload in session_payloads:
        source = context.extracted / Path(*payload.member.split("/"))
        values.update(inspect_jsonl(source, "claude-code").versions)
    profiles.update(compatibility_profiles(values))
    return profiles


def _destination_compatibility_profiles(destination: Path) -> set[str]:
    versions: set[str] = set()
    for transcript in claude_transcript_candidates(destination):
        versions.update(inspect_jsonl(transcript, "claude-code").versions)
    profiles = set(compatibility_profiles(versions))
    if profiles:
        return profiles
    executable = shutil.which("claude")
    if executable:
        try:
            completed = subprocess.run(
                [executable, "--version"], capture_output=True, text=True, timeout=5, check=False
            )
            if completed.returncode == 0:
                value = completed.stdout.strip() or completed.stderr.strip()
                profile = compatibility_profile(value)
                return {profile} if profile else set()
        except (OSError, subprocess.SubprocessError):
            pass
    return set()
