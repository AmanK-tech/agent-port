---
name: doctor
description: Validate a Codex or Claude Code destination or completed Agent Port restore without changing it. Use for diagnostics, integrity checks, and post-restore verification.
---

# Diagnose with Agent Port

Read [the safety contract](../../references/safety-contract.md).

Run `agent-port --version` first and require `0.5.4` or newer in the `0.5.x` series. Use
`agent-port doctor HARNESS_HOME --harness HARNESS --format json` with an explicit path. This
workflow is read-only: do not repair files, apply
a restore, register projects, delete run data, or roll back unless the user separately requests the
appropriate workflow.

Doctor validates the harness generally; it is not migration-specific proof. For a retained Agent
Port run directory, use the `verify` workflow instead.

Summarize integrity, foreign-key and JSONL checks, missing rollout paths, unavailable projects,
warnings, and failures. Do not print transcript content or suspected secret values. If repair would
require replanning or rollback, explain that boundary rather than improvising filesystem or SQLite
changes.
