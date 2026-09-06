# Agent Port

Move your Codex or Claude Code conversations and personal skills to another computer, while
keeping the conversations already on your new computer. Agent Port provides a guided plugin
and a local Python CLI for backup, encrypted transfer, restore, verification, and rollback.

**Codex restores into Codex. Claude Code restores into Claude Code.** Your repositories travel
separately through Git or your usual file-transfer method.

## What you need

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/) or
  [pipx](https://pipx.pypa.io/stable/installation/).
- The same Agent Port release on both computers.
- Both computers on the same private network for direct transfer. You can also move an archive
  yourself and restore it offline.
- Codex or Claude Code initialized on the new computer, with your project folders in place.

## Install

Install the CLI on both computers:

```bash
uv tool install "agent-port==0.5.4"
agent-port --version
```

Alternatively, use `pipx install "agent-port==0.5.4"`. If you downloaded the release wheel,
use `uv tool install /path/to/agent_port-0.5.4-py3-none-any.whl`.

Then install the plugin in the harness you use.

**Codex**

```bash
codex plugin marketplace add AmanK-tech/agent-port
codex plugin add agent-port@agent-port
```

Start a new task and ask Agent Port to help you migrate.

**Claude Code**

```bash
claude plugin marketplace add AmanK-tech/agent-port
claude plugin install agent-port@agent-port --scope user
```

Run `/reload-plugins`, then `/agent-port:migrate`. The plugin also provides
`/agent-port:backup`, `/agent-port:restore`, `/agent-port:verify`, `/agent-port:doctor`,
and `/agent-port:rollback`.

The CLI performs the migration. The plugin guides you through its commands and decisions.
See [installation, updates, and removal](docs/plugins.md) for details.

## Move to a new computer

1. **On the old computer:** ask Agent Port to migrate your setup to another computer. It creates
   a native backup and displays a temporary pairing code. Keep the transfer running.
2. **On the new computer:** ask Agent Port to receive the migration and provide that pairing code.
   It receives and checks the archive in a private workspace under `~/Agent-Port/Migrations/`.
   Receiving the archive does not change your Codex or Claude Code data.
3. **Review the restore plan:** confirm where your project folders live on the new computer.
   Review missing projects, conversation conflicts, and any differing personal skills. A blocked
   plan must be resolved before restoration can proceed.
4. **Approve and close the destination harness:** the plugin arms a temporary handoff. Quit Codex
   or Claude Code on the new computer. The worker waits for closure, backs up the destination,
   restores the data, and verifies the result before reporting success.
5. **Reopen and verify:** after the success notification, reopen the harness and ask Agent Port to
   verify the migration. Open a migrated conversation to confirm it resumes as expected.

Your archive, plans, destination backup, and recovery evidence are retained in the migration
workspace until you explicitly request cleanup. Keep them until you have checked the result.

## Back up or restore from the terminal

Inspect and back up a native home:

```bash
agent-port inspect ~/.codex
agent-port backup ~/.codex --output codex-backup.agentpack
agent-port inspect codex-backup.agentpack
```

For Claude Code, use `~/.claude` as the source instead. Inspect personal skills with
`agent-port skills inspect ~/.codex` or `agent-port skills inspect ~/.claude`.

To transfer directly, run this on the old computer:

```bash
agent-port transfer send ~/.codex
```

On the new computer, run the receiver and enter the pairing code at its hidden prompt:

```bash
agent-port transfer receive --output codex-backup.agentpack
```

Create a restore plan, replacing the example project paths with your own:

```bash
agent-port restore plan codex-backup.agentpack \
  --destination ~/.codex \
  --map "/Users/old/Work=~/Developer/Work" \
  --output restore-plan.json

agent-port restore plan-info restore-plan.json --format json
```

After reviewing a ready plan, close the destination harness and apply it:

```bash
agent-port restore apply restore-plan.json --confirm-harness-closed
```

The command prints the backup and run-directory paths. Use that exact run directory to verify:

```bash
agent-port restore verify /path/printed/by/apply
```

See the [restore guide](docs/restore.md) for skill conflict policies, path mapping, handoff,
older archives, and rollback. Rollback requires a closed harness and refuses to overwrite data
that changed after the restore.

## If something goes wrong

| Problem | What to do |
| --- | --- |
| Pairing or authentication fails | Keep the source transfer running and copy its complete current pairing code. If it expired or stopped, restart the sender and use the new code. No account password or SSH setup is needed. |
| The new computer cannot find the sender | Confirm both computers are on the same private network. Add `--host ADDRESS --port PORT` to the receive command using the fallback endpoint printed by the sender. |
| A retry needs the pairing code again | Give the code to the plugin again. Its private code file is intentionally consumed once. A failed receiver attempt does not by itself mean the sender needs restarting. |
| The restore plan is blocked | Review the reported path mappings, missing projects, schema compatibility, or conflicting skills. Resolve the decisions and generate a fresh plan. |
| The handoff is waiting | Quit the destination harness completely. The worker expires after 30 minutes; if it expires, start a new handoff. |
| Verification reports changed or failed | Keep the migration workspace and inspect the reported findings. Do not treat the migration as complete. |

For local diagnostics, run `agent-port doctor ~/.codex --harness codex` or
`agent-port doctor ~/.claude --harness claude-code`.

## What is preserved, and what is excluded

- Native conversations, project associations, supported attachments, and complete personal skill
  directories are preserved. Path mapping changes structural metadata, without rewriting your
  historical messages.
- Existing destination data is checked for collisions. Differing skills require an explicit
  decision. Compatible Claude Code histories with append-only growth retain the longer history.
- Authentication files, caches, logs, plugin-managed skills, and machine-specific runtime files
  are excluded. Reinstall managed plugins through their harness.
- Inspection, backup, and restore work locally without uploading data. Only an explicit transfer
  opens a temporary encrypted LAN listener; Agent Port provides no cloud storage or relay.
- Archives can still contain sensitive conversation content, source code, and absolute paths.
  Store them securely.

**Compatibility:** Codex SQLite restoration remains experimental and supports migration pairs
39→39, 39→40, and 40→40. Claude Code restore requires a matching detected major/minor compatibility
profile. Unknown schemas and incompatible profiles are blocked. LAN transfer supports Agent Port
0.5.4 and newer within the 0.5.x series. These checks do not guarantee support for every future
harness version.

## Contributing and technical documentation

For development, clone the repository and run:

```bash
uv sync --locked --all-groups
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

Tests use synthetic temporary harness homes. They do not inspect your real Codex or Claude Code data.

- [Plugin guide](docs/plugins.md)
- [Restore guide](docs/restore.md)
- [Security model](docs/security.md) and [archive format](docs/archive-format.md)
- [Release checklist](docs/release.md) and [adapter development](docs/adapter-development.md)
- [Legacy standalone skill](docs/agent-skill.md), for harnesses without plugin support
