from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_SKIP_DIRECTORIES = frozenset(
    {
        ".agent-port-runs",
        ".cache",
        ".claude",
        ".codex",
        ".cargo",
        ".config",
        ".local",
        ".npm",
        ".nvm",
        ".pyenv",
        ".rustup",
        ".ssh",
        ".venv",
        "Library",
        "node_modules",
        "__pycache__",
    }
)
_MAX_DISCOVERY_ENTRIES = 20_000
_GIT_TIMEOUT_SECONDS = 3


@dataclass(frozen=True)
class RepositoryCandidate:
    path: Path
    fingerprint: str


def repository_fingerprint(path: Path) -> str | None:
    """Return a secret-free identity for a repository's origin remote."""
    if not path.is_dir() or not (path / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    remote = completed.stdout.strip()
    if completed.returncode != 0 or not remote:
        return None
    canonical = _canonical_remote(remote)
    if not canonical:
        return None
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def discover_repositories(root: Path) -> list[RepositoryCandidate]:
    """Find origin-backed Git worktrees below a destination home without reading their files."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        return []
    candidates: list[RepositoryCandidate] = []
    pending: list[tuple[Path, int]] = [(root, 0)]
    visited = 0
    while pending and visited < _MAX_DISCOVERY_ENTRIES:
        current, depth = pending.pop()
        visited += 1
        if (current / ".git").exists():
            fingerprint = repository_fingerprint(current)
            if fingerprint is not None:
                candidates.append(RepositoryCandidate(current, fingerprint))
            continue
        if depth >= 8:
            continue
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in sorted(children, reverse=True):
            if child.is_dir() and not child.is_symlink() and child.name not in _SKIP_DIRECTORIES:
                pending.append((child, depth + 1))
    return sorted(candidates, key=lambda item: item.path.as_posix())


def _canonical_remote(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if "://" in value:
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        default_ports = {"https": 443, "http": 80, "ssh": 22, "git": 9418}
        remote_path = parsed.path.strip("/").removesuffix(".git")
        if parsed.scheme not in default_ports or not host or not remote_path:
            return None
        authority = host.casefold()
        if port is not None and port != default_ports[parsed.scheme]:
            authority += f":{port}"
        return f"{authority}/{remote_path}"
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return None
    match = re.fullmatch(r"(?:[^/@:\s]+@)?([^/:\s]+):(.+)", value)
    if match:
        host, remote_path = match.groups()
        remote_path = remote_path.strip("/").removesuffix(".git")
        return f"{host.casefold()}/{remote_path}" if remote_path else None
    # Local filesystem origins are machine-specific and cannot identify a clone
    # on another computer. Never resolve them relative to Agent Port's own cwd.
    return None
