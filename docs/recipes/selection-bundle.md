# Retrieve a Selection as a Bundle

**Need:** Pick up a related screenshot, log, and report together without
requiring their producer to build an archive first.

**Prerequisites:** The files are managed items in the same zone. ZIP download
is enabled for that zone, and the selection fits the configured archive limits.

## Steps

1. Click one history item, then `Ctrl`-click / `Command`-click to add items, or `Shift`-click to select a range.
2. Check the selected filenames in the content panel.
3. Choose `Copy links` if the consumer can read the server-side paths, or `Download ZIP` to retrieve the selected files through the browser.
4. Check the copied reference list or open the downloaded ZIP and confirm that it contains the expected data files.

## Observable Result

One selection produces a formatted list of filesystem references or a ZIP of
the selected files. The items remain separate managed pairs in the zone;
downloading the ZIP does not move or delete them.

## Pitfalls

- Reference lists are text formatted according to zone settings, not public URLs or a guarantee of consumer filesystem access.
- The ZIP contains selected data files, not sidecars or a backup of Pasteberth metadata. Comments do not become task instructions embedded in the downloaded files.
- A selection is not a stored bundle identity or a transaction spanning a workflow. Later retention or deletion can remove individual source items.
- ZIP limits or a busy zone can prevent download. Check the result rather than assuming that clicking the action completed the archive.
- Selection also exposes destructive and transfer actions. Check names and destination before using them; multi-file transfer can partly succeed.

See [using Pasteberth](../using-pasteberth.md),
[optional zone workflow](zone-workflow.md), [API reference](../reference/api.md),
and [backup operations](../operations.md).
