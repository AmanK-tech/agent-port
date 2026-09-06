from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agent_port import __version__
from agent_port.application.backup import BackupService
from agent_port.application.handoff import RestoreHandoffService
from agent_port.application.inspection import InspectService
from agent_port.application.migration import (
    MIGRATION_STATE_NAME,
    MigrationWorkspaceService,
    consume_pairing_code_file,
    load_migration_document,
    next_restore_plan_path,
    update_migration_document,
)
from agent_port.application.restore import (
    DoctorService,
    RestoreApplyService,
    RestorePlanInfoService,
    RestorePlanService,
    RollbackService,
)
from agent_port.application.transfer import TransferReceiveService, TransferSendService
from agent_port.application.verification import RestoreVerificationService
from agent_port.domain.errors import AgentPortError, RestoreError, TransferError
from agent_port.domain.models import (
    ArchiveInspectionReport,
    CountSummary,
    InspectionReport,
    MigrationStateValue,
    MigrationWorkspaceDocument,
    RestorePlanInfo,
    TransferOffer,
)
from agent_port.presentation.rendering import (
    render_archive,
    render_inspection,
    render_json,
)

app = typer.Typer(
    name="agent-port",
    help="Inspect, transfer, back up, and safely restore native coding-agent sessions and skills.",
    no_args_is_help=True,
)
skills_app = typer.Typer(help="Inspect user, project, system, and managed skills.")
app.add_typer(skills_app, name="skills")
restore_app = typer.Typer(help="Plan, hand off, apply, verify, or roll back a native restore.")
app.add_typer(restore_app, name="restore")
transfer_app = typer.Typer(help="Send or receive an encrypted .agentpack on the local network.")
app.add_typer(transfer_app, name="transfer")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Agent Port is local-first and never uploads archive data."""


@app.command("inspect")
def inspect_command(
    source: Annotated[Path, typer.Argument(help="Harness home or .agentpack archive.")],
    harness: Annotated[
        str, typer.Option("--harness", help="auto, codex, or claude-code.")
    ] = "auto",
    output_format: Annotated[str, typer.Option("--format", help="text or json.")] = "text",
    redact_paths: Annotated[
        bool, typer.Option("--redact-paths", help="Redact filesystem paths in report output.")
    ] = False,
    user_home: Annotated[
        Path | None,
        typer.Option("--user-home", help="Portable source home for external Codex skill roots."),
    ] = None,
) -> None:
    """Inspect a harness home or verify an archive without changing it."""
    try:
        result = InspectService().execute(source, harness, user_home)
        _emit_report(result, output_format, redact_paths)
    except AgentPortError as error:
        _fail(error)


@skills_app.command("inspect")
def inspect_skills_command(
    source: Annotated[Path, typer.Argument(help="Harness home to inspect.")],
    harness: Annotated[str, typer.Option("--harness")] = "auto",
    output_format: Annotated[str, typer.Option("--format")] = "text",
    user_home: Annotated[Path | None, typer.Option("--user-home")] = None,
) -> None:
    """Classify skills without executing their scripts."""
    try:
        result = InspectService().execute(source, harness, user_home)
        if not isinstance(result, InspectionReport):
            raise typer.BadParameter("skills inspect requires a harness home, not an archive")
        _validate_format(output_format)
        typer.echo(
            render_json(result)
            if output_format == "json"
            else render_inspection(result, skills_only=True)
        )
    except AgentPortError as error:
        _fail(error)


@app.command("backup")
def backup_command(
    source: Annotated[Path, typer.Argument(help="Harness home to back up.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Destination .agentpack file.")],
    harness: Annotated[str, typer.Option("--harness")] = "auto",
    include: Annotated[
        str, typer.Option("--include", help="Comma-separated: sessions,skills.")
    ] = "sessions,skills",
    user_home: Annotated[Path | None, typer.Option("--user-home")] = None,
) -> None:
    """Create a checksummed native backup without modifying the source."""
    try:
        selected = frozenset(item.strip() for item in include.split(",") if item.strip())
        result = BackupService().execute(source, output, harness, selected, user_home)
        typer.echo(f"Created {result.output}")
        typer.echo(f"Harness: {result.manifest.harness.value}")
        typer.echo(f"Archive members: {result.archived_files}")
        typer.echo(f"Archived bytes: {result.archived_bytes}")
        _emit_content_counts(result.manifest.counts)
    except AgentPortError as error:
        _fail(error)


@transfer_app.command("send")
def transfer_send_command(
    source: Annotated[Path, typer.Argument(help="Harness home to back up and transfer.")],
    harness: Annotated[str, typer.Option("--harness")] = "auto",
    include: Annotated[
        str, typer.Option("--include", help="Comma-separated: sessions,skills.")
    ] = "sessions,skills",
    user_home: Annotated[Path | None, typer.Option("--user-home")] = None,
    timeout: Annotated[
        int, typer.Option("--timeout", help="Pairing-code lifetime in seconds (30-3600).")
    ] = 600,
) -> None:
    """Create a temporary backup and serve it once over an encrypted LAN connection."""

    def show_offer(offer: TransferOffer) -> None:
        typer.echo(f"Pairing code: {offer.pairing_code}")
        typer.echo(f"Expires at Unix time: {offer.expires_at}")
        if offer.discovery_available:
            typer.echo("LAN discovery: available")
        else:
            typer.echo("LAN discovery: unavailable; use the fallback endpoint below")
        for host in offer.hosts:
            typer.echo(f"Fallback endpoint: {host}:{offer.port}")
        typer.echo("Destination is ready for Agent Port plugin-guided receipt.")

    try:
        selected = frozenset(item.strip() for item in include.split(",") if item.strip())
        result = TransferSendService().execute(
            source=source,
            requested_harness=harness,
            include=selected,
            user_home=user_home,
            timeout=timeout,
            on_ready=show_offer,
        )
        typer.echo(f"Transfer verified by receiver ({result.archive_sha256}).")
        _emit_content_counts(result.content)
    except AgentPortError as error:
        _fail(error)


@transfer_app.command("prepare")
def transfer_prepare_command(
    root: Annotated[
        Path | None,
        typer.Option(
            "--root", help="Persistent migration root (default: ~/Agent-Port/Migrations)."
        ),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Existing migration workspace to prepare for a retry."),
    ] = None,
    output_format: Annotated[str, typer.Option("--format", help="text or json.")] = "text",
    redact_paths: Annotated[bool, typer.Option("--redact-paths")] = False,
) -> None:
    """Create a persistent migration workspace and protected one-use code file."""
    try:
        _validate_format(output_format)
        result = MigrationWorkspaceService().execute(root=root, workspace=workspace)
        if output_format == "json":
            typer.echo(render_json(result, redact_paths=redact_paths))
        else:
            typer.echo(f"Migration workspace prepared: {result.migration_id}")
            if not redact_paths:
                typer.echo(f"Workspace: {result.workspace}")
                typer.echo(f"Archive: {result.archive}")
                typer.echo(f"One-use pairing-code file: {result.pairing_code_file}")
    except AgentPortError as error:
        _fail(error)


@transfer_app.command("receive")
def transfer_receive_command(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Destination .agentpack; defaults inside --workspace."),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Prepared persistent migration workspace."),
    ] = None,
    pairing_code: Annotated[
        str | None,
        typer.Option(
            "--pairing-code",
            help="Temporary pairing code for a plugin or other non-interactive caller.",
        ),
    ] = None,
    pairing_code_file: Annotated[
        Path | None,
        typer.Option(
            "--pairing-code-file",
            help="Protected one-use file; deleted immediately after Agent Port reads it.",
        ),
    ] = None,
    host: Annotated[
        str | None, typer.Option("--host", help="Fallback source host when discovery is blocked.")
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="Fallback source port when discovery is blocked.")
    ] = None,
    discovery_timeout: Annotated[
        float, typer.Option("--discovery-timeout", help="LAN discovery wait in seconds.")
    ] = 8.0,
    output_format: Annotated[str, typer.Option("--format", help="text or json.")] = "text",
    redact_paths: Annotated[bool, typer.Option("--redact-paths")] = False,
) -> None:
    """Receive and verify one .agentpack without changing a harness."""
    document: MigrationWorkspaceDocument | None = None
    try:
        _validate_format(output_format)
        if pairing_code is not None and pairing_code_file is not None:
            raise TransferError("Use only one of --pairing-code or --pairing-code-file.")
        if workspace is not None:
            document = load_migration_document(workspace)
            if pairing_code is not None:
                raise TransferError(
                    "Prepared workspaces accept the pairing code only through their private path."
                )
        if output is None:
            if document is None:
                raise TransferError("--output is required unless --workspace is provided.")
            output = Path(document.archive)
        if document is not None:
            expected_code_file = (
                Path(document.pairing_code_file) if document.pairing_code_file else None
            )
            if expected_code_file is None:
                raise TransferError(
                    "Migration workspace has no active pairing-code path; prepare it again."
                )
            if (
                pairing_code_file is not None
                and pairing_code_file.expanduser().absolute() != expected_code_file
            ):
                raise TransferError(
                    "Pairing-code file does not match the active migration workspace."
                )
            pairing_code_file = expected_code_file
        if pairing_code_file is not None:
            pairing_code = consume_pairing_code_file(
                pairing_code_file,
                private_workspace=(Path(document.workspace) if document is not None else None),
            )
        if pairing_code is None:
            pairing_code = typer.prompt("Pairing code", hide_input=True)
        result = TransferReceiveService().execute(
            output=output,
            pairing_code=pairing_code,
            host=host,
            port=port,
            discovery_timeout=discovery_timeout,
        )
        if document is not None:
            document = update_migration_document(
                document,
                archive=result.output,
                pairing_code_file=None,
                archive_sha256=result.archive_sha256,
                harness=result.harness,
                status=MigrationStateValue.RECEIVED,
                content=result.content,
                last_error=None,
            )
            result = result.model_copy(
                update={
                    "workspace": document.workspace,
                    "state_file": str(Path(document.workspace) / MIGRATION_STATE_NAME),
                }
            )
        if output_format == "json":
            typer.echo(render_json(result, redact_paths=redact_paths))
        else:
            typer.echo(f"Received and verified: {result.output}")
            typer.echo(f"Harness: {result.harness}")
            typer.echo(f"Archive bytes: {result.archived_bytes}")
            typer.echo(f"SHA-256: {result.archive_sha256}")
            _emit_content_counts(result.content)
            typer.echo(f"Next: agent-port restore plan {result.output} --destination HARNESS_HOME")
    except AgentPortError as error:
        if document is not None:
            update_migration_document(
                document,
                pairing_code_file=None,
                status=MigrationStateValue.FAILED,
                last_error=str(error),
            )
        _fail(error)


@restore_app.command("plan")
def restore_plan_command(
    archive: Annotated[Path, typer.Argument(help="Source .agentpack archive.")],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Saved JSON plan; auto-numbered in a migration workspace."
        ),
    ] = None,
    destination: Annotated[
        Path | None,
        typer.Option("--destination", help="Initialized .claude or .codex harness directory."),
    ] = None,
    destination_home: Annotated[
        Path | None,
        typer.Option(
            "--destination-home", help="Destination user's home for portable path mapping."
        ),
    ] = None,
    mappings: Annotated[
        list[str] | None,
        typer.Option("--map", help="Approved SOURCE=DESTINATION project or home-root mapping."),
    ] = None,
    accepted_unmapped: Annotated[list[str] | None, typer.Option("--accept-unmapped")] = None,
    skill_conflicts: Annotated[list[str] | None, typer.Option("--skill-conflict")] = None,
    output_format: Annotated[str, typer.Option("--format", help="text or json.")] = "text",
    redact_paths: Annotated[bool, typer.Option("--redact-paths")] = False,
) -> None:
    """Verify an archive and write the mandatory, fingerprinted dry-run plan."""
    try:
        _validate_format(output_format)
        if output is None:
            output = next_restore_plan_path(archive)
        if output is None:
            raise RestoreError(
                "--output is required unless the archive is in an Agent Port migration workspace."
            )
        result = RestorePlanService().execute(
            archive=archive,
            output=output,
            destination=destination,
            destination_home=destination_home,
            mapping_values=mappings,
            accepted_unmapped=accepted_unmapped,
            skill_conflicts=skill_conflicts,
        )
        info = RestorePlanInfoService().execute(output)
        if output_format == "json":
            typer.echo(render_json(info, redact_paths=redact_paths))
        else:
            _emit_plan_info(info, redact_paths)
            for project in result.projects:
                source = "<redacted>" if redact_paths else project.source
                destination_value = "<redacted>" if redact_paths else project.destination
                state = "exists" if project.exists else "missing"
                typer.echo(
                    f"  - project: {source} -> {destination_value} "
                    f"({project.conversation_count} conversations, {state})"
                )
            typer.echo(
                "Next: arm a plugin handoff, or close the destination harness before direct apply."
            )
        if not result.ready:
            raise typer.Exit(code=1)
    except AgentPortError as error:
        _fail(error)


@restore_app.command("plan-info")
def restore_plan_info_command(
    plan: Annotated[Path, typer.Argument(help="Saved restore plan JSON.")],
    output_format: Annotated[str, typer.Option("--format", help="text or json.")] = "text",
    redact_paths: Annotated[bool, typer.Option("--redact-paths")] = False,
) -> None:
    """Reload and compactly summarize the canonical saved restore plan."""
    try:
        _validate_format(output_format)
        result = RestorePlanInfoService().execute(plan)
        if output_format == "json":
            typer.echo(render_json(result, redact_paths=redact_paths))
        else:
            _emit_plan_info(result, redact_paths)
        if not result.ready:
            raise typer.Exit(code=1)
    except AgentPortError as error:
        _fail(error)


@restore_app.command("handoff")
def restore_handoff_command(
    plan: Annotated[Path, typer.Argument(help="Saved restore plan JSON.")],
    confirm_quit_to_apply: Annotated[
        bool,
        typer.Option(
            "--confirm-quit-to-apply",
            help="Authorize apply only after Agent Port verifies the harness has closed.",
        ),
    ] = False,
    timeout: Annotated[
        int, typer.Option("--timeout", help="Handoff lifetime in seconds (30-86400).")
    ] = 1800,
    output_format: Annotated[str, typer.Option("--format", help="text or json.")] = "text",
    redact_paths: Annotated[bool, typer.Option("--redact-paths")] = False,
) -> None:
    """Arm a temporary worker which applies only after the destination harness closes."""
    try:
        _validate_format(output_format)
        result = RestoreHandoffService().execute(plan, confirm_quit_to_apply, timeout)
        if output_format == "json":
            typer.echo(render_json(result, redact_paths=redact_paths))
        else:
            typer.echo(f"Restore handoff armed: {result.plan_id}")
            typer.echo(f"State: {result.state.value}")
            _emit_content_counts(result.content)
            typer.echo(
                "Quit every destination harness process. "
                "Agent Port will verify closure before apply."
            )
            typer.echo("Wait for the completion notification before reopening the harness.")
            if not redact_paths:
                typer.echo(f"Status: {result.status_path}")
    except AgentPortError as error:
        _fail(error)


@restore_app.command("apply")
def restore_apply_command(
    plan: Annotated[Path, typer.Argument(help="Saved restore plan JSON.")],
    confirm_harness_closed: Annotated[
        bool, typer.Option("--confirm-harness-closed", help="Confirm the harness is closed.")
    ] = False,
    register_projects: Annotated[
        bool, typer.Option("--register-projects", help="Open existing mapped Codex projects.")
    ] = False,
) -> None:
    """Apply an unchanged, ready restore plan and verify the result."""
    try:
        result = RestoreApplyService().execute(
            plan, confirm_harness_closed, register_projects=register_projects
        )
        typer.echo(f"Restore verified: {result.plan_id}")
        typer.echo(f"Destination backup: {result.backup}")
        typer.echo(f"Rollback directory: {result.run_directory}")
        typer.echo(f"Applied: {result.applied}; skipped: {result.skipped}")
        typer.echo("Initial closed-harness verification: passed")
        if result.restart_required:
            typer.echo("Restart or reload the harness to activate restored skills.")
        for warning in result.registration_warnings:
            typer.echo(f"Warning: {warning}", err=True)
    except AgentPortError as error:
        _fail(error)


@restore_app.command("verify")
def restore_verify_command(
    run_directory: Annotated[Path, typer.Argument(help="Completed or pending restore run.")],
    legacy_plan: Annotated[
        Path | None,
        typer.Option("--plan", help="Saved plan for a legacy run without plan.json."),
    ] = None,
    output_format: Annotated[str, typer.Option("--format", help="text or json.")] = "text",
    redact_paths: Annotated[bool, typer.Option("--redact-paths")] = False,
) -> None:
    """Verify recorded restore safety and the current migrated data without changing it."""
    try:
        _validate_format(output_format)
        result = RestoreVerificationService().execute(run_directory, legacy_plan)
        if output_format == "json":
            typer.echo(render_json(result, redact_paths=redact_paths))
        else:
            typer.echo(f"Migration status: {result.status.value}")
            typer.echo(
                f"Restore completed safely: {'yes' if result.restore_completed_safely else 'no'}"
            )
            typer.echo(
                f"Current migrated data intact: {'yes' if result.current_data_intact else 'no'}"
            )
            typer.echo(f"Destination valid: {'yes' if result.destination_valid else 'no'}")
            if result.initial_verification_status is not None:
                typer.echo(
                    "Initial closed-harness verification: "
                    f"{result.initial_verification_status.value}"
                )
            if result.initial_verification_path is not None and not redact_paths:
                typer.echo(f"Initial verification evidence: {result.initial_verification_path}")
            typer.echo(
                f"Operations: {result.counts.applied_operations} applied; "
                f"{result.counts.skipped_operations} skipped"
            )
            typer.echo(
                f"Top-level conversations: {result.counts.verified_conversations}/"
                f"{result.counts.expected_conversations} verified"
            )
            typer.echo(
                "Associated subagent transcripts: "
                f"{result.counts.verified_subagent_transcripts}/"
                f"{result.counts.expected_subagent_transcripts} verified"
            )
            typer.echo(
                f"Total transcript files: {result.counts.verified_transcript_files}/"
                f"{result.counts.expected_transcript_files} verified"
            )
            typer.echo(
                f"Active projects: {result.counts.verified_projects}/"
                f"{result.counts.expected_projects} verified"
            )
            typer.echo(
                f"Skills: {result.counts.verified_skills}/{result.counts.expected_skills} verified"
            )
            typer.echo(
                "Attachments: "
                f"{result.counts.verified_attachments}/"
                f"{result.counts.expected_attachments} verified"
            )
            for diagnostic in result.diagnostics:
                typer.echo(f"  - {diagnostic.severity.value}: {diagnostic.message}")
        if result.status.value == "failed":
            raise typer.Exit(code=1)
        if not result.valid:
            raise typer.Exit(code=2)
    except AgentPortError as error:
        _fail(error)


@restore_app.command("rollback")
def restore_rollback_command(
    run_directory: Annotated[Path, typer.Argument(help="Completed restore run directory.")],
    confirm_harness_closed: Annotated[
        bool, typer.Option("--confirm-harness-closed", help="Confirm the harness is closed.")
    ] = False,
) -> None:
    """Undo a completed restore if its post-restore state is unchanged."""
    try:
        result = RollbackService().execute(run_directory, confirm_harness_closed)
        if result.already_rolled_back:
            typer.echo("Restore run was already rolled back.")
        else:
            typer.echo(f"Rollback completed; restored {result.restored} path(s).")
    except AgentPortError as error:
        _fail(error)


@app.command("doctor")
def doctor_command(
    destination: Annotated[Path, typer.Argument(help="Harness home to validate.")],
    harness: Annotated[str, typer.Option("--harness")] = "auto",
    output_format: Annotated[str, typer.Option("--format", help="text or json.")] = "text",
    redact_paths: Annotated[bool, typer.Option("--redact-paths")] = False,
) -> None:
    """Validate a harness installation or completed restore without changing it."""
    try:
        result = DoctorService().execute(destination, harness)
        _validate_format(output_format)
        if output_format == "json":
            typer.echo(render_json(result, redact_paths=redact_paths))
        else:
            typer.echo(f"Valid: {'yes' if result.valid else 'no'}")
            for check in result.checks:
                typer.echo(f"  - {check}")
            for diagnostic in result.diagnostics:
                typer.echo(f"  - {diagnostic.severity.value}: {diagnostic.message}")
        if not result.valid:
            raise typer.Exit(code=1)
    except AgentPortError as error:
        _fail(error)


def _emit_report(
    result: InspectionReport | ArchiveInspectionReport,
    output_format: str,
    redact_paths: bool,
) -> None:
    _validate_format(output_format)
    if output_format == "json":
        typer.echo(render_json(result, redact_paths=redact_paths))
    elif isinstance(result, ArchiveInspectionReport):
        typer.echo(render_archive(result, redact_paths=redact_paths))
    else:
        typer.echo(render_inspection(result, redact_paths=redact_paths))


def _emit_content_counts(counts: CountSummary) -> None:
    conversations = counts.conversations if counts.conversations is not None else "unknown"
    typer.echo(f"Top-level conversations: {conversations}")
    typer.echo(f"Associated subagent transcripts: {counts.subagent_transcripts}")
    typer.echo(f"Total transcript files: {counts.transcript_files}")
    typer.echo(f"Active projects: {counts.projects}")
    typer.echo(f"User-owned skills: {counts.skills}")
    typer.echo(f"Attachments: {counts.attachments}")


def _emit_plan_info(result: RestorePlanInfo, redact_paths: bool) -> None:
    typer.echo(f"Saved restore plan: {'<redacted>' if redact_paths else result.plan_path}")
    typer.echo(f"Plan ID: {result.plan_id}")
    typer.echo(f"Status: {result.status}")
    _emit_content_counts(result.content)
    operation_summary = (
        ", ".join(f"{name}={count}" for name, count in result.operation_counts.items()) or "none"
    )
    typer.echo(f"Operations: {operation_summary}")
    for mapping in result.mappings:
        source = "<redacted>" if redact_paths else mapping.source
        destination = "<redacted>" if redact_paths else mapping.destination
        typer.echo(f"  - mapping: {source} -> {destination}")
    for mapping in result.suggested_mappings:
        source = "<redacted>" if redact_paths else mapping.source
        destination = "<redacted>" if redact_paths else mapping.destination
        typer.echo(f"  - suggested mapping: {source} -> {destination}")
    for conflict in [*result.blockers, *result.notices]:
        label = "BLOCKER" if conflict.blocking else "notice"
        identity = "<redacted>" if redact_paths else conflict.identity
        typer.echo(f"  - {label}: {conflict.message} ({identity})")


def _validate_format(value: str) -> None:
    if value not in {"text", "json"}:
        raise typer.BadParameter("--format must be text or json")


def _fail(error: Exception) -> None:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1)
