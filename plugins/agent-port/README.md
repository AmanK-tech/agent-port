# Agent Port plugin

Agent Port is a local-first guided interface for the `agent-port` CLI. It helps Codex and Claude
Code inspect, transfer over the same LAN, back up, restore, verify, and roll back their own native
sessions and user-owned skills. The plugin guides the workflow; the CLI performs the transfer and
other migration operations. On a destination machine, the plugin asks for the temporary pairing
code and runs the receiver itself; users do not need to paste a receive command into a separate
terminal. It does not convert sessions between harnesses.

The plugin requires `agent-port>=0.5.5,<0.6` on `PATH`. It never installs the CLI automatically.

See the repository [README](https://github.com/AmanK-tech/agent-port#readme) for marketplace
installation, security boundaries, and the old-machine/new-machine workflow.
