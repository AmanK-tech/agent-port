from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from agent_port.application.migration import update_migration_for_path
from agent_port.application.restore import (
    RestoreApplyService,
    RestorePlanInfoService,
    RestorePreflightService,
)
from agent_port.domain.errors import RestoreError
from agent_port.domain.models import (
    HandoffState,
    HarnessName,
    HarnessProcessRecord,
    MigrationStateValue,
    RestoreHandoffStatus,
    RestoreVerificationResult,
    RestoreVerificationState,
    RollbackJournal,
)
from agent_port.infrastructure.archive.common import canonical_json
from agent_port.infrastructure.processes import HarnessProcessInspector

Clock = Callable[[], datetime]
Launcher = Callable[[list[str]], None]


class RestoreHandoffService:
    def __init__(
        self,
        inspector: HarnessProcessInspector | None = None,
        clock: Clock | None = None,
        launcher: Launcher | None = None,
    ) -> None:
        self._inspector = inspector or HarnessProcessInspector()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._launcher = launcher or _launch_worker

    def execute(
        self,
        plan_path: Path,
        confirm_quit_to_apply: bool,
        timeout_seconds: int = 1800,
    ) -> RestoreHandoffStatus:
        if not confirm_quit_to_apply:
            raise RestoreError("Handoff requires --confirm-quit-to-apply.")
        if timeout_seconds < 30 or timeout_seconds > 86400:
            raise RestoreError("Handoff timeout must be between 30 and 86400 seconds.")
        resolved_plan = plan_path.expanduser().resolve()
        plan = RestorePreflightService().execute(resolved_plan)
        observed = self._inspector.active(plan.archive.harness)
        if not observed:
            raise RestoreError(
                "Cannot arm handoff because no active destination harness process was detected."
            )
        run_directory = Path(plan.run_directory)
        content = RestorePlanInfoService().execute(resolved_plan).content
        status_path = handoff_status_path(run_directory)
        if status_path.exists():
            existing = load_handoff_status(status_path)
            raise RestoreError(
                f"Restore handoff already exists in state {existing.state.value}: {status_path}"
            )
        now = self._clock()
        status = RestoreHandoffStatus(
            plan_id=plan.plan_id,
            plan_path=str(resolved_plan),
            run_directory=str(run_directory),
            status_path=str(status_path),
            harness=plan.archive.harness,
            state=HandoffState.ARMED,
            armed_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=timeout_seconds),
            observed_processes=observed,
            content=content,
        )
        write_handoff_status(status_path, status)
        update_migration_for_path(
            Path(plan.archive.path),
            current_plan=str(resolved_plan),
            handoff_status=str(status_path),
            run_directory=str(run_directory),
            status=MigrationStateValue.HANDOFF_ARMED,
            content=content,
            last_error=None,
        )
        try:
            self._launcher(
                [
                    sys.executable,
                    "-m",
                    "agent_port.infrastructure.handoff_worker",
                    str(status_path),
                ]
            )
        except OSError as error:
            failed = status.model_copy(
                update={
                    "state": HandoffState.FAILED,
                    "updated_at": self._clock(),
                    "error": f"Could not start restore handoff worker: {error}",
                }
            )
            write_handoff_status(status_path, failed)
            update_migration_for_path(
                resolved_plan,
                status=MigrationStateValue.FAILED,
                last_error=failed.error,
            )
            raise RestoreError(failed.error or "Could not start restore handoff worker.") from error
        return status


class RestoreHandoffWorker:
    def __init__(
        self,
        inspector: HarnessProcessInspector | None = None,
        clock: Clock | None = None,
        sleep: Callable[[float], None] | None = None,
        apply_service: RestoreApplyService | None = None,
        notifier: Callable[[str, str], None] | None = None,
        poll_seconds: float = 0.5,
    ) -> None:
        self._inspector = inspector or HarnessProcessInspector()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        self._apply = apply_service or RestoreApplyService()
        self._notifier = notifier or notify_user
        self._poll_seconds = poll_seconds

    def execute(self, status_path: Path) -> RestoreHandoffStatus:
        status_path = status_path.expanduser().resolve()
        status = load_handoff_status(status_path)
        if status.state not in {HandoffState.ARMED, HandoffState.WAITING}:
            return status
        closed_observations = 0
        while self._clock() < status.expires_at:
            active = self._inspector.active(status.harness)
            status = self._update(
                status_path,
                status,
                state=HandoffState.WAITING,
                observed_processes=active,
                error=None,
            )
            if active:
                closed_observations = 0
                self._sleep(self._poll_seconds)
                continue
            closed_observations += 1
            if closed_observations < 2:
                self._sleep(self._poll_seconds)
                continue
            status = self._update(
                status_path,
                status,
                state=HandoffState.APPLYING,
                observed_processes=[],
                error=None,
            )
            monitor = _ClosureMonitor(self._inspector, status.harness, self._poll_seconds)
            monitor.start()
            initial_path = Path(status.run_directory) / "initial-verification.json"
            initial: RestoreVerificationResult | None = None
            try:
                self._apply.execute(
                    Path(status.plan_path),
                    confirm_harness_closed=True,
                    closure_guard=monitor.assert_closed,
                )
                monitor.assert_closed()
                initial = RestoreVerificationResult.model_validate_json(initial_path.read_bytes())
                if not initial.success_gate_passed:
                    raise RestoreError(
                        "Initial verification did not pass the complete success gate."
                    )
            except Exception as error:
                rolled_back = _run_was_rolled_back(Path(status.run_directory))
                if initial is None and initial_path.is_file():
                    try:
                        initial = RestoreVerificationResult.model_validate_json(
                            initial_path.read_bytes()
                        )
                    except (OSError, ValidationError):
                        initial = None
                failed = self._update(
                    status_path,
                    status,
                    state=HandoffState.FAILED,
                    observed_processes=self._inspector.active(status.harness),
                    error=str(error),
                    rolled_back=rolled_back,
                    initial_verification_status=(initial.status if initial else None),
                    initial_verification_path=(str(initial_path) if initial else None),
                )
                self._send_notification(
                    "Restore failed", "Agent Port kept or restored the previous state."
                )
                update_migration_for_path(
                    Path(status.plan_path),
                    status=MigrationStateValue.FAILED,
                    last_error=str(error),
                )
                return failed
            finally:
                monitor.stop()
            if initial is None:
                raise RestoreError("Initial verification evidence is missing after restore.")
            succeeded = self._update(
                status_path,
                status,
                state=HandoffState.SUCCEEDED,
                observed_processes=[],
                error=None,
                initial_verification_status=initial.status,
                initial_verification_path=str(initial_path),
            )
            self._send_notification(
                "Restore verified", "Restore verified; safe to reopen the destination harness."
            )
            update_migration_for_path(
                Path(status.plan_path),
                status=MigrationStateValue.VERIFIED,
                verification_result=str(initial_path),
                last_error=None,
            )
            return succeeded
        expired = self._update(
            status_path,
            status,
            state=HandoffState.EXPIRED,
            observed_processes=self._inspector.active(status.harness),
            error="Destination harness did not remain closed before the handoff expired.",
        )
        self._send_notification("Restore handoff expired", "No restore was applied.")
        update_migration_for_path(
            Path(status.plan_path),
            status=MigrationStateValue.FAILED,
            last_error=expired.error,
        )
        return expired

    def _send_notification(self, title: str, message: str) -> None:
        try:
            self._notifier(title, message)
        except Exception:
            return

    def _update(
        self,
        status_path: Path,
        status: RestoreHandoffStatus,
        *,
        state: HandoffState,
        observed_processes: list[HarnessProcessRecord],
        error: str | None,
        initial_verification_status: RestoreVerificationState | None = None,
        initial_verification_path: str | None = None,
        rolled_back: bool | None = None,
    ) -> RestoreHandoffStatus:
        updated = status.model_copy(
            update={
                "state": state,
                "updated_at": self._clock(),
                "observed_processes": observed_processes,
                "error": error,
                "initial_verification_status": (
                    initial_verification_status
                    if initial_verification_status is not None
                    else status.initial_verification_status
                ),
                "initial_verification_path": (
                    initial_verification_path
                    if initial_verification_path is not None
                    else status.initial_verification_path
                ),
                "rolled_back": status.rolled_back if rolled_back is None else rolled_back,
            }
        )
        write_handoff_status(status_path, updated)
        return updated


class _ClosureMonitor:
    def __init__(
        self,
        inspector: HarnessProcessInspector,
        harness: HarnessName,
        poll_seconds: float,
    ) -> None:
        self._inspector = inspector
        self._harness = harness
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._reopened = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._poll_seconds * 4))

    def assert_closed(self) -> None:
        if self._reopened.is_set() or self._inspector.active(self._harness):
            self._reopened.set()
            raise RestoreError("Destination harness reopened during restore handoff.")

    def _watch(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            if self._inspector.active(self._harness):
                self._reopened.set()
                return


def handoff_status_path(run_directory: Path) -> Path:
    return run_directory.parent / ".handoffs" / f"{run_directory.name}.json"


def _run_was_rolled_back(run_directory: Path) -> bool:
    journal_path = run_directory / "journal.json"
    if not journal_path.is_file():
        return False
    try:
        return RollbackJournal.model_validate_json(journal_path.read_bytes()).rolled_back
    except (OSError, ValidationError):
        return False


def load_handoff_status(path: Path) -> RestoreHandoffStatus:
    try:
        return RestoreHandoffStatus.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise RestoreError(f"Cannot load restore handoff status {path}: {error}") from error


def write_handoff_status(path: Path, status: RestoreHandoffStatus) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical_json(status.model_dump(mode="json", exclude_none=True)))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _launch_worker(command: list[str]) -> None:
    if os.name == "nt":
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
            creationflags=0x00000200 | 0x00000008,
        )
    else:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )


def notify_user(title: str, message: str) -> None:
    try:
        system = platform.system()
        if system == "Darwin" and shutil.which("osascript"):
            script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
            subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                timeout=5,
            )
        elif system == "Linux" and shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", title, message],
                check=False,
                capture_output=True,
                timeout=5,
            )
    except (OSError, subprocess.SubprocessError):
        return
