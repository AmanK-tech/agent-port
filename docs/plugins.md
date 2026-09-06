# Agent Port plugins

Agent Port ships one plugin bundle with separate Codex and Claude Code manifests. Both harnesses use
the same workflow skills and safety contract; the `agent-port` CLI remains the sole migration
engine.

## Customer setup

Install Python 3.11 or newer and the CLI:

```bash
uv tool install agent-port
agent-port --version
```

For Codex:

```bash
codex plugin marketplace add AmanK-tech/agent-port
codex plugin add agent-port@agent-port
```

Start a new thread after installation or update.

For Claude Code:

```bash
claude plugin marketplace add AmanK-tech/agent-port
claude plugin install agent-port@agent-port --scope user
```

Run `/reload-plugins`. The available skills are `/agent-port:migrate`,
`/agent-port:backup`, `/agent-port:restore`, `/agent-port:verify`, `/agent-port:doctor`, and
`/agent-port:rollback`.

## Migration journey

Install the CLI and plugin on both machines. On the old machine, invoke migrate to inspect the
harness, classify skills, and start the sender. On the new machine, invoke migrate and provide the
temporary pairing code when asked. The plugin creates a retained workspace under
`~/Agent-Port/Migrations/`, writes the code to Agent Port's protected one-use file, and runs the
receiver without putting the code in a command. It inspects the archive once, creates auto-numbered
plans, reloads the final plan through `restore plan-info`, and asks only for actual mapping or
differing-skill decisions. It then arms the handoff, asks the user only to quit, and verifies the
completed migration automatically after restart.

If a project was cloned into a different folder on the destination, Agent Port matches its origin
remote and suggests a `SOURCE=DESTINATION` mapping when the match is unique. Approve the
suggestion before regenerating the plan. The pairing flow is same-LAN only. If discovery is
blocked, use the `--host` and `--port` endpoint printed by `transfer send`. Ad hoc SSH/SFTP,
relays, cloud transfer, firewall changes, and cross-harness session conversion are not supported.

## Local development

Codex can add the repository checkout as a local marketplace:

```bash
codex plugin marketplace add /absolute/path/to/agent-port
codex plugin add agent-port@agent-port
```

Claude Code can validate and load the bundle directly:

```bash
claude plugin validate ./plugins/agent-port --strict
claude --plugin-dir ./plugins/agent-port
```

Run `/reload-plugins` after Claude plugin changes. Start a new Codex thread after reinstalling an
updated local plugin.

## Updates and removal

Update the configured marketplace, then reinstall or update the plugin using the harness plugin
manager. Removing the plugin does not uninstall the CLI and never removes archives, plans, or run
directories. Removing the CLI does not remove the plugin.

If the plugin reports that `agent-port` is missing or incompatible, install or upgrade the CLI
explicitly. The plugin will never do this automatically.

## Trust boundary

The plugin contains instructions, references, images, and manifests only. It has no hook, MCP
server, app integration, installer, or bundled executable. The CLI may launch one temporary,
expiring restore handoff worker after explicit approval. Git origin matching uses one-way
fingerprints only; remote URLs and credentials are not archived. All destination mutation remains
in the schema-gated CLI after saved-plan, preflight, and process-closure checks.
