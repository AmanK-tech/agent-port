---
name: agent-port
description: Orchestrates Agent Port to inspect, transfer, back up, plan, safely apply, verify, or roll back native Codex and Claude Code migrations. Use when moving coding-agent sessions or user-owned skills between machines, examining .agentpack archives, resolving restore mappings or conflicts, validating destinations, or recovering a restore.
---

# Agent Port

Use the `agent-port` CLI as the sole migration engine. Do not reproduce its archive, mapping,
database, verification, or rollback logic in ad hoc scripts.

## Establish the boundary

1. Identify whether the user wants inspection, LAN transfer, backup, restore planning, apply,
   validation, or rollback.
2. Confirm the source, archive, destination, and intended harness when relevant.
3. Run `agent-port --version` before other commands and require `0.5.4` or newer in the `0.5.x`
   series. If it is unavailable or incompatible, stop and explain that Agent Port must be installed
   or upgraded; never install or upgrade it automatically.
4. Use explicit paths. Do not inspect unrelated home directories.

Refuse cross-harness session conversion, credential migration, cloud or cross-network transfer,
creation of missing repositories, and execution of archived skill scripts.
Only use `agent-port transfer` after the user explicitly requests LAN transfer.
Never print transcript content or suspected secret values. Pairing codes are intentionally visible
in user-facing CLI, tool, and chat output, but must stay out of commands, process arguments,
Agent Port diagnostic logs, generated summaries, and migration state.

Use only documented commands. Never probe with `--help`, run `transfer inspect`, target an archive
with `skills inspect`, use shell redirection or pipelines, or dump migration files with `ls`, `cat`,
`grep`, `head`, `diff`, or checksum commands.

## Inspect safely

Inspect a harness or archive before backup or restore:

```bash
agent-port inspect PATH --format json
agent-port skills inspect HARNESS_HOME --format json
```

Use `--redact-paths` when the output will be shared. Summarize harness and version, schema,
counts, eligible skills, exclusions, warnings, and missing paths without dumping payload content.

## Back up on the source machine

Follow this order:

```text
inspect harness -> inspect skill ownership -> create backup -> inspect archive
```

Create only the requested session and skill payloads at an explicit `.agentpack` path:

```bash
agent-port backup HARNESS_HOME --include sessions,skills --output BACKUP.agentpack
agent-port inspect BACKUP.agentpack --format json
```

Report the archive path, harness, counts, and warnings. The user may move it manually or explicitly
request Agent Port's encrypted same-LAN pairing flow. Do not invoke `scp`, SFTP, cloud storage, or
an ad hoc transfer mechanism.

## Transfer on the local network

Only after an explicit transfer request, start the source command and clearly show its temporary
pairing code:

```bash
agent-port transfer send HARNESS_HOME
```

On the destination, preserve an explicit path or prepare the default retained workspace:

```text
agent-port transfer prepare --format json
```

Ask for the code as free-form input. Use the harness file-write tool, not a shell command, to write
it to the exact absolute returned path, then run:

```text
agent-port transfer receive --workspace MIGRATION_WORKSPACE --format json
```

The code may remain visible in user-facing CLI, tool, and chat output, but never place it in a
command, Agent Port diagnostic log, or migration state. Agent Port consumes the file once. If
receipt fails, prepare a new authorized path in the same workspace and retry without `chmod`.
Automatic discovery is the default; if blocked, also ask for the exact fallback host and port.
Never require a Terminal, open firewall rules, use a relay,
or imply that receipt applies the archive. Inspect the received archive exactly once.

## Create the mandatory restore plan

On the destination machine, follow this order:

```text
inspect archive -> inspect destination -> collect mappings and policies -> save plan -> review
```

Create a saved plan; never apply an archive directly:

```bash
agent-port restore plan BACKUP.agentpack \
  --destination HARNESS_HOME \
  --destination-home USER_HOME \
  --map "SOURCE_PREFIX=DESTINATION_PREFIX" \
  --format json
```

Exit code 1 with `status: blocked` means a plan was saved successfully. Present its suggested root
mapping, operations, blockers, and only actual differing eligible skill decisions.
Collect explicit approval before adding `--accept-unmapped`, `--skill-conflict NAME=skip`, or
`--skill-conflict NAME=replace`. Regenerate the plan when a decision changes; never hand-edit its
JSON. Never compare user-owned archive skills with managed or cached plugin skills, and never
present Claude `project_registration: false` as a warning.

Session ID conflicts, harness mismatches, unsupported schemas or versions, and stale inputs must
stop the workflow. Do not weaken validation to continue.

## Hand off plugin-guided apply safely

After every revision, run `agent-port restore plan-info RESTORE_PLAN.json --format json`. Use only
that final summary's plan ID, run directory, mappings, operations, blockers, policies, top-level
conversation count, associated subagent count, total transcript count, active projects, user-owned
skills, and attachments. For a plugin-guided restore, ask for fresh explicit authorization to
arm that exact plan and apply only after Agent Port verifies the destination harness has closed. A
general "continue" is insufficient.

Run the handoff yourself:

```bash
agent-port restore handoff RESTORE_PLAN.json --confirm-quit-to-apply --format json
```

Tell the user only to quit every destination harness process and wait for the completion
notification. Never hand them the apply command or require a Terminal. When they return after
restart, automatically run:

```bash
agent-port restore verify RUN_DIRECTORY --format json
```

If verification is pending, ask the user to quit again. Report the handoff state, all three
verification booleans, counts, destination backup, rollback directory, registration warnings, and
restart requirement. Add project registration only with explicit approval and only for Codex.

Only `verified` with all three booleans true and complete expected/verified counts is success.
Treat `pending` as a request to quit again, `changed` as uncertified current integrity, `failed` as
failure, and `rolled-back` as no active migrated data. Never call changed or incomplete state
complete or benign.

Direct CLI users outside a plugin may still run `restore apply` after fresh explicit confirmation
that the harness is already closed; never pass `--confirm-harness-closed` without that confirmation.

## Handle failure and rollback

For a stale plan or changed archive or destination, stop and create a new plan. For a failed
apply, preserve the run directory and failure report, report whether automatic rollback
succeeded, and do not manually delete destination files.

Roll back only when the user explicitly requests it. Immediately before rollback, obtain fresh
confirmation that the harness is closed:

```bash
agent-port restore rollback RUN_DIRECTORY --confirm-harness-closed
```

Respect rollback refusal when restored state has changed. Never overwrite newer user data or
attempt a forced rollback. Never delete retained migration evidence automatically.
