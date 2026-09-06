# Adapter development

Harness-specific filesystem and schema knowledge belongs under `agent_port.adapters`. Domain and
application code must not contain Codex or Claude Code paths.

Adapters implement discovery and backup through:

1. `detect` returns evidence without mutating the source.
2. `inspect` produces a harness-independent report without transcript content.
3. Skill discovery classifies ownership and migration eligibility.
4. `collect_backup` copies only approved native data into a temporary staging directory.

Restore adapters additionally implement `plan_restore`, `stage_restore`, `apply_restore`,
`verify_restore`, and `register_projects`. Planning describes every mutation as a typed operation
and never changes the destination. Shared execution creates backups, journals affected paths, and
performs generic atomic file installation; adapters retain native metadata and compatibility logic.

Every restorable schema or transcript profile needs a synthetic compatibility fixture. Unknown
versions produce blocking conflicts instead of best-effort mutation.

Adapters must tolerate empty installations, validate JSONL one object per line, keep internal
formats opaque when possible, and return explicit diagnostics for unsupported layouts. A healthy
but unfamiliar Codex SQLite database may be snapshotted and fingerprinted for backup, but it must
not be marked restore-compatible.

Every adapter must pass the shared synthetic contract tests. Tests may not resolve or inspect the
developer's actual `~/.codex`, `~/.claude`, `$CODEX_HOME`, or `$CLAUDE_CONFIG_DIR`.
