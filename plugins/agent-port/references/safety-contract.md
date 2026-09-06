# Agent Port safety contract

Agent Port is local-first and restores native sessions only into the harness that created them.

Never:

- Convert Codex sessions into Claude Code sessions or the reverse.
- Copy authentication files, API keys, credentials, caches, logs, or managed plugin skills.
- Print archived transcript bodies, prompts, tool-result content, command-result content, or
  suspected secret values.
- Put a pairing code in a command, process argument, Agent Port diagnostic log, Agent Port-generated
  summary, or persisted migration state. The temporary code is intentionally visible in
  user-facing CLI, tool, and chat output so the user can transfer it between machines.
- Probe migration commands with `--help`, shell pipelines, or raw file-dump utilities.
- Execute archived skill scripts.
- Create missing repositories or silently accept unmapped structural paths.
- Upload an archive or transfer it beyond the local network.
- Start `agent-port transfer` without an explicit user request on both machines.
- Apply an archive directly; a saved, current, blocker-free plan is mandatory.
- Pass `--confirm-harness-closed` without a fresh, explicit closed-harness confirmation for a
  direct apply or rollback.
- Arm a plugin restore handoff without fresh, explicit authorization to apply only after Agent Port
  verifies that every destination harness process has closed.
- Force a rollback when Agent Port reports that restored state has changed.
- Delete received archives, plans, run directories, backups, or rollback evidence automatically.

Use explicit paths, keep the operation inside the user's requested source and destination, and stop
when a harness, schema, version, mapping, collision, or fingerprint check blocks the workflow.

Plugin-guided apply must use the temporary restore handoff. The worker independently verifies
same-user harness process closure before and throughout apply. A reopened harness must prevent apply
or trigger automatic rollback. The handoff expires and never becomes a persistent daemon.

An `.agentpack` may contain source code, personal data, filesystem paths, and secrets previously
shared in a session. Treat it like a device backup. Agent Port's transfer command is permitted only
for an explicitly requested, pairing-code-authenticated same-LAN transfer; receipt never authorizes
restore or any harness mutation.

Plugin-guided receipt uses a persistent workspace under `~/Agent-Port/Migrations/` unless the user
chooses another archive path. The user may paste the temporary code into chat, where the harness may
retain it in its transcript. The plugin writes it only to Agent Port's authorized one-use path in a
private workspace; Agent Port restricts and deletes the file immediately after reading.
