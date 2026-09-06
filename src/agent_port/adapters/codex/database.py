from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from agent_port.domain.errors import BackupError
from agent_port.domain.models import Diagnostic, NativeSchema, Severity


@dataclass
class DatabaseInspection:
    schema: NativeSchema
    thread_ids: set[str] = field(default_factory=set)
    project_paths: set[str] = field(default_factory=set)
    thread_projects: dict[str, str] = field(default_factory=dict)
    rollout_paths: set[str] = field(default_factory=set)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def inspect_database(path: Path) -> DatabaseInspection:
    with tempfile.TemporaryDirectory(prefix="agent-port-sqlite-") as temporary:
        snapshot = Path(temporary) / "snapshot.sqlite"
        snapshot_database(path, snapshot)
        try:
            with closing(sqlite3.connect(snapshot)) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise BackupError(f"SQLite integrity check failed for {path}: {integrity}")
                rows = connection.execute(
                    "SELECT name, type, COALESCE(sql, '') FROM sqlite_master "
                    "WHERE type IN ('table', 'index', 'trigger', 'view') ORDER BY type, name"
                ).fetchall()
                normalized = json.dumps(rows, separators=(",", ":"), ensure_ascii=True)
                fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                columns: set[str] = set()
                if "threads" in tables:
                    columns = {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(threads)").fetchall()
                    }
                recognized = "threads" in tables and "id" in columns
                result = DatabaseInspection(
                    schema=NativeSchema(
                        kind="sqlite-schema-fingerprint",
                        value=f"sha256:{fingerprint}",
                        recognized=recognized,
                    )
                )
                if recognized:
                    selected = ["id"]
                    selected.extend(
                        column for column in ("cwd", "rollout_path") if column in columns
                    )
                    query = (
                        "SELECT "
                        + ", ".join(f'"{column}"' for column in selected)
                        + " FROM threads"
                    )
                    for row in connection.execute(query):
                        values = dict(zip(selected, row, strict=True))
                        _add_string(result.thread_ids, values.get("id"))
                        _add_string(result.project_paths, values.get("cwd"))
                        _add_string(result.rollout_paths, values.get("rollout_path"))
                        thread_id = values.get("id")
                        cwd = values.get("cwd")
                        if isinstance(thread_id, str) and isinstance(cwd, str) and cwd:
                            result.thread_projects[thread_id] = cwd
                else:
                    result.diagnostics.append(
                        Diagnostic(
                            code="opaque-codex-database",
                            severity=Severity.WARNING,
                            message=(
                                "SQLite schema is healthy but unfamiliar; it will be backed up "
                                "opaquely and is not approved for restore."
                            ),
                            path=str(path),
                        )
                    )
                return result
        except sqlite3.DatabaseError as error:
            raise BackupError(f"Cannot inspect SQLite database {path}: {error}") from error


def snapshot_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
        with (
            closing(sqlite3.connect(source_uri, uri=True)) as source_connection,
            closing(sqlite3.connect(destination)) as destination_connection,
        ):
            source_connection.backup(destination_connection)
        source_mtime_ns = source.stat().st_mtime_ns
        os.utime(destination, ns=(source_mtime_ns, source_mtime_ns))
    except sqlite3.DatabaseError as error:
        destination.unlink(missing_ok=True)
        raise BackupError(f"Cannot snapshot SQLite database {source}: {error}") from error


def _add_string(target: set[str], value: object) -> None:
    if isinstance(value, str) and value:
        target.add(value)
