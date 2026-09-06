---
name: restore
description: Plan and safely apply a same-harness Agent Port restore. Use when restoring an .agentpack archive, mapping project paths, or resolving session and skill conflicts.
---

# Restore with Agent Port

Read [the safety contract](../../references/safety-contract.md), then follow
[the destination workflow](../../references/destination-workflow.md).

Run `agent-port --version` and require `0.5.5` or newer in the `0.5.x` series. Never apply an
archive directly. Use only the canonical workflow commands; do not probe with `--help`, use shell
pipelines, dump files, run `transfer inspect`, or run archive-targeted `skills inspect`.

Inspect the archive once with `agent-port inspect ARCHIVE --format json`, then create a saved,
auto-numbered plan with `agent-port restore plan ARCHIVE --destination HARNESS_HOME
--destination-home DESTINATION_HOME --format json`. A `status: blocked` result was saved
successfully. Present its suggested root mapping, actual blockers, and differing eligible user-skill
policies. Obtain approval before accepting an unmapped path, skipping or replacing a differing
skill, or enabling Codex project registration. Never present Claude `project_registration: false`
as a warning.

Regenerate after every decision. Reload the final file with `agent-port restore plan-info
RESTORE_PLAN.json --format json`; never hand-edit or dump it. Every summary must distinguish
top-level conversations, associated subagent transcripts, total transcript files, active projects,
user-owned skills, and attachments. Never print transcript, memory, attachment, tool-result,
inventory, or raw command content.

Harness mismatches, incompatible version profiles, genuinely divergent sessions, unsupported
schemas, and stale fingerprints must stop the workflow. Compatible patch versions and append-only
session growth are handled by Agent Port and are not conflicts.

Obtain fresh explicit authorization. This is the fresh explicit confirmation for the saved plan;
the handoff worker, rather than a second Terminal interaction, confirms that every harness process
has closed. Then run `agent-port restore handoff RESTORE_PLAN.json
--confirm-quit-to-apply --format json` yourself. Tell the user to quit the harness and wait for the
notification; never send them to a Terminal. When they return, automatically run `agent-port
restore verify RUN_DIRECTORY --format json`. If pending, ask them to quit again. Report the safety,
current-data, destination, content-count, backup, and rollback results. Direct CLI users may still
use `restore apply --confirm-harness-closed`. Never clean up retained evidence automatically.

Only `verified` with all three booleans true and every expected count verified is success. For
`pending`, ask the user to quit again. For `changed`, say current integrity cannot be certified and
do not guess why. For `failed`, report rollback evidence. For `rolled-back`, state that migrated
data is no longer active. Never describe changed or incomplete results as complete or benign.
