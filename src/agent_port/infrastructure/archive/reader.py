from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path

from pydantic import ValidationError

from agent_port.domain.errors import ArchiveError
from agent_port.domain.models import (
    ArchiveInspectionReport,
    ArchiveManifest,
    ChecksumsDocument,
    Diagnostic,
    HarnessName,
    PayloadsDocument,
    Severity,
    TimestampsDocument,
)
from agent_port.infrastructure.archive.common import resolve_symlink_member, validate_member_name


class AgentPackReader:
    def inspect(self, source: Path) -> ArchiveInspectionReport:
        if not source.is_file():
            raise ArchiveError(f"Archive does not exist: {source}")
        try:
            with zipfile.ZipFile(source, "r") as archive:
                members = archive.infolist()
                names = [member.filename for member in members if not member.is_dir()]
                if len(names) != len(set(names)):
                    raise ArchiveError("Archive contains duplicate member names.")
                for member in members:
                    validate_member_name(member.filename.rstrip("/"))
                    if member.flag_bits & 0x1:
                        raise ArchiveError(
                            f"Encrypted archive members are unsupported: {member.filename}"
                        )
                required = {"manifest.json", "projects.json", "skills.json", "checksums.json"}
                missing = required.difference(names)
                if missing:
                    raise ArchiveError(
                        f"Archive is missing required entries: {', '.join(sorted(missing))}"
                    )
                manifest_value = json.loads(archive.read("manifest.json"))
                manifest = ArchiveManifest.model_validate(manifest_value)
                raw_counts = manifest_value.get("counts", {})
                if (
                    manifest.harness is HarnessName.CLAUDE_CODE
                    and isinstance(raw_counts, dict)
                    and "subagent_transcripts" not in raw_counts
                ):
                    conversations = manifest.counts.conversations or 0
                    manifest = manifest.model_copy(
                        update={
                            "counts": manifest.counts.model_copy(
                                update={
                                    "subagent_transcripts": max(
                                        manifest.counts.transcript_files - conversations, 0
                                    )
                                }
                            )
                        }
                    )
                if manifest.format_version == 2:
                    required.add("payloads.json")
                    if "payloads.json" not in names:
                        raise ArchiveError("Format v2 archive is missing payloads.json.")
                checksums = ChecksumsDocument.model_validate_json(archive.read("checksums.json"))
                self._validate_inventory(archive.read("projects.json"), "projects")
                self._validate_inventory(archive.read("skills.json"), "skills")
                payloads = None
                if manifest.format_version == 2:
                    payloads = PayloadsDocument.model_validate_json(archive.read("payloads.json"))
                timestamps = (
                    TimestampsDocument.model_validate_json(archive.read("timestamps.json"))
                    if "timestamps.json" in names
                    else None
                )
                expected_names = set(names).difference({"checksums.json"})
                if set(checksums.entries) != expected_names:
                    missing_checksums = expected_names.difference(checksums.entries)
                    extra_checksums = set(checksums.entries).difference(expected_names)
                    details = []
                    if missing_checksums:
                        details.append(f"missing: {', '.join(sorted(missing_checksums))}")
                    if extra_checksums:
                        details.append(f"extra: {', '.join(sorted(extra_checksums))}")
                    raise ArchiveError(
                        "Checksum index does not match archive members ("
                        + "; ".join(details)
                        + ")."
                    )
                total_bytes = len(archive.read("checksums.json"))
                by_name = {member.filename: member for member in members}
                for name, expected in checksums.entries.items():
                    member = by_name[name]
                    data = archive.read(member)
                    total_bytes += len(data)
                    actual = f"sha256:{hashlib.sha256(data).hexdigest()}"
                    if actual != expected.sha256 or len(data) != expected.size:
                        raise ArchiveError(f"Checksum mismatch for archive member: {name}")
                    unix_mode = member.external_attr >> 16
                    if not stat.S_ISREG(unix_mode) and not stat.S_ISLNK(unix_mode):
                        raise ArchiveError(f"Unsupported archive entry type: {name}")
                    actual_type = self._member_type(member)
                    if actual_type != expected.type:
                        raise ArchiveError(f"Entry type mismatch for archive member: {name}")
                    actual_mode = stat.S_IMODE(member.external_attr >> 16)
                    if actual_mode != expected.mode:
                        raise ArchiveError(f"Permission mode mismatch for archive member: {name}")
                    if actual_mode & 0o7000:
                        raise ArchiveError(f"Unsafe special permission bits: {name}")
                    if actual_type == "symlink":
                        try:
                            target = data.decode("utf-8", errors="strict")
                        except UnicodeDecodeError as error:
                            raise ArchiveError(
                                f"Invalid symlink target encoding: {name}"
                            ) from error
                        resolved_target = resolve_symlink_member(name, target)
                        if not any(
                            candidate == resolved_target
                            or candidate.startswith(f"{resolved_target}/")
                            for candidate in names
                        ):
                            raise ArchiveError(f"Symlink target is missing from archive: {name}")
                if payloads is not None:
                    payload_names = [item.member for item in payloads.payloads]
                    if len(payload_names) != len(set(payload_names)):
                        raise ArchiveError("payloads.json contains duplicate members.")
                    native_names = {name for name in names if name.startswith("native/")}
                    if set(payload_names) != native_names:
                        raise ArchiveError(
                            "Payload inventory does not match native archive members."
                        )
                    for payload in payloads.payloads:
                        validate_member_name(payload.original_path)
                        entry = checksums.entries[payload.member]
                        if payload.sha256 != entry.sha256 or payload.mode != entry.mode:
                            raise ArchiveError(
                                f"Payload metadata mismatch for archive member: {payload.member}"
                            )
                regular_native_names = {
                    name
                    for name in names
                    if name.startswith("native/") and self._member_type(by_name[name]) == "file"
                }
                if timestamps is not None:
                    if set(timestamps.entries) != regular_native_names:
                        raise ArchiveError(
                            "Timestamp inventory does not match regular native archive members."
                        )
                    for name, mtime_ns in timestamps.entries.items():
                        validate_member_name(name)
                        if mtime_ns < 0:
                            raise ArchiveError(
                                f"Timestamp must be non-negative for archive member: {name}"
                            )
                diagnostics = [
                    Diagnostic(
                        code="archive-verified",
                        severity=Severity.INFO,
                        message="All indexed archive members passed SHA-256 verification.",
                    )
                ]
                if timestamps is None:
                    diagnostics.append(
                        Diagnostic(
                            code="source-timestamps-unavailable",
                            severity=Severity.WARNING,
                            message=(
                                "Source modification times are unavailable; recent-session "
                                "ordering cannot be guaranteed."
                            ),
                        )
                    )
                else:
                    diagnostics.append(
                        Diagnostic(
                            code="source-timestamps-preserved",
                            severity=Severity.INFO,
                            message="Source modification times are present and verified.",
                        )
                    )
                return ArchiveInspectionReport(
                    source=str(source.resolve()),
                    manifest=manifest,
                    checksums_valid=True,
                    archived_files=len(names),
                    archived_bytes=total_bytes,
                    diagnostics=diagnostics,
                )
        except (
            zipfile.BadZipFile,
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValidationError,
        ) as error:
            raise ArchiveError(f"Invalid agentpack archive: {error}") from error

    def read_manifest(self, source: Path) -> ArchiveManifest:
        return self.inspect(source).manifest

    def read_payloads(self, source: Path) -> PayloadsDocument:
        manifest = self.read_manifest(source)
        if manifest.format_version != 2:
            raise ArchiveError("Format v1 archives do not contain a payload inventory.")
        with zipfile.ZipFile(source, "r") as archive:
            return PayloadsDocument.model_validate_json(archive.read("payloads.json"))

    def read_timestamps(self, source: Path) -> TimestampsDocument | None:
        self.inspect(source)
        with zipfile.ZipFile(source, "r") as archive:
            if "timestamps.json" not in archive.namelist():
                return None
            return TimestampsDocument.model_validate_json(archive.read("timestamps.json"))

    def read_inventory(self, source: Path, name: str) -> dict[str, object]:
        if name not in {"projects.json", "skills.json"}:
            raise ArchiveError(f"Unsupported inventory document: {name}")
        self.inspect(source)
        with zipfile.ZipFile(source, "r") as archive:
            value = json.loads(archive.read(name))
        if not isinstance(value, dict):
            raise ArchiveError(f"Invalid inventory document: {name}")
        return value

    def materialize(self, source: Path, destination: Path) -> None:
        """Safely materialize verified native payloads without using ZipFile.extract."""
        self.inspect(source)
        timestamp_document = self.read_timestamps(source)
        timestamps = timestamp_document.entries if timestamp_document is not None else {}
        destination.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(source, "r") as archive:
            members = [
                member
                for member in archive.infolist()
                if not member.is_dir() and member.filename.startswith("native/")
            ]
            regular = [member for member in members if self._member_type(member) == "file"]
            links = [member for member in members if self._member_type(member) == "symlink"]
            for member in regular:
                target = destination / archive_path(member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    raise ArchiveError(f"Refusing to overwrite staged member: {member.filename}")
                with archive.open(member, "r") as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                os.chmod(target, stat.S_IMODE(member.external_attr >> 16))
                mtime_ns = timestamps.get(member.filename)
                if mtime_ns is not None:
                    os.utime(target, ns=(mtime_ns, mtime_ns))
            for member in links:
                target = destination / archive_path(member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                link_value = archive.read(member).decode("utf-8", errors="strict")
                resolve_symlink_member(member.filename, link_value)
                os.symlink(link_value, target)

    @staticmethod
    def _member_type(member: zipfile.ZipInfo) -> str:
        unix_mode = member.external_attr >> 16
        return "symlink" if stat.S_ISLNK(unix_mode) else "file"

    @staticmethod
    def _validate_inventory(data: bytes, key: str) -> None:
        value = json.loads(data)
        if not isinstance(value, dict) or not isinstance(value.get(key), list):
            raise ArchiveError(f"Invalid {key}.json inventory document.")


def archive_path(value: str) -> Path:
    validate_member_name(value)
    return Path(*value.split("/"))
