from __future__ import annotations

import contextlib
import os
import shutil
import stat
from pathlib import Path

from agent_port.domain.errors import BackupError


def stable_copy(source: Path, destination: Path) -> None:
    """Copy a regular file and fail if it changed while being read."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        before = source.stat()
        shutil.copy2(source, destination)
        after = source.stat()
        signature_before = (before.st_ino, before.st_size, before.st_mtime_ns)
        signature_after = (after.st_ino, after.st_size, after.st_mtime_ns)
        if signature_before == signature_after:
            return
    raise BackupError(f"File changed repeatedly during backup: {source}")


def copy_tree_safely(
    source: Path,
    destination: Path,
    excluded_names: frozenset[str] = frozenset(),
) -> None:
    """Copy a tree without following symlinks outside its root."""
    source_root = source.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        output_dir = destination / relative
        output_dir.mkdir(parents=True, exist_ok=True)

        for name in list(directories):
            if _excluded(name, excluded_names):
                directories.remove(name)
                continue
            item = current_path / name
            if item.is_symlink():
                _copy_validated_symlink(item, output_dir / name, source_root)
                directories.remove(name)

        for name in files:
            if _excluded(name, excluded_names):
                continue
            item = current_path / name
            output = output_dir / name
            if item.is_symlink():
                _copy_validated_symlink(item, output, source_root)
            elif item.is_file():
                stable_copy(item, output)


def _excluded(name: str, excluded_names: frozenset[str]) -> bool:
    return name.startswith("._") or name.casefold() in excluded_names


def _copy_validated_symlink(source: Path, destination: Path, root: Path) -> None:
    try:
        resolved = source.resolve(strict=True)
    except OSError as error:
        raise BackupError(f"Broken symlink cannot be backed up: {source}: {error}") from error
    if not resolved.is_relative_to(root):
        raise BackupError(f"Symlink escapes the selected root: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(os.readlink(source), destination)
    mode = stat.S_IMODE(source.lstat().st_mode)
    if hasattr(os, "lchmod"):
        with contextlib.suppress(OSError):
            os.lchmod(destination, mode)
