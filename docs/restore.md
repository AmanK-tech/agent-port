# Restore runbook

Restoration is same-harness only. Direct CLI users keep Codex or Claude Code closed through apply
or rollback. Plugin users review and arm the plan first, then quit when the handoff asks.

## Create and inspect the plan

```bash
agent-port restore plan backup.agentpack \
  --destination ~/.codex \
  --destination-home ~ \
  --map "/Users/old/Company=~/Developer/Work/Company" \
  --output restore-plan.json

agent-port restore plan-info restore-plan.json --format json
```

Mappings use component-aware longest-prefix matching. Existing identity paths are accepted. A
missing unmapped project blocks apply unless its exact archived path is passed through
`--accept-unmapped`. This records the missing project; it does not create a fake directory.
When an archived project has a Git origin, `restore plan` suggests a `SOURCE=DESTINATION` mapping
for exactly one matching clone found below `--destination-home`. Review and approve that mapping
by regenerating the plan with `--map`; ambiguous or non-Git projects still require an explicit
path.

A saved plan reports `ready` or `blocked`; blocked-plan exit status does not mean plan generation
failed. Identical sessions are skipped. If one same-ID JSONL history is a structural prefix of the
other, Agent Port preserves or appends the longer valid history. Malformed, ambiguous, truncated,
or genuinely divergent histories block. Differing skills default to `error` and support explicit
`skip` or whole-directory `replace` decisions.

## Apply

```bash
agent-port restore apply restore-plan.json --confirm-harness-closed
```

From the Agent Port plugin, arm a handoff instead. The command returns while a temporary local
worker waits for every destination harness process to close:

```bash
agent-port restore handoff restore-plan.json --confirm-quit-to-apply
```

The worker expires after 30 minutes and records status under `.agent-port-runs/.handoffs/`. It
rechecks closure throughout apply and automatically rolls back if the harness reopens.

Apply rejects a changed archive, modified plan, changed destination, harness mismatch, unsupported
Codex migration, or incompatible Claude Code version. It then creates a destination backup, stages
payloads, journals every affected path, commits native metadata, and runs verification.
Restore and rollback take an exclusive destination lock; an overlapping operation is rejected
before it can create a rollback journal. The operating system releases the lock if a process exits.

Codex project registration is optional and runs after successful verification:

```bash
agent-port restore apply restore-plan.json \
  --confirm-harness-closed \
  --register-projects
```

Only mapped project directories that already exist are opened through `codex app`. Registration
warnings do not undo a valid restore.

## Validate or roll back

```bash
agent-port doctor ~/.codex --harness codex
agent-port restore verify PATH_TO_RUN_DIRECTORY
agent-port restore rollback PATH_TO_RUN_DIRECTORY --confirm-harness-closed
```

The run directory contains `destination-before.agentpack`, `journal.json`, staged data,
`plan.json`, `payloads.json`, `verification.json`, `result.json`, and immutable
`initial-verification.json`. The initial verification runs while the harness remains closed and
must verify every expected count or apply rolls back. Later `restore verify` checks current migrated
data and writes `current-verification.json` without replacing the initial evidence.
Append-only resumed transcripts remain valid. Verification separately reports top-level
conversations, associated subagent transcripts, total transcript files, active projects, skills,
and attachments. Keep the archive and run until the restored harness has been tested. Manual
rollback is unavailable after restored paths change because overwriting newer user data is unsafe.

For Claude, project counts refer to source native containers. A container may hold conversations
with different working directories, and a working directory may occur in several containers.
Container mapping follows its structural native identity; nested subagents and tool results stay
with their parent conversation. Rolled-back runs report zero expected migrated content and remain
`rolled-back`, not `verified`.

Runs without retained `initial-verification.json` emit `initial-verification-unavailable`.
Recorded apply checks and a healthy destination do not establish when later file loss occurred.
See [the destination restore diagnosis](destination-restore-fix.md) for the 0.5.5 fixes and the
limits of the evidence from the reported older run. Regenerate plans with the upgraded CLI before
retrying a failed migration; existing verified archives can be reused.
