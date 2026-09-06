# Security and privacy

Agent Port is local-first. Inspection, backup, and restore do not require a network and never
execute transcript content or skill scripts.

Adapters build payloads from explicit allowlists. Credentials, settings that may contain secrets,
logs, caches, plugin installations, telemetry, feedback bundles, sockets, and temporary runtime
state remain excluded.

Skill inspection warns about credential-like filenames and fields, absolute paths, malformed
frontmatter, missing license metadata, and unsafe symlinks. Warnings do not print possible secret
values. Symlinks must resolve inside the selected skill directory; broken and escaping links make
the skill ineligible for backup.

Conversation archives may still contain sensitive source code, command output, personal data,
filesystem paths, or secrets previously shared with an agent. `.agentpack` files should therefore
be handled like encrypted device backups. Encryption is not part of format version 2.

## Transfer boundary

`transfer send` creates a verified archive in a permission-restricted temporary directory, derives
a single-use SSH identity and separate discovery/authentication values from a random 128-bit pairing
secret, and opens a temporary LAN listener. Zeroconf discovery records are authenticated before the
receiver trusts their endpoint or host-key fingerprint. The receiver pins that fingerprint before
sending the derived SSH password.

The SSH server accepts only private, link-local, or loopback peers and one fixed binary transfer
command. It rejects shells, PTYs, arbitrary commands, SFTP access, environment injection,
forwarding, concurrent receivers, expired codes, and further authentication after five failures.
It stops after verified acknowledgement or ten minutes by default. No daemon, OS SSH configuration,
relay, account, cloud store, or firewall modification is involved.

`transfer receive` writes to a mode-`0600` partial file on POSIX systems, verifies the declared byte
count and whole-archive SHA-256, performs the normal `.agentpack` member verification, and publishes
without overwriting an existing path. Windows does not expose POSIX permission bits, so received
files inherit the destination directory's access-control list. Interruption removes the partial
file. Receipt never creates a restore plan or mutates a harness.

Plugin-guided receipt first creates a mode-`0700` workspace under `~/Agent-Port/Migrations/` and an
authorized one-use pairing-code path without precreating the file. The code is intentionally
visible in source CLI, tool, and chat output, but is never placed in a command, process argument,
Agent Port diagnostic log, or migration state. The harness writes only to the returned absolute
path. The receiver verifies the private same-user workspace and regular file, restricts the open
descriptor to mode `0600`, reads at most 256 bytes, and unlinks it immediately on success or
failure. Files outside a prepared private workspace still require mode `0600` before opening.

## Restore boundary

Restore planning uses `restore plan`, and `restore plan-info` reloads its compact canonical summary.
Direct CLI apply accepts only that saved plan, rechecks its preconditions, and requires
`--confirm-harness-closed`.

Plugin-guided restore uses `restore handoff` instead of trusting a closed-harness assertion from an
active harness. After explicit approval, a permission-restricted worker waits for all same-user
target-harness processes to close and monitors for reopening throughout apply. It expires after 30
minutes, opens no network listener, and exits after success or failure.

Before the first harness mutation, apply creates a verified destination `.agentpack` and a durable
rollback journal. Files are staged separately, replacements use same-filesystem atomic renames,
and Codex metadata merges run inside a SQLite transaction. Failed apply attempts automatically
restore journaled paths. A complete initial verification runs while the harness remains closed;
any failed boolean or incomplete content count triggers rollback before a success notification.

Manual rollback checks post-restore fingerprints first. It refuses when the user or harness has
changed restored state since apply completed. Path mappings affect only declared structural fields;
Agent Port does not rewrite arbitrary prompts, messages, command output, or tool output.

Claude compatibility uses all structurally observed versions normalized to major/minor. Patch-only
differences are accepted. Append-only session merges are journaled, fingerprinted, verified, and
rolled back like replacements; any non-prefix divergence remains blocking.

Automated tests operate only on synthetic homes created under pytest temporary directories.
