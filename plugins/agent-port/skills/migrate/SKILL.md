---
name: migrate
description: Guide a complete same-harness Agent Port migration from an old machine to a new machine. Use when moving native Codex or Claude Code sessions and user-owned skills between computers.
---

# Migrate with Agent Port

Read [the safety contract](../../references/safety-contract.md) before acting. Determine whether
this is the source or destination stage. Never imply that Codex sessions can become Claude Code
sessions, or the reverse.

Run `agent-port --version` and require `0.5.4` or newer in the `0.5.x` series. If missing or
incompatible, stop and show `uv tool install "agent-port>=0.5.4,<0.6"` or the documented `pipx`
alternative; never install or upgrade automatically.

Follow [the source workflow](../../references/source-workflow.md) or
[the destination workflow](../../references/destination-workflow.md). Use only the exact commands
there. Never probe with `--help`, invent flags, use shell redirection or pipelines, or inspect
migration files with `ls`, `cat`, `grep`, `head`, `diff`, or checksum commands.

For a requested same-LAN transfer, run `agent-port transfer send HARNESS_HOME` on the source. On
the destination, preserve an explicit archive path; otherwise create the retained workspace:

```text
agent-port transfer prepare --format json
```

Ask for the pairing code as an ordinary free-form reply, never as a multiple-choice or constrained
question. Use the harness file-write operation—not a shell command—to write exactly the code to
the exact absolute returned `pairing_code_file`, then run:

```text
agent-port transfer receive --workspace MIGRATION_WORKSPACE --format json
```

The code remains visible to the user in source CLI, tool, and chat output, but never put it in a
command, Agent Port diagnostic log, or migration state. Agent Port restricts and deletes the file
immediately after reading it. If receipt fails, run `agent-port transfer prepare --workspace
MIGRATION_WORKSPACE --format json`, use its new absolute path, and retry without `chmod`. If
discovery fails, also ask only for the exact fallback host and port.
Never require a destination Terminal.

Inspect the archive exactly once with `agent-port inspect ARCHIVE --format json`. Never run
`transfer inspect`, `skills inspect` without a source, or `skills inspect` against an archive.
Create the auto-numbered plan with `agent-port restore plan ARCHIVE --destination HARNESS_HOME
--destination-home DESTINATION_HOME --format json`. Exit code 1 with `status: blocked` means the
plan was saved successfully and needs decisions; it is not a command failure.

If homes differ, present the single suggested root mapping and request approval. Regenerate with
the approved `--map SOURCE=DESTINATION`; ask separately only for paths outside that root or actual
differing eligible user skills. Never compare archive skills with plugin-managed or cached skills,
or mention Claude `project_registration: false`.

After each revision and immediately before handoff, reload the exact final file:

```text
agent-port restore plan-info RESTORE_PLAN.json --format json
```

Report top-level conversations, associated subagent transcripts, total transcript files, active
projects, user-owned skills, and attachments. Use only this summary's final plan ID, run directory,
mappings, operations, blockers, and differing-skill policies.

Ask for fresh explicit authorization to arm the reviewed plan; a general “continue” is
insufficient. Then run `agent-port restore handoff RESTORE_PLAN.json --confirm-quit-to-apply
--format json` yourself. Tell the user only to quit every harness instance and wait for the
notification. Never hand them `restore apply`.

When the user returns, immediately run `agent-port restore verify RUN_DIRECTORY --format json`. If
pending, ask them to quit again. Report all three verification booleans, separate content counts,
and retained archive, backup, and rollback evidence. Direct CLI users may still use the documented
closed-harness apply command. Cleanup is always a separate explicit request.

Use this result table without speculation:

- `verified`: report success only when every expected count is verified.
- `pending`: ask the user to quit every harness instance again.
- `changed`: say current migrated integrity cannot be certified; never call it benign or complete.
- `failed`: report failure and whether automatic rollback restored the previous state.
- `rolled-back`: state that migrated data is no longer active.
