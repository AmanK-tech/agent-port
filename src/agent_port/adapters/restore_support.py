from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from agent_port.domain.errors import RestoreError
from agent_port.domain.models import (
    Diagnostic,
    OperationKind,
    PlannedOperation,
    RestoreConflict,
    Severity,
    SkillConflictPolicy,
    SkillOwner,
    SkillScope,
    VerificationReport,
)
from agent_port.domain.ports import AdapterRestorePlan, RestorePlanningContext
from agent_port.infrastructure.filesystem.jsonl import inspect_jsonl
from agent_port.infrastructure.filesystem.skills import inspect_skill
from agent_port.infrastructure.path_mapping import PathMapper


def sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def verify_expected_payloads(
    operations: list[PlannedOperation], harness: str
) -> VerificationReport:
    diagnostics: list[Diagnostic] = []
    checks: list[str] = []
    for operation in operations:
        if operation.kind is OperationKind.SKIP or operation.expected_sha256 is None:
            continue
        destination = Path(operation.destination)
        if (
            destination.is_dir()
            and operation.member
            and operation.member.startswith("native/skills/")
        ):
            actual = inspect_skill(destination, SkillOwner.USER, SkillScope.USER).checksum
        elif destination.is_symlink():
            target = os.readlink(destination).encode("utf-8", errors="surrogateescape")
            actual = sha256_bytes(target)
        elif destination.is_file():
            actual = sha256_file(destination)
        else:
            continue
        if actual != operation.expected_sha256:
            diagnostics.append(
                Diagnostic(
                    code="restored-checksum-mismatch",
                    severity=Severity.ERROR,
                    message="Restored payload checksum does not match its plan.",
                    path=str(destination),
                )
            )
        else:
            checks.append(f"Checksum verified: {destination.name}")
    return VerificationReport(valid=not diagnostics, checks=checks, diagnostics=diagnostics)


def project_mapper(context: RestorePlanningContext) -> PathMapper:
    from agent_port.domain.models import PathMapping

    return PathMapper(
        [
            PathMapping(source=project.source, destination=project.destination)
            for project in context.projects
            if project.source != project.destination
        ]
    )


def transform_jsonl(path: Path, harness: str, mapper: PathMapper) -> bytes:
    output = bytearray()
    try:
        lines = path.read_bytes().splitlines(keepends=True)
    except OSError as error:
        raise RestoreError(f"Cannot read staged JSONL {path}: {error}") from error
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip(b"\r\n")
        if not line.strip():
            raise RestoreError(f"Blank JSONL record in {path} at line {number}.")
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RestoreError(f"Malformed JSONL in {path} at line {number}: {error}") from error
        if not isinstance(value, dict):
            raise RestoreError(f"JSONL record is not an object in {path} at line {number}.")
        changed = False
        if harness == "codex" and value.get("type") == "session_meta":
            payload = value.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
                mapped = mapper.apply(payload["cwd"])
                if mapped.mapped and mapped.value != payload["cwd"]:
                    payload["cwd"] = mapped.value
                    changed = True
        elif harness == "codex" and path.name in {"history.jsonl", "session_index.jsonl"}:
            if isinstance(value.get("cwd"), str):
                mapped = mapper.apply(value["cwd"])
                if mapped.mapped and mapped.value != value["cwd"]:
                    value["cwd"] = mapped.value
                    changed = True
        elif harness == "claude-code" and isinstance(value.get("cwd"), str):
            mapped = mapper.apply(value["cwd"])
            if mapped.mapped and mapped.value != value["cwd"]:
                value["cwd"] = mapped.value
                changed = True
        if changed:
            output.extend(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
            output.extend(b"\n")
        else:
            output.extend(line)
            output.extend(b"\n")
    return bytes(output)


def classify_append_only_jsonl(source: bytes, destination: Path) -> tuple[str, bytes]:
    try:
        destination_bytes = destination.read_bytes()
    except OSError as error:
        raise RestoreError(f"Cannot read destination JSONL {destination}: {error}") from error
    source_records, source_lines = _ordered_jsonl_records(source, "staged source")
    destination_records, _destination_lines = _ordered_jsonl_records(
        destination_bytes, str(destination)
    )
    shared = min(len(source_records), len(destination_records))
    if source_records[:shared] != destination_records[:shared]:
        return "diverged", destination_bytes
    if len(source_records) == len(destination_records):
        return "identical", destination_bytes
    if len(destination_records) > len(source_records):
        return "destination-ahead", destination_bytes
    prefix = destination_bytes
    if prefix and not prefix.endswith((b"\n", b"\r")):
        prefix += b"\n"
    return "source-ahead", prefix + b"".join(source_lines[len(destination_records) :])


def apply_append_only_jsonl(source: Path, destination: Path) -> None:
    try:
        source_bytes = source.read_bytes()
    except OSError as error:
        raise RestoreError(f"Cannot read staged JSONL {source}: {error}") from error
    relation, merged = classify_append_only_jsonl(source_bytes, destination)
    if relation != "source-ahead":
        raise RestoreError(f"Append-only JSONL precondition changed for {destination}: {relation}.")
    source_mtime_ns = source.stat().st_mtime_ns
    destination_mtime_ns = destination.stat().st_mtime_ns
    merged_mtime_ns = max(source_mtime_ns, destination_mtime_ns)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.agent-port-tmp")
    try:
        with temporary.open("xb") as output:
            output.write(merged)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        os.utime(destination, ns=(merged_mtime_ns, merged_mtime_ns))
    finally:
        temporary.unlink(missing_ok=True)


def _ordered_jsonl_records(data: bytes, label: str) -> tuple[list[dict[str, object]], list[bytes]]:
    records: list[dict[str, object]] = []
    lines: list[bytes] = []
    for number, raw_line in enumerate(data.splitlines(keepends=True), start=1):
        line = raw_line.rstrip(b"\r\n")
        if not line.strip():
            raise RestoreError(f"Blank JSONL record in {label} at line {number}.")
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RestoreError(f"Malformed JSONL in {label} at line {number}: {error}") from error
        if not isinstance(value, dict):
            raise RestoreError(f"JSONL record is not an object in {label} at line {number}.")
        records.append(value)
        lines.append(line + b"\n")
    return records, lines


def destination_session_ids(destination: Path, harness: str) -> dict[str, Path]:
    return {
        identity: paths[0]
        for identity, paths in destination_session_candidates(destination, harness).items()
    }


def destination_session_candidates(destination: Path, harness: str) -> dict[str, list[Path]]:
    identities: dict[str, list[Path]] = {}
    if harness == "claude-code":
        # Subagents may share their parent's sessionId. They are reconciled by
        # their path beneath that conversation, not as competing conversations.
        transcripts = [
            path
            for path in claude_transcript_candidates(destination)
            if len(path.relative_to(destination / "projects").parts) == 2
        ]
    else:
        transcripts = []
        for root in (destination / "sessions", destination / "archived_sessions"):
            if root.is_dir():
                transcripts.extend(path for path in root.rglob("*.jsonl") if path.is_file())
    for path in transcripts:
        summary = inspect_jsonl(path, harness)
        for identity in summary.session_ids or {path.stem}:
            identities.setdefault(identity, []).append(path)
    return identities


def claude_transcript_candidates(destination: Path) -> list[Path]:
    projects = destination / "projects"
    if not projects.is_dir():
        return []
    result: list[Path] = []
    for container in sorted(path for path in projects.iterdir() if path.is_dir()):
        top_level = [
            path
            for path in container.glob("*.jsonl")
            if path.is_file() and not _is_claude_noise(path, projects)
        ]
        if not top_level:
            continue
        result.extend(top_level)
        result.extend(
            path
            for path in container.rglob("*.jsonl")
            if path.is_file()
            and path not in top_level
            and not _is_claude_noise(path, projects)
            and "subagents" in path.relative_to(container).parts[:-1]
        )
    return sorted(set(result))


def _is_claude_noise(path: Path, projects: Path) -> bool:
    names = {".ds_store", "thumbs.db", "desktop.ini"}
    return any(
        part.startswith("._") or part.casefold() in names
        for part in path.relative_to(projects).parts
    )


def plan_skills(context: RestorePlanningContext) -> AdapterRestorePlan:
    operations: list[PlannedOperation] = []
    conflicts: list[RestoreConflict] = []
    included_names = {Path(skill.source_path).name for skill in context.skills}
    for name in sorted(included_names):
        source = context.extracted / "native" / "skills" / name
        if not source.is_dir():
            conflicts.append(
                RestoreConflict(
                    kind="missing-skill-payload",
                    identity=name,
                    destination=str(source),
                    message="Included skill directory is missing from the archive payload.",
                )
            )
            continue
        destination = (
            context.destination_home / ".agents" / "skills" / name
            if context.manifest.harness.value == "codex"
            else context.destination / "skills" / name
        )
        source_record = inspect_skill(source, SkillOwner.USER, SkillScope.USER)
        if not source_record.eligible:
            conflicts.append(
                RestoreConflict(
                    kind="unsafe-skill",
                    identity=name,
                    destination=str(destination),
                    message="Skill failed restore-time validation.",
                )
            )
            continue
        if not destination.exists() and not destination.is_symlink():
            operations.append(
                PlannedOperation(
                    kind=OperationKind.COPY,
                    member=f"native/skills/{name}",
                    destination=str(destination),
                    identity=name,
                    expected_sha256=source_record.checksum,
                    policy=context.skill_policies.get(name, SkillConflictPolicy.ERROR),
                )
            )
            continue
        if not destination.is_dir() or destination.is_symlink():
            conflicts.append(
                RestoreConflict(
                    kind="unsafe-skill-destination",
                    identity=name,
                    destination=str(destination),
                    message="Destination skill is not a safe directory.",
                )
            )
            continue
        destination_record = inspect_skill(destination, SkillOwner.USER, SkillScope.USER)
        if not destination_record.eligible:
            conflicts.append(
                RestoreConflict(
                    kind="unsafe-skill-destination",
                    identity=name,
                    destination=str(destination),
                    message="Destination skill contains unsafe or malformed content.",
                )
            )
            continue
        if destination_record.checksum == source_record.checksum:
            operations.append(
                PlannedOperation(
                    kind=OperationKind.SKIP,
                    member=f"native/skills/{name}",
                    destination=str(destination),
                    identity=name,
                    expected_sha256=source_record.checksum,
                )
            )
            continue
        policy = context.skill_policies.get(name, SkillConflictPolicy.ERROR)
        if policy is SkillConflictPolicy.ERROR:
            conflicts.append(
                RestoreConflict(
                    kind="skill-collision",
                    identity=name,
                    destination=str(destination),
                    message="Destination has a different skill with the same name.",
                )
            )
        elif policy is SkillConflictPolicy.SKIP:
            operations.append(
                PlannedOperation(
                    kind=OperationKind.SKIP,
                    member=f"native/skills/{name}",
                    destination=str(destination),
                    identity=name,
                    policy=policy,
                )
            )
            conflicts.append(
                RestoreConflict(
                    kind="skill-collision-skipped",
                    identity=name,
                    destination=str(destination),
                    message="Different destination skill will be retained.",
                    blocking=False,
                )
            )
        else:
            operations.append(
                PlannedOperation(
                    kind=OperationKind.REPLACE,
                    member=f"native/skills/{name}",
                    destination=str(destination),
                    identity=name,
                    expected_sha256=source_record.checksum,
                    policy=policy,
                )
            )
    return AdapterRestorePlan(operations=operations, conflicts=conflicts)


def stage_payload_tree(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)


def merge_jsonl(source: Path, destination: Path) -> None:
    source_mtime_ns = source.stat().st_mtime_ns
    destination_mtime_ns = destination.stat().st_mtime_ns if destination.is_file() else 0
    records: dict[str, dict[str, object]] = {}
    for path in (destination, source):
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise RestoreError(f"Invalid JSONL {path}:{number}: {error}") from error
            if not isinstance(value, dict):
                raise RestoreError(f"Invalid non-object JSONL record {path}:{number}")
            key = jsonl_identity(value)
            existing = records.get(key)
            if existing is not None and existing != value:
                raise RestoreError(f"Conflicting JSONL record identity {key} in {destination}")
            records[key] = value
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.agent-port-tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as output:
        for value in records.values():
            output.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, destination)
    merged_mtime_ns = max(source_mtime_ns, destination_mtime_ns)
    os.utime(destination, ns=(merged_mtime_ns, merged_mtime_ns))


def jsonl_merge_conflicts(source_data: bytes, destination: Path) -> list[str]:
    if not destination.is_file():
        return []
    destination_records = _jsonl_records(destination.read_bytes(), destination)
    source_records = _jsonl_records(source_data, destination)
    return sorted(
        key
        for key, value in source_records.items()
        if key in destination_records and destination_records[key] != value
    )


def jsonl_identity(value: dict[str, object]) -> str:
    for key in ("id", "thread_id", "session_id", "sessionId"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return f"{key}:{item}"
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"record:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _jsonl_records(data: bytes, path: Path) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise RestoreError(f"Invalid JSONL encoding in {path}: {error}") from error
    for number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise RestoreError(f"Invalid JSONL {path}:{number}: {error}") from error
        if not isinstance(value, dict):
            raise RestoreError(f"Invalid non-object JSONL record {path}:{number}")
        identity = jsonl_identity(value)
        existing = records.get(identity)
        if existing is not None and existing != value:
            raise RestoreError(f"Conflicting duplicate JSONL identity {identity} in {path}")
        records[identity] = value
    return records


def sqlite_migration_version(path: Path) -> int | None:
    try:
        with closing(
            sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        ) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "_sqlx_migrations" not in tables:
                return None
            row = connection.execute("SELECT MAX(version) FROM _sqlx_migrations").fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except (sqlite3.DatabaseError, OSError, ValueError):
        return None


def merge_sqlite(
    source: Path,
    destination: Path,
    map_path: Callable[[str], str],
    rollout_paths: dict[str, str],
) -> None:
    allowed_tables = ("threads", "thread_dynamic_tools", "thread_spawn_edges")
    try:
        with (
            closing(sqlite3.connect(source)) as source_db,
            closing(sqlite3.connect(destination)) as destination_db,
        ):
            destination_db.execute("PRAGMA foreign_keys=ON")
            destination_db.execute("BEGIN IMMEDIATE")
            for table in allowed_tables:
                source_columns = _table_columns(source_db, table)
                destination_columns = _table_columns(destination_db, table)
                if not source_columns or not destination_columns:
                    continue
                common = [column for column in source_columns if column in destination_columns]
                required_missing = [
                    column
                    for column, required, default, _pk in destination_columns.values()
                    if column not in common and required and default is None
                ]
                if required_missing:
                    raise RestoreError(
                        f"Destination table {table} has unsupported required columns: "
                        + ", ".join(required_missing)
                    )
                quoted = ",".join(f'"{column}"' for column in common)
                rows = source_db.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
                primary = [column for column, details in source_columns.items() if details[3]]
                for row in rows:
                    values = dict(zip(common, row, strict=True))
                    if table == "threads":
                        thread_id = values.get("id")
                        if isinstance(values.get("cwd"), str):
                            values["cwd"] = map_path(values["cwd"])
                        if isinstance(thread_id, str) and thread_id in rollout_paths:
                            values["rollout_path"] = rollout_paths[thread_id]
                    columns = list(values)
                    if primary and all(values.get(column) is not None for column in primary):
                        where = " AND ".join(f'"{column}"=?' for column in primary)
                        key_values = [values[column] for column in primary]
                        existing = destination_db.execute(
                            f'SELECT {quoted} FROM "{table}" WHERE {where}', key_values
                        ).fetchone()
                        if existing is not None:
                            if tuple(values[column] for column in common) != tuple(existing):
                                raise RestoreError(
                                    f"Conflicting {table} row for key "
                                    + ",".join(str(value) for value in key_values)
                                )
                            continue
                    placeholders = ",".join("?" for _column in columns)
                    destination_db.execute(
                        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
                        [values[column] for column in columns],
                    )
            integrity = destination_db.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RestoreError(f"SQLite integrity check failed: {integrity}")
            foreign_keys = destination_db.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise RestoreError(f"SQLite foreign-key check failed: {foreign_keys[:3]}")
            destination_db.commit()
    except sqlite3.DatabaseError as error:
        raise RestoreError(f"SQLite merge failed: {error}") from error


def _table_columns(
    connection: sqlite3.Connection, table: str
) -> dict[str, tuple[str, bool, object, bool]]:
    try:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {row[1]: (row[1], bool(row[3]), row[4], bool(row[5])) for row in rows}
