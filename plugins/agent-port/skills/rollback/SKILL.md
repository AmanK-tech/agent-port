---
name: rollback
description: Safely undo an Agent Port restore from its retained run directory. Use only when the user explicitly requests rollback of a completed or failed restoration.
---

# Roll back with Agent Port

Read [the safety contract](../../references/safety-contract.md).

Run `agent-port --version` first and require `0.5.4` or newer in the `0.5.x` series. Require an
explicit rollback request and the exact retained run directory. Never guess a run directory or
delete restored files manually.

Immediately before rollback, obtain fresh explicit confirmation that the destination harness is
closed. Only then run:

```bash
agent-port restore rollback RUN_DIRECTORY --confirm-harness-closed
```

Respect refusal when current state differs from the recorded post-restore fingerprints. Never force
rollback or overwrite newer user data. Report the rollback result, verification status, preserved
run directory, and any manual follow-up without exposing transcript or suspected-secret content.
