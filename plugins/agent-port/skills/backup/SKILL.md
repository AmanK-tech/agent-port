---
name: backup
description: Inspect and create a verified native Agent Port backup for Codex or Claude Code. Use when preserving sessions, project associations, or user-owned skills.
---

# Back up with Agent Port

Read [the safety contract](../../references/safety-contract.md), then follow
[the source workflow](../../references/source-workflow.md).

Run `agent-port --version` first and require `0.5.5` or newer in the `0.5.x` series. If
unavailable, stop and provide the documented installation command without running it.

Enforce this order:

```text
inspect harness -> inspect skill ownership -> create backup -> inspect archive
```

Use only explicit user-approved paths. Do not overwrite an existing archive implicitly. Create only
the requested `sessions` and `skills` payloads. Summarize counts, eligible skills, exclusions,
warnings, and archive location without printing transcript or suspected-secret content.

After verification, explain that the archive must be protected like a device backup. Offer the
encrypted same-LAN pairing flow only when the user explicitly asks to transfer it. Do not invoke
SCP, SFTP, cloud storage, a relay, or another transfer mechanism.
