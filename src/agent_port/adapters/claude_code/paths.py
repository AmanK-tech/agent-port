from __future__ import annotations

from pathlib import Path

from agent_port.infrastructure.filesystem.jsonl import inspect_jsonl


def encode_project_path(value: str) -> str:
    return value.replace("\\", "-").replace("/", "-").replace(":", "-")


def project_container_paths(projects_root: Path) -> dict[str, str]:
    """Recover each native container's root from top-level structural metadata."""
    candidates: dict[str, set[str]] = {}
    for transcript in sorted(projects_root.glob("*/*.jsonl")):
        if transcript.is_file():
            candidates.setdefault(transcript.parent.name, set()).update(
                inspect_jsonl(transcript, "claude-code").project_paths
            )
    result: dict[str, str] = {}
    for container, paths in candidates.items():
        matches = {path for path in paths if encode_project_path(path) == container}
        # Working directories may change within a session or between sessions in
        # one container. Prefer the path that actually names the native container.
        selected = matches or paths
        if len(selected) == 1:
            result[container] = next(iter(selected))
    return result
