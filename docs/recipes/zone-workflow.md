# Use Zones as an Optional Workflow

**Need:** Distinguish artifacts you are collecting from artifacts you want to
inspect or reuse next. A workflow is one possible interpretation of zones,
not a required Pasteberth structure.

**Prerequisites:** Two distinct active zones, for example `Inputs` and
`Review`, with enough target retention for the selected files. Their labels
are conventions chosen by your environment, not built-in stages. The source
items must be managed pairs, and target filenames must be unused.

## Steps

1. Deposit an artifact into whichever zone fits its present context.
2. Select it and add a concise comment such as "Compare the totals with the original; the second page needs attention."
3. When useful, select one or more items in the source zone and choose the target using `Choose destination`.
4. Choose `Copy` to keep the source, or `Move` to remove source pairs after successful target publication.
5. Check the transfer result and both histories. Continue working, copy back, skip a stage, or stop there as the situation requires.

## Observable Result

Successfully transferred items appear in the target with their filenames and
metadata, including comments. Copy retains source items; a successful move
removes their source pairs. New references point into the target zone.

## Pitfalls

- Pasteberth enforces no workflow order, roles, assignments, or approvals. A zone called `Review` does not require a reviewer, and a comment does not trigger a task.
- You can instead organize zones by project or topic, with no stages at all. Interfaces are not assigned to particular participants.
- Occupied target names are rejected rather than replaced. If an operation is partly successful, inspect both zones before retrying; a target may exist even if later source removal failed.
- Target retention applies and can evict transferred items. Several selected files are not an atomic workflow bundle, a durable queue, or a guaranteed handoff.
- Moving invalidates old source paths. Pass the current target reference to consumers and keep important files outside temporary retention.

See [using Pasteberth](../using-pasteberth.md), [concepts](../concepts.md),
[API transfers](../reference/api.md), and [CLI transfers](../reference/cli.md).
