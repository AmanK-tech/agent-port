from __future__ import annotations

import os
import shutil
import stat
import uuid
import zipfile
from pathlib import Path

from agent_port.domain.errors import ArchiveError, BackupError
from agent_port.domain.models import ChecksumsDocument
from agent_port.infrastructure.archive.common import (
    canonical_json,
    describe_path,
    stage_entries,
    validate_member_name,
)


class AgentPackWriter:
    def write(self, staging: Path, output: Path) -> tuple[int, int]:
        if output.exists():
            raise BackupError(f"Refusing to overwrite existing output: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)

        entries = stage_entries(staging, frozenset({"checksums.json"}))
        checksums = {path.relative_to(staging).as_posix(): describe_path(path) for path in entries}
        (staging / "checksums.json").write_bytes(
            canonical_json(
                ChecksumsDocument(entries=checksums).model_dump(mode="json", exclude_none=True)
            )
        )
        all_entries = stage_entries(staging)
        partial = output.parent / f".{output.name}.{uuid.uuid4().hex}.partial"
        try:
            with zipfile.ZipFile(
                partial, mode="x", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as archive:
                for path in all_entries:
                    name = path.relative_to(staging).as_posix()
                    validate_member_name(name)
                    self._write_entry(archive, path, name)
            from agent_port.infrastructure.archive.reader import AgentPackReader

            AgentPackReader().inspect(partial)
            try:
                os.link(partial, output)
            except FileExistsError as error:
                raise BackupError(f"Refusing to overwrite existing output: {output}") from error
            partial.unlink()
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        total_bytes = sum(path.lstat().st_size for path in all_entries)
        return len(all_entries), total_bytes

    @staticmethod
    def _write_entry(archive: zipfile.ZipFile, path: Path, name: str) -> None:
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.compress_type = zipfile.ZIP_DEFLATED
        if path.is_symlink():
            info.external_attr = (stat.S_IFLNK | mode) << 16
            archive.writestr(info, os.readlink(path).encode("utf-8", errors="surrogateescape"))
            return
        if not path.is_file():
            raise ArchiveError(f"Unsupported archive entry type: {path}")
        info.external_attr = (stat.S_IFREG | mode) << 16
        info.file_size = metadata.st_size
        with path.open("rb") as source, archive.open(info, "w") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
