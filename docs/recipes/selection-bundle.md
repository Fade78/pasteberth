# Retrieve a Selection as a Bundle

**Need:** Pick up a related screenshot, log, and report together without
requiring their producer to build an archive first.

**Prerequisites:** The files are managed items in the same zone. ZIP download
is enabled for that zone, and the selection fits the configured archive limits.

**Unreleased (after `2.1.21`):** defaults allow 64 files per ZIP and four active
ZIP acquisitions/transfers across all zones in one server process. The existing
limits remain 256 MiB of selected source bytes and 300 seconds of streaming.
Operators can change these [budgets](../reference/configuration.md#operational-budget-defaults).

## Steps

1. Click one history item, then `Ctrl`-click / `Command`-click to add items, or `Shift`-click to select a range.
2. Check the selected filenames in the content panel.
3. Choose `Copy links` if the consumer can read the server-side paths, or `Download ZIP` to retrieve the selected files through the browser.
4. Check the copied reference list or open the downloaded ZIP and confirm that it contains the expected data files.

## Observable Result

One selection produces a formatted list of filesystem references or a ZIP of
the selected files. The items remain separate managed pairs in the zone;
downloading the ZIP does not move or delete them.

**Unreleased:** once acquisition succeeds, the ZIP serves retained source
versions without holding zone locks. Other managed operations can replace or
delete even those source filenames while the ZIP streams. This version stability
applies to cooperating managed writes, not arbitrary external in-place edits.
Selecting items in the browser does not acquire their versions; acquisition
happens when the server handles the download request.

## Pitfalls

- Reference lists are text formatted according to zone settings, not public URLs or a guarantee of consumer filesystem access.
- The ZIP contains selected data files, not sidecars or a backup of Pasteberth metadata. Comments do not become task instructions embedded in the downloaded files.
- A selection is not a stored bundle identity or a transaction spanning a workflow. Later retention or deletion can remove individual source items.
- ZIP limits or a busy zone can prevent download. **Unreleased:** too many files returns `413 too_large`; an exclusive writer at acquisition returns `423 zone_busy`; all archive slots occupied returns `503 server_busy`. Honor `Retry-After: 1` for contention and check that the downloaded ZIP is complete, especially after timeout or disconnect.
- **Unreleased:** downloads do not start or wait for discovery. A new zone is unknown until a refresh publishes it; later registry publication records loss of membership. This does not guarantee immediate acquisition or fast filesystem I/O.
- Selection also exposes destructive and transfer actions. Check names and destination before using them; multi-file transfer can partly succeed.

See [using Pasteberth](../using-pasteberth.md),
[optional zone workflow](zone-workflow.md), [API reference](../reference/api.md),
and [backup operations](../operations.md).
