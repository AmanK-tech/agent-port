# Destination-machine workflow

1. Verify `agent-port` is `0.5.5` or newer in the `0.5.x` series. Do not discover commands with
   `--help` or improvise after a usage error.
2. Run `agent-port transfer prepare --format json`. Ask for the active pairing code as free-form
   input, write it with the harness file tool to the exact absolute `pairing_code_file` path, and
   run `agent-port transfer receive --workspace WORKSPACE --format json`. Never place the code in a
   command, abbreviate the returned path, or run `chmod`. Preserve a user-supplied archive path with `--output`;
   otherwise retain the default under `~/Agent-Port/Migrations/`.
3. Inspect exactly once with `agent-port inspect ARCHIVE --format json`. Never use `transfer
   inspect` or archive-targeted `skills inspect`.
4. Create the auto-numbered plan with `agent-port restore plan ARCHIVE --destination HARNESS_HOME
   --destination-home DESTINATION_HOME --format json`. Treat exit code 1 plus `status: blocked` as
   a valid saved plan. Present the suggested home-root mapping, any unique Git-clone project
   mappings, and only actual blockers or differing eligible skills. Ask for approval before
   accepting each suggested mapping.
5. Regenerate after every approved decision. Never edit or dump plan JSON.
6. Run `agent-port restore plan-info FINAL_PLAN --format json` and use only its final identifiers,
   mappings, operation counts, content counts, blockers, and skill policies.
7. Obtain fresh authorization to arm that exact plan, run `agent-port restore handoff FINAL_PLAN
   --confirm-quit-to-apply --format json` yourself, and tell the user only to quit the harness and
   wait for the completion notification.
8. When the user returns, automatically run `agent-port restore verify FINAL_RUN_DIRECTORY --format
   json`. If pending, ask them to quit again. Report the safety, current-data, destination, separate
   conversation/subagent/transcript counts, and retained evidence.

Interpret results strictly: `verified` is success; `pending` requires the user to quit again;
`changed` cannot certify current integrity; `failed` is failure; and `rolled-back` means migrated
data is no longer active. Never call incomplete or changed results complete or benign.

Never use `ls`, `cat`, `grep`, `head`, `diff`, manual checksum commands, shell redirection, or shell
pipelines. Never dump transcript, memory, attachment, tool-result, inventory, plan, or command
contents. Cleanup is never automatic.
