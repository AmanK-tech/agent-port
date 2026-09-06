from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from agent_port.domain.errors import ArchiveError
from agent_port.domain.models import ArchiveEntry


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def stage_entries(staging: Path, exclude: frozenset[str] = frozenset()) -> list[Path]:
    entries: list[Path] = []
    for current, directories, files in os.walk(staging, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            item = current_path / name
            if item.is_symlink():
                relative = item.relative_to(staging).as_posix()
                if relative not in exclude:
                    entries.append(item)
                directories.remove(name)
        for name in files:
            item = current_path / name
            relative = item.relative_to(staging).as_posix()
            if relative not in exclude:
                entries.append(item)
    return sorted(entries, key=lambda path: path.relative_to(staging).as_posix())


def describe_path(path: Path) -> ArchiveEntry:
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if path.is_symlink():
        data = os.readlink(path).encode("utf-8", errors="surrogateescape")
        entry_type: Literal["file", "symlink"] = "symlink"
    elif path.is_file():
        data = path.read_bytes()
        entry_type = "file"
    else:
        raise ArchiveError(f"Unsupported archive entry type: {path}")
    return ArchiveEntry(
        sha256=f"sha256:{hashlib.sha256(data).hexdigest()}",
        size=len(data),
        type=entry_type,
        mode=mode,
    )


def validate_member_name(name: str) -> None:
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or "//" in name
        or re.match(r"^[A-Za-z]:", name)
    ):
        raise ArchiveError(f"Unsafe archive member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArchiveError(f"Unsafe archive member name: {name!r}")


def resolve_symlink_member(name: str, target: str) -> str:
    if not target or "\\" in target or target.startswith("/") or re.match(r"^[A-Za-z]:", target):
        raise ArchiveError(f"Unsafe symlink target in archive member {name}: {target!r}")
    parts = list(PurePosixPath(name).parent.parts)
    for part in PurePosixPath(target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ArchiveError(f"Symlink target escapes archive root: {name}")
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise ArchiveError(f"Invalid empty symlink target: {name}")
    return PurePosixPath(*parts).as_posix()
