# Privacy

Agent Port operates locally. The plugin does not provide a remote service, telemetry endpoint,
cloud store, MCP server, relay, or account system. It does not upload archives or harness data.

The CLI reads only the source, archive, destination, and user-home paths selected for the requested
operation. Normal reports omit transcript bodies and detected secret values. Authentication files,
caches, logs, and machine-specific state are excluded from supported backups.

An `.agentpack` can still contain source code, conversation data, paths, and secrets previously
shared in a session. Users are responsible for storing and transferring archives securely.

Only an explicitly invoked `agent-port transfer` command opens a temporary listener. It accepts a
single pairing-code-authenticated connection from a private, link-local, or loopback address,
transfers the archive over SSH, and removes its temporary archive and key material after verified
receipt or timeout. Local discovery advertises only protocol metadata, expiration, a random session
identifier, an ephemeral host-key fingerprint, and an authentication tag. It never advertises
archive names, harness identity, paths, content, or archive size.

Questions and security reports can be submitted through the repository's GitHub issue tracker.
