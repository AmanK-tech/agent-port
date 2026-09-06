from __future__ import annotations

import ntpath
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from agent_port.domain.errors import RestoreError
from agent_port.domain.models import PathMapping

_WINDOWS = re.compile(r"^(?:\\\\\?\\)?[A-Za-z]:[\\/]")


def parse_mapping(value: str, source_home: str | None, destination_home: Path) -> PathMapping:
    if "=" not in value:
        raise RestoreError(f"Invalid path mapping {value!r}; expected SOURCE=DESTINATION.")
    source, destination = value.split("=", 1)
    source = _expand_tilde(source.strip(), source_home)
    destination = _expand_tilde(destination.strip(), str(destination_home))
    if not source or not destination:
        raise RestoreError("Path mappings cannot contain an empty side.")
    if not is_absolute(source) or not is_absolute(destination):
        raise RestoreError(f"Path mappings must be absolute after expansion: {value!r}")
    return PathMapping(source=normalize(source), destination=normalize(destination))


def normalize(value: str) -> str:
    if _WINDOWS.match(value):
        cleaned = value.removeprefix("\\\\?\\").replace("/", "\\")
        normalized = ntpath.normpath(cleaned)
        drive, tail = ntpath.splitdrive(normalized)
        return drive.upper() + tail
    return posixpath.normpath(value.replace("\\", "/"))


def is_absolute(value: str) -> bool:
    return bool(_WINDOWS.match(value)) or value.startswith("/")


@dataclass(frozen=True)
class MappingResult:
    value: str
    mapped: bool


class PathMapper:
    def __init__(self, mappings: list[PathMapping]) -> None:
        normalized: dict[tuple[str, ...], tuple[str, str]] = {}
        for mapping in mappings:
            source = normalize(mapping.source)
            destination = normalize(mapping.destination)
            key = _parts(source)
            existing = normalized.get(key)
            if existing is not None and _parts(existing[1]) != _parts(destination):
                raise RestoreError(f"Conflicting mappings for source path: {source}")
            normalized[key] = (source, destination)
        self.mappings = [
            PathMapping(source=source, destination=destination)
            for source, destination in sorted(
                normalized.values(), key=lambda item: len(_parts(item[0])), reverse=True
            )
        ]

    def apply(self, value: str) -> MappingResult:
        candidate = normalize(value)
        for mapping in self.mappings:
            suffix = _relative_suffix(candidate, mapping.source)
            if suffix is not None:
                separator = "\\" if _WINDOWS.match(mapping.destination) else "/"
                mapped = mapping.destination
                if suffix:
                    mapped = mapped.rstrip("/\\") + separator + suffix.replace("/", separator)
                return MappingResult(normalize(mapped), True)
        return MappingResult(candidate, False)


def fingerprint_path(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    if path.is_symlink():
        digest.update(b"symlink\0" + path.readlink().as_posix().encode())
    elif path.is_file():
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    elif path.is_dir():
        digest.update(b"directory\0")
        for child in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
            relative = child.relative_to(path).as_posix()
            digest.update(relative.encode() + b"\0")
            if child.is_symlink():
                digest.update(b"symlink\0" + child.readlink().as_posix().encode())
            elif child.is_file():
                with child.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
    else:
        digest.update(b"missing\0")
    return f"sha256:{digest.hexdigest()}"


def _expand_tilde(value: str, home: str | None) -> str:
    if value == "~":
        if home is None:
            raise RestoreError("Cannot expand ~ because the corresponding home is unknown.")
        return home
    if value.startswith(("~/", "~\\")):
        if home is None:
            raise RestoreError("Cannot expand ~ because the corresponding home is unknown.")
        return home.rstrip("/\\") + "/" + value[2:]
    return value


def _parts(value: str) -> tuple[str, ...]:
    if _WINDOWS.match(value):
        drive, tail = ntpath.splitdrive(value)
        return (drive.casefold(), *(part.casefold() for part in tail.split("\\") if part))
    return tuple(part for part in value.split("/") if part)


def _relative_suffix(value: str, prefix: str) -> str | None:
    value_parts = _parts(value)
    prefix_parts = _parts(prefix)
    if value_parts[: len(prefix_parts)] != prefix_parts:
        return None
    raw_parts = _raw_parts(value)
    return "/".join(raw_parts[len(prefix_parts) :])


def _raw_parts(value: str) -> tuple[str, ...]:
    if _WINDOWS.match(value):
        drive, tail = ntpath.splitdrive(value)
        return (drive, *(part for part in tail.split("\\") if part))
    return tuple(part for part in value.split("/") if part)
