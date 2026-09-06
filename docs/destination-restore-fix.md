# Destination restore diagnosis (0.5.5)

The September 2026 report contained two different outcomes: automatic rollback after a complete
apply, and an older unrolled-back run whose session files were later missing.

## Reproduced automatic rollback

An isolated restore of the reported source data reproduced the first outcome. Before the fix,
all 12 conversations, all 12 transcript files, and all 3 attachments verified. The final count
check nevertheless compared 7 verified projects with 6 expected projects and initiated rollback.

Claude backup counts native project containers. Verification incorrectly counted working-directory
associations. One container can contain several working directories, and the same directory can
occur in several containers. Version 0.5.5 uses source container identities for both counts. The
same archived data now verifies 12 conversations and 6 projects without rollback.

The investigation also found that payload inventory generation assigned the last encountered
working directory to an entire container. Container selection now prefers the structural path
whose encoded name identifies that container. Planning reconstructs this from transcripts even
for older archives. Subagents and tool results follow the parent conversation's destination;
subagents sharing their parent's session ID no longer cause false duplicate-session conflicts.

## Rollback isolation

A sequential failed restore preserves a previous successful restore, including when it appends
to that restore's transcripts. A regression test verifies the earlier run again after rollback.

An overlapping pair of restores exposed a separate race: both could journal the same paths as
missing before either wrote them. One restore could then encounter the other's newly written
file, fail, and delete those files through its own rollback journal. Version 0.5.5 holds an
exclusive operating-system lock throughout apply and rollback to prevent cooperating Agent Port
processes from entering this sequence. The competing restore is rejected before journal creation.
This fix does not establish that overlap occurred in the reported older run.

## What the older run establishes

The older run reports valid apply-time checks and post-restore fingerprints but currently lacks
22 transcripts and 8 of 10 attachments. Its journal does not report rollback, and it has no retained
`initial-verification.json`. That establishes a difference between recorded apply and current
state, not a deletion timestamp or responsible process. Current code writes initial evidence on
successful applies as well as on initial-verification failures; it is not a rollback-only file.

The user clarified that skills were not part of the needed transfer. Its 32 applied operations
equal 22 transcripts plus 10 attachments. The reported skill checks therefore do not establish
a successful skill write or isolate a difference between two writers; the failure concerns
session trees.

Claude also has a documented startup cleanup policy for old session files and tool results.
That is another candidate, not a confirmed explanation for this incident. Compare the affected
files' retained timestamps and the destination's effective `cleanupPeriodDays` with the run's
creation time and any later rollback journals. See Claude's
[application-data cleanup documentation](https://code.claude.com/docs/en/claude-directory#cleaned-up-automatically).

Verification now reports absent initial evidence explicitly and includes exact count mismatches
in failure diagnostics. The original archives and all existing run evidence should be retained;
create a fresh plan with 0.5.5 before retrying.
