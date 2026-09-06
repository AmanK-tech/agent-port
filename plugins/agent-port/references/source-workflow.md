# Source-machine workflow

1. Run `agent-port --version` and require `0.5.4` or newer in the `0.5.x` series.
2. Inspect only the requested harness home.
3. Inspect skill ownership and explain exclusions.
4. For a normal backup, create the requested `.agentpack` at an explicit path and inspect it.
5. For an explicitly requested same-LAN migration, run `agent-port transfer send HARNESS_HOME`
   exactly and clearly show the temporary pairing code and fallback endpoint. Tell the user
   to provide the code to the Agent Port plugin on the destination; do not instruct them to type the
   receive command themselves.
6. Never initiate SCP, SFTP, cloud storage, a relay, or another ad hoc network mechanism.
