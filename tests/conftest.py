from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "portable-home"
    source = home / ".codex"
    project = tmp_path / "project"
    project.mkdir()
    transcript = source / "sessions" / "2026" / "06" / "30" / "rollout-thread-1.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "thread-1",
                    "cwd": str(project),
                    "cli_version": "1.2.3",
                },
            },
            {"type": "event_msg", "payload": {"message": "synthetic secret text"}},
        ],
    )
    write_jsonl(
        source / "history.jsonl",
        [{"session_id": "thread-1", "text": "synthetic history entry"}],
    )
    write_jsonl(
        source / "session_index.jsonl",
        [{"id": "thread-1", "cwd": str(project)}],
    )
    attachment = source / "attachments" / "synthetic.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("synthetic attachment", encoding="utf-8")
    database = source / "state.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, rollout_path TEXT)"
        )
        connection.execute("CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO _sqlx_migrations VALUES (39)")
        connection.execute(
            "CREATE TABLE thread_dynamic_tools ("
            "thread_id TEXT, name TEXT, definition TEXT, PRIMARY KEY (thread_id, name))"
        )
        connection.execute(
            "INSERT INTO thread_dynamic_tools VALUES "
            "('thread-1', 'synthetic-tool', '{\"type\":\"object\"}')"
        )
        connection.execute(
            "CREATE TABLE thread_spawn_edges ("
            "parent_thread_id TEXT, child_thread_id TEXT, relation TEXT, "
            "PRIMARY KEY (parent_thread_id, child_thread_id))"
        )
        connection.execute(
            "INSERT INTO thread_spawn_edges VALUES ('thread-1', 'thread-1', 'synthetic')"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?)",
            ("thread-1", str(project), str(transcript)),
        )
        connection.commit()
    skill = home / ".agents" / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Synthetic test skill.\n---\n\nDo a safe thing.\n",
        encoding="utf-8",
    )
    script = skill / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    system_skill = source / "skills" / ".system" / "managed"
    system_skill.mkdir(parents=True)
    (system_skill / "SKILL.md").write_text(
        "---\nname: managed\ndescription: Managed fixture.\n---\n", encoding="utf-8"
    )
    cached_skill = source / "skills" / ".curated" / "cached"
    cached_skill.mkdir(parents=True)
    (cached_skill / "SKILL.md").write_text(
        "---\nname: cached\ndescription: Cached fixture.\n---\n", encoding="utf-8"
    )
    plugin_skill = source / "plugins" / "cache" / "example" / "skills" / "plugin-skill"
    plugin_skill.mkdir(parents=True)
    (plugin_skill / "SKILL.md").write_text(
        "---\nname: plugin-skill\ndescription: Plugin fixture.\n---\n", encoding="utf-8"
    )
    project_skill = project / ".agents" / "skills" / "project-skill"
    project_skill.mkdir(parents=True)
    (project_skill / "SKILL.md").write_text(
        "---\nname: project-skill\ndescription: Project fixture.\n---\n", encoding="utf-8"
    )
    unknown_skill = home / "custom-skills" / "unknown-skill"
    unknown_skill.mkdir(parents=True)
    (unknown_skill / "SKILL.md").write_text(
        "---\nname: unknown-skill\ndescription: Unknown ownership fixture.\n---\n",
        encoding="utf-8",
    )
    (source / "config.toml").write_text(
        '[[skills.config]]\npath = "'
        + (unknown_skill / "SKILL.md").as_posix()
        + '"\nenabled = true\n',
        encoding="utf-8",
    )
    return source


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    source = tmp_path / ".claude"
    project = tmp_path / "project"
    project.mkdir()
    transcript = source / "projects" / "-synthetic-project" / "session-1.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "system",
                "sessionId": "session-1",
                "cwd": str(project),
                "version": "2.3.4",
            },
            {"type": "assistant", "sessionId": "session-1", "message": {"content": "hello"}},
        ],
    )
    skill = source / "skills" / "personal"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: personal\ndescription: Synthetic personal skill.\n---\n",
        encoding="utf-8",
    )
    project_skill = project / ".claude" / "skills" / "project-skill"
    project_skill.mkdir(parents=True)
    (project_skill / "SKILL.md").write_text(
        "---\nname: project-skill\ndescription: Project fixture.\n---\n", encoding="utf-8"
    )
    return source
