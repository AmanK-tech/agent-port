---
name: verify
description: Verify that an Agent Port restore completed safely and that its migrated data remains intact. Use after restart, for pending handoffs, or when the user is unsure whether migration succeeded.
---

# Verify an Agent Port restore

Read [the safety contract](../../references/safety-contract.md).

Run `agent-port --version` first and require `0.5.5` or newer in the `0.5.x` series. Verification
does not mutate harness data; it records its result in retained migration evidence. Use the exact
run directory from the final saved plan or completed handoff:

```text
agent-port restore verify RUN_DIRECTORY --format json
```

For a legacy 0.5.1 run without `plan.json`, add the exact original `--plan RESTORE_PLAN.json`; never
guess or scan unrelated directories for it.

Interpret `verified` as all three booleans being true. `pending` means the armed worker has not
completed, usually because the destination harness is open. Ask the user to quit again and do not
fall back to a manual apply command. `changed` means apply evidence is valid but current migrated
data or destination integrity no longer matches. `failed` means restore evidence or the handoff
failed. `rolled-back` means migrated data is no longer expected.

Never speculate that `changed` is benign, and never say a migration is complete unless status is
`verified`, all three booleans are true, and every expected content count is verified.

Report the status, `restore_completed_safely`, `current_data_intact`, `destination_valid`, top-level
conversation, associated subagent, total transcript, active-project, skill and attachment counts,
restart requirement, and retained evidence. Do not repair, delete, register projects, apply, or
roll back unless the user separately requests the corresponding workflow.
