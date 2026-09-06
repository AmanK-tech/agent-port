# Plugin listing kit

## Positioning

**Name:** Agent Port

**Short description:** Safely migrate native Codex and Claude Code sessions and skills.

**Long description:** Agent Port is a local-first guided workflow for inspecting, transferring,
backing up, restoring, verifying, and rolling back native coding-agent sessions and user-owned skills. It keeps
Codex data in Codex and Claude Code data in Claude Code, excludes credentials, and requires a saved
restore plan before mutation.

**Category:** Productivity / developer tools

**Keywords:** backup, migration, Codex, Claude Code, sessions, skills, local-first

## Reviewer notes

- The plugin requires the separately published `agent-port` Python CLI.
- It never installs or upgrades dependencies automatically.
- It contains no hooks, MCP servers, apps, monitors, or bundled executables. The CLI can launch one
  temporary, expiring handoff worker after explicit restore approval.
- It transfers archives only through an explicit, temporary, pairing-code-authenticated same-LAN
  command; it has no hosted service, relay, or cloud upload.
- Plugin apply requires fresh handoff authorization and objective harness-process closure. Direct
  apply and rollback require fresh confirmation that the destination harness is closed.
- Cross-harness resumable-session conversion and credential migration are explicitly refused.

Validate locally before submission:

```bash
python tools/validate_plugins.py
claude plugin validate plugins/agent-port --strict
```

Anthropic community marketplace submission is a post-release distribution action, not a prerequisite
for repository marketplace installation. Codex workspace sharing is optional and does not imply
public directory publication.
