from __future__ import annotations

import os
from pathlib import Path

import psutil

from agent_port.domain.models import HarnessName, HarnessProcessRecord


class HarnessProcessInspector:
    """Find active harness processes owned by the current user."""

    def active(self, harness: HarnessName) -> list[HarnessProcessRecord]:
        current = psutil.Process()
        try:
            username = current.username()
        except (psutil.AccessDenied, psutil.Error):
            username = None
        records: list[HarnessProcessRecord] = []
        for process in psutil.process_iter(
            attrs=("pid", "name", "exe", "cmdline", "username", "create_time")
        ):
            try:
                info = process.info
                if info["pid"] == os.getpid():
                    continue
                owner = info.get("username")
                if username is not None and owner != username:
                    continue
                name = str(info.get("name") or "")
                executable = str(info.get("exe") or "")
                command = [str(item) for item in (info.get("cmdline") or [])]
                if not _matches_harness(harness, name, executable, command):
                    continue
                created_at = float(info.get("create_time") or process.create_time())
                records.append(
                    HarnessProcessRecord(
                        pid=int(info["pid"]),
                        created_at=created_at,
                        name=name or Path(executable).name or harness.value,
                    )
                )
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, ValueError):
                continue
        return sorted(records, key=lambda item: item.pid)


def _matches_harness(harness: HarnessName, name: str, executable: str, command: list[str]) -> bool:
    normalized_name = name.casefold()
    executable_name = Path(executable).name.casefold() if executable else ""
    first_argument = Path(command[0]).name.casefold() if command else ""
    command_text = " ".join(command).casefold()
    candidates = {normalized_name, executable_name, first_argument}
    if harness is HarnessName.CLAUDE_CODE:
        if candidates.intersection({"claude", "claude.exe", "claude-code", "claude-code.exe"}):
            return True
        return "@anthropic-ai/claude-code" in command_text
    if candidates.intersection({"codex", "codex.exe", "codex app", "codex.app"}):
        return True
    return "/codex.app/contents/macos/codex" in executable.casefold()
