# Agentpack archive format

`.agentpack` files are ZIP containers with normalized entry order, timestamps, paths, and Unix
permission metadata. Version 2 is current; version 1 remains inspectable and is restorable only
when its native layout is unambiguous.

```text
backup.agentpack
├── manifest.json
├── projects.json
├── skills.json
├── payloads.json
├── timestamps.json
├── checksums.json
└── native/
    ├── sessions/
    ├── state/
    └── skills/
```

`manifest.json` identifies the source harness, observed harness versions, normalized compatibility
profiles, adapter and format versions, creation time, source platform and roots, payload counts,
adapter capabilities, and native schema fingerprints. Counts separately identify top-level
conversations, associated subagent transcripts, total transcript files, active projects,
user-owned skills, and attachments. New fields remain optional for existing format-2 archives.

`checksums.json` uses SHA-256 and indexes every other regular file or symlink in the archive. It
records byte size, entry type, and permission mode. It cannot index itself without creating a
self-referential digest.

`timestamps.json` is an optional, checksummed format-v2 extension that maps every regular native
member to its source nanosecond modification time. New backups include it so materialization and
path-transformed restores preserve recent-session ordering while ZIP timestamps remain normalized.
Older format-v2 archives remain restorable but report that recent-session ordering is unavailable.

Version 2 `payloads.json` identifies every `native/` member by logical role, original relative
location, destination scope, restore strategy, checksum, and permission mode. Native members and
payload entries must match exactly. Version 1 has no payload inventory.

Native files are preserved without cross-harness normalization. A future restore implementation
must reject a manifest whose harness does not match its destination adapter.

Claude backups include only project containers with a surviving top-level conversation and their
associated subagents, tool results, memory, and attachments. `.DS_Store`, AppleDouble `._*`,
`Thumbs.db`, and `desktop.ini` entries are excluded.

Readers must reject absolute paths, parent traversal, backslashes, duplicate names, encrypted
members, unsafe symlinks, special permission bits, missing metadata, inventory mismatch, and
checksum mismatch before materialization. Materialization never uses unrestricted ZIP extraction.
