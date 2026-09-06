# Changelog

All notable Agent Port changes are documented here. CLI and plugin versions are released in
lockstep.

## 0.5.5 - 2026-09-06

### Added

- Restore planning recognizes a unique cloned Git repository by a one-way origin fingerprint and
  suggests the required source-to-destination project mapping when the folder path changed.

### Fixed

- Claude verification now counts source project containers consistently with the archive. Mixed
  working directories and shared paths no longer trigger rollback after every transcript verifies.
- Claude project folders are mapped from their native container identity instead of the last
  working directory encountered. New plans also recover the correct identity from older archives.
- Subagent transcripts are reconciled beneath their parent conversation, including repeated
  restores and parents already stored in a different destination container.
- Rolled-back runs now clear all expected content counts consistently while remaining explicitly
  unsuccessful migrations. Count failures report the exact expected and verified values.
- Verification reports missing initial evidence explicitly instead of leaving its absence unexplained.

### Safety

- Repository fingerprints are stored separately from the project inventory and never include
  remote URLs or credentials. Ambiguous or non-Git matches remain blocked until the user supplies
  an explicit mapping.
- Restore and rollback now hold a process-level destination lock. Overlapping runs cannot prepare
  conflicting rollback journals and delete each other's restored files.

## 0.5.4 - 2026-09-05

### Added

- Added checksummed source timestamp metadata so restored native sessions retain recent-session
  ordering while archives keep deterministic ZIP timestamps.
- Added immutable closed-harness initial verification evidence and separate current verification
  snapshots with explicit transcript-retention classifications.

### Fixed

- Invalid pairing codes and rejected authentication now explain how to retry on the destination
  without configuring SSH or entering an account password.
- Verification retains expected counts in the saved plan, so moving or replacing the original
  archive cannot invalidate an intact restore.
- Codex verification counts unique conversation IDs, preserves the database's canonical rollout
  selection, and verifies conversations which span multiple project paths.
- Restore success now requires all safety booleans and every expected content count; incomplete
  initial verification automatically rolls back before notification.
- Project verification is counted independently instead of collapsing every project after one
  transcript failure.
- Plugin receipt now uses a non-precreated authorized path inside its private workspace and infers
  that path from `--workspace`, avoiding file overwrite and permission retries.

### Safety

- Pairing codes remain intentionally visible to the user in CLI, tool, and chat output while
  remaining excluded from process arguments, Agent Port logs, and persisted migration state.
- Plugin workflows no longer call changed or incomplete verification results complete or benign.

## 0.5.3 - 2026-07-19

### Added

- Added persistent `~/Agent-Port/Migrations/` workspaces, protected one-use pairing-code files,
  structured receive output, auto-numbered restore plans, and canonical `restore plan-info` output.
- Added explicit Claude compatibility profiles and separate top-level, subagent, and total
  transcript counts throughout archive, plan, handoff, and verification results.

### Fixed

- Compatible Claude patch versions no longer produce an unknown-version blocker, including for
  existing 0.5.2 archives whose profile can be derived from transcript structure.
- Same-session histories now preserve or merge append-only growth in either direction and block
  only malformed, ambiguous, or genuinely divergent histories.
- Claude backups now package only active project containers and exclude platform metadata noise.
- Plugin workflows use tested commands without exploratory help calls, unsupported subcommands,
  archive skill inspection, or raw output dumps.

### Safety

- Plugin pairing codes no longer appear in CLI arguments or process listings and are removed from
  the one-use file immediately after reading.
- Received archives, plans, restore runs, backups, and rollback evidence are retained until a
  separate explicit cleanup request.

## 0.5.2 - 2026-07-18

### Added

- Added a temporary restore handoff worker which waits for objective same-user harness closure,
  monitors for reopening during apply, persists status, expires after 30 minutes, and sends a
  best-effort completion notification.
- Added `restore verify` for run-specific proof across the final plan, apply result, journal,
  rollback backup, current migrated payloads, native indexes/databases, and destination health.
- Added append-aware transcript verification, self-contained plan and payload snapshots, legacy
  0.5.1 plan support, and a dedicated plugin verification workflow.

### Safety

- Plugin users no longer need to copy an apply command into Terminal. Explicit authorization arms
  the handoff, while mutation remains blocked until the worker verifies the harness is closed.
- Reopening the harness during apply causes failure and uses the existing automatic rollback path.

## 0.5.1 - 2026-07-18

### Fixed

- Restored automatic LAN discovery with current Zeroconf releases by honoring the handler's
  keyword callback contract.
- Added harness-driven, non-interactive receipt so plugin users provide only the temporary pairing
  code instead of running the destination command in a separate terminal.
- Counted Claude Code conversations from packaged top-level transcripts, kept nested subagent
  transcripts separate, and ignored stale project containers with no surviving conversation.
- Preserved user-requested destination archive paths and clarified that a failed local prompt does
  not consume a still-running source sender.

## 0.5.0 - 2026-07-12

### Added

- Pairing-code-authenticated `.agentpack` transfer between computers on the same LAN.
- Temporary restricted SSH transport, authenticated Zeroconf discovery, and a manual endpoint
  fallback for networks which block multicast discovery.
- Whole-archive SHA-256 verification, atomic no-clobber receipt, POSIX mode-`0600` destination
  files, and inherited destination-directory access controls on Windows.

### Safety

- Transfer requires explicit commands on both machines, expires after ten minutes by default, and
  never restores or mutates the receiving harness.
- No daemon, OS SSH setup, firewall modification, NAT traversal, relay, account, or cloud storage is
  introduced.

## 0.4.0 - 2026-07-04

### Added

- One shared Agent Port plugin bundle for Codex and Claude Code.
- Repo-hosted marketplace catalogs for both harnesses.
- Guided migrate, backup, restore, doctor, and rollback skills.
- Marketplace branding, privacy, terms, listing copy, and customer installation documentation.
- Strict plugin validation, deterministic plugin ZIP creation, isolated install smoke tests, and a
  tag-gated Trusted Publishing workflow.

### Changed

- Plugin installation is now the primary customer experience.
- The standalone portable skill remains available as a one-release compatibility fallback.

### Safety

- The plugins never install the CLI automatically, execute archived skill scripts, migrate
  credentials, or convert sessions between harnesses.
- Apply and rollback continue to require the CLI's saved-plan, fingerprint, backup, verification,
  and fresh harness-closed confirmation gates.
