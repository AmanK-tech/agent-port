from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

import yaml

from agent_port.domain.models import Diagnostic, Severity, SkillOwner, SkillRecord, SkillScope
from agent_port.infrastructure.filesystem.copying import copy_tree_safely

_ABSOLUTE_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\\\Users\\\\)")
_SECRET_NAMES = {
    ".env",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "secrets.json",
}
_SECRET_FIELD = re.compile(r"(?i)(api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]")


def scan_skill_root(
    root: Path,
    owner: SkillOwner,
    scope: SkillScope,
) -> list[SkillRecord]:
    if not root.is_dir():
        return []
    records: list[SkillRecord] = []
    for candidate in sorted(root.iterdir(), key=lambda path: path.name):
        if candidate.name.startswith(".") and candidate.name not in {".system"}:
            continue
        if candidate.is_dir() and (candidate / "SKILL.md").is_file():
            records.append(inspect_skill(candidate, owner, scope))
    return records


def inspect_skill(path: Path, owner: SkillOwner, scope: SkillScope) -> SkillRecord:
    warnings: list[Diagnostic] = []
    portability_checks = owner is SkillOwner.USER
    manifest = path / "SKILL.md"
    parsed_name = path.name
    try:
        text = manifest.read_text(encoding="utf-8")
        frontmatter = _parse_frontmatter(text)
        value = frontmatter.get("name")
        if isinstance(value, str) and value.strip():
            parsed_name = value.strip()
        if portability_checks and not isinstance(frontmatter.get("description"), str):
            warnings.append(
                _finding(
                    "missing-description",
                    Severity.WARNING,
                    "SKILL.md has no description.",
                    manifest,
                )
            )
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as error:
        warnings.append(
            _finding("malformed-skill", Severity.ERROR, f"Cannot parse SKILL.md: {error}", manifest)
        )

    digest = hashlib.sha256()
    file_count = 0
    license_name = "unknown"
    root = path.resolve()
    for item in _walk_entries(path):
        relative = item.relative_to(path).as_posix()
        mode = stat.S_IMODE(item.lstat().st_mode)
        digest.update(f"{relative}\0{mode:o}\0".encode())
        if item.is_symlink():
            target = os.readlink(item)
            digest.update(b"symlink\0" + target.encode("utf-8", errors="surrogateescape"))
            try:
                resolved = item.resolve(strict=True)
            except OSError:
                warnings.append(
                    _finding(
                        "broken-symlink", Severity.ERROR, "Skill contains a broken symlink.", item
                    )
                )
            else:
                if not resolved.is_relative_to(root):
                    warnings.append(
                        _finding(
                            "escaping-symlink",
                            Severity.ERROR,
                            "Skill symlink escapes the skill directory.",
                            item,
                        )
                    )
            file_count += 1
            continue
        if not item.is_file():
            continue
        file_count += 1
        data = item.read_bytes()
        digest.update(b"file\0" + hashlib.sha256(data).digest())
        lower_name = item.name.lower()
        if lower_name.startswith(("license", "copying")):
            license_name = f"file:{relative}"
        if portability_checks and (
            lower_name in _SECRET_NAMES or "secret" in lower_name or "credential" in lower_name
        ):
            warnings.append(
                _finding(
                    "credential-like-filename",
                    Severity.WARNING,
                    "Skill contains a credential-like filename; review it before sharing.",
                    item,
                )
            )
        if len(data) <= 1_000_000:
            decoded = data.decode("utf-8", errors="ignore")
            if portability_checks and _ABSOLUTE_PATH.search(decoded):
                warnings.append(
                    _finding(
                        "absolute-path",
                        Severity.WARNING,
                        "Skill contains a source-machine absolute path.",
                        item,
                    )
                )
            if portability_checks and _SECRET_FIELD.search(decoded):
                warnings.append(
                    _finding(
                        "credential-like-field",
                        Severity.WARNING,
                        "Skill contains a credential-like field; its value was not displayed.",
                        item,
                    )
                )

    eligible = owner is SkillOwner.USER and not any(
        warning.severity is Severity.ERROR for warning in warnings
    )
    return SkillRecord(
        name=parsed_name,
        scope=scope,
        owner=owner,
        source_path=str(path.resolve(strict=False)),
        files=file_count,
        checksum=f"sha256:{digest.hexdigest()}",
        license=license_name,
        eligible=eligible,
        warnings=warnings,
    )


def copy_skill(record: SkillRecord, destination: Path) -> None:
    copy_tree_safely(Path(record.source_path), destination)


def _parse_frontmatter(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing YAML frontmatter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as error:
        raise ValueError("unterminated YAML frontmatter") from error
    value = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(value, dict):
        raise ValueError("frontmatter must be a mapping")
    return value


def _walk_entries(root: Path) -> list[Path]:
    entries: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            entry = current_path / name
            if entry.is_symlink():
                entries.append(entry)
                directories.remove(name)
        entries.extend(current_path / name for name in files)
    return sorted(entries, key=lambda item: item.relative_to(root).as_posix())


def _finding(code: str, severity: Severity, message: str, path: Path) -> Diagnostic:
    return Diagnostic(code=code, severity=severity, message=message, path=str(path))
