# Legacy portable Agent Port skill

The repository retains one harness-neutral orchestration skill at
`skills/agent-port/SKILL.md`. It can guide Codex or Claude Code through Agent Port's existing CLI,
but it does not implement migration logic or install the CLI.

This is a compatibility fallback for clients that cannot install plugins. The supported primary
experience is the dual-harness marketplace plugin documented in [plugins.md](plugins.md).

## Install

Install Agent Port first and verify that `agent-port --version` succeeds. Then copy the complete
skill directory into the harness's user-skill root:

| Harness | Default destination |
| --- | --- |
| Codex | `~/.agents/skills/agent-port` |
| Claude Code | `$CLAUDE_CONFIG_DIR/skills/agent-port` when set; otherwise `~/.claude/skills/agent-port` |

Do not overwrite an existing differing directory. Compare it, preserve the destination copy, and
make an explicit update decision. Restart or reload the harness after installation.

To update the wrapper, compare the installed directory with the new repository version and replace
the whole directory only after review. To remove it, delete only the installed `agent-port` skill
directory; this does not uninstall the CLI or change any archives.

## Source-machine workflow

Ask the harness to use Agent Port to:

1. Verify the CLI version.
2. Inspect the Codex or Claude Code home.
3. Inspect skill ownership.
4. Create and inspect an explicit `.agentpack` backup, or explicitly invoke
   `agent-port transfer send` for a same-LAN migration.
5. Keep the pairing code temporary and use only the fallback endpoint printed by the CLI if local
   discovery is blocked.

Treat either archive like a device backup because it can contain source code, conversation data,
filesystem paths, and secrets previously shared in a session.

## Destination-machine workflow

Ask the harness to:

1. Prepare a persistent migration workspace, write the pasted pairing code to the exact returned
   absolute one-use path, run `transfer receive --workspace`, and inspect the received archive
   exactly once.
2. Use the plan's suggested home-root mapping and collect only unresolved path or differing-skill
   decisions.
3. Write an auto-numbered plan and reload the final file through `restore plan-info`.
4. Stop on blockers, harness mismatches, unsupported schemas, or stale inputs.
5. Obtain fresh authorization to arm the reviewed plan, run `restore handoff`, and ask the user only
   to quit the destination harness.
6. When the user returns after restart, automatically run `restore verify` and retain the archive,
   plan, verification result, backup, and rollback directory.

The wrapper must obtain explicit decisions before accepting an unmapped path, skipping or replacing
a differing skill, or registering Codex projects. A generic instruction such as “continue” does not
authorize a restore handoff.

## Rollback

Rollback requires the exact retained run directory and a fresh harness-closed confirmation. Agent
Port refuses rollback if restored state has since changed. The wrapper must not force it or
overwrite newer user data.

## Boundaries

The skill permits only Agent Port's explicitly requested same-LAN transfer. It does not perform
automatic SSH/SFTP, relay or cloud transfer, cross-harness session conversion, credential migration,
repository creation, plugin installation, or archived skill-script execution. The marketplace
plugin supersedes this standalone wrapper for new installations.
