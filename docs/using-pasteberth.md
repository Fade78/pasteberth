# Using Pasteberth

Use this guide once a service and at least one zone are available. For a new
installation, start with [deployment](deployment.md). For the storage model,
read [concepts](concepts.md).

## Choose a Zone

Open the service URL and sign in if authentication is enabled. Groups can show
all zones or a subset, in an area or tab layout. Changing views does not copy
files or change their permissions.

Select the zone you intend to paste into. Keyboard focus on an action or history
item does not silently change that paste target. A file dropped directly on a
zone goes to that zone, regardless of the current paste target.

Zones can separate projects, topics, or stages. For serial development across
several projects, use each project's exchange zone to keep its inputs and
results apart; there is no requirement to work on all projects at once.

## Put an Artifact In

1. Select the intended paste target, then press `Ctrl+V` or `Command+V` for clipboard content.
2. Alternatively, drag files onto a zone or use its file picker.
3. Check the upload result and the zone history. Several files are uploaded sequentially as independent operations; a failure does not cancel the others.

An upload can fail because of size, content policy, free space, a busy zone, or a
filename conflict. Replacing a managed filename requires explicit confirmation;
a foreign file is not overwritten. The UI also asks before an upload would
exceed retention, but the server decides which items are removed.

After publication, the UI tries to copy the returned reference. Browser
clipboard permission can prevent this even when the file was saved. Check the
copy status and use `Copy link` to retry. Anonymous uploads, including clipboard
pastes, can return an existing identical managed item in the same zone rather
than add another one. Preserved filenames are not deduplicated that way.

Tools can publish through [CLI, API, or MCP](integrations.md). A producer that
already writes into the target directory must explicitly
[register the file](recipes/register-file.md); ordinary files alone do not
appear in the managed history.

## Pick It Up Again

Select an item in the history to inspect it and see its available actions.

**Unreleased (runtime version still `2.1.21`):** the selected item's time gains
a date once its stored `created_at` is more than 24 elapsed hours old, not just
because midnight has passed. Dates and times use the browser's local timezone
and the unchanged `en` locale. Tooltips and multiple-selection rows already
show full dates. This changes the display, not the stored timestamp.

| Need | Action and limit |
|---|---|
| Give a local tool a path | `Copy link` copies a formatted filesystem reference, not a public web link. The tool needs access to the same server-side path. |
| Bring a file to another machine | Download it through the browser. This retrieves the stored file rather than granting filesystem access. |
| Reuse an image in another application | Use the image copy action when available. Browser permissions and image decoding can affect the result. |
| Reuse text or HTML clipboard content | Use the text copy action. Rich HTML requires browser support and may fall back to plain text. |
| Reuse several files | Select them and copy their references or download a ZIP. |

PNG, JPEG, and WebP receive previews when structural validation succeeds. Valid
UTF-8 without NUL bytes is treated as text; other accepted files are opaque
binary. A PDF or workbook can be staged and downloaded without Pasteberth
interpreting its pages or cells.

For stored HTML, the normal copy action sanitizes the HTML clipboard flavor,
removing scripts, event handlers, CSS, forms, remote URLs, and non-raster
resources. Embedded raster data images can remain. The preview shows sanitized
text, not rendered stored HTML. When sanitization changes content, the UI offers
an explicit `Copy raw HTML` action. Use that only when you intend to pass the
unsanitized source onward. Storage and downloads retain the original file;
arbitrary web formatting is not guaranteed to survive a clipboard round trip.

## Add Context

Select one item and choose `Comment` or `Edit comment`. A short instruction can
make a later return easier: "Use the second worksheet; totals exclude tax."
Save with the form button or `Ctrl+Enter` / `Command+Enter`; an empty comment
clears the note. Default limits are 280 Unicode characters and 1 KiB of UTF-8,
with unsafe code points rejected; operators can configure the limits.

Comments accompany artifacts. They do not assign work, trigger execution, or
record an approval process.

## Work With a Selection

1. Click a history item to select it.
2. Use `Shift`-click for a range or `Ctrl`-click / `Command`-click to add or remove items within the zone.
3. Check the selected filenames before choosing `Copy links`, `Download ZIP`, or `Delete selected`.
4. To transfer, use `Choose destination`, then `Copy` or `Move`.

Copy leaves the source items in place. A successful move publishes target pairs
before removing their source pairs. Transfers preserve filenames and metadata,
including comments and `created_at`, and reject occupied target names rather
than replace them. The displayed creation time is not arrival time in the
destination zone; transfers do not add an arrival timestamp.
The target's retention and free-space rules apply. A batch can partly succeed;
read the result and inspect both zones before retrying. Selection does not make
the files one atomic bundle.

ZIP download must be enabled for the zone and is subject to archive limits. It
retrieves selected data files, not a managed-pair backup with sidecars. Deleting
managed items removes the data and sidecar, so download anything you need first.

**Unreleased (after `2.1.21`):** ZIP defaults cap each selection at 64 files and
the server at four active archives per process, alongside the existing 256 MiB
source-byte and 300-second streaming limits. Once acquired, downloads serve
open versions without zone locks, allowing managed writes, including replacement
or deletion of those files, to proceed. Acquisition can still wait on storage;
ZIP writer contention returns `423`, while full archive capacity returns `503`.
This does not protect against external in-place edits. Check download completion
before relying on the result; see the [bundle recipe](recipes/selection-bundle.md).

## Refresh and Retention

Visible browser tabs refresh every 10 seconds; hidden tabs refresh when made
visible. An item published by another client may therefore take a refresh to
appear. `NEW` markers belong to the current browser tab, clear as items are
selected, and reset on reload. They are not delivery receipts.

Collection zones appear after a scan observes them and a subsequent refresh
reads the completed registry. **Unreleased:** overview polls reuse that registry
during a cooldown of the greater of 10 seconds or the last full refresh
duration, measured from completion. The next eligible poll can start one
background job. Mutations, directory resolution, and explicit history reads
bypass that cooldown or wait for a running refresh. Downloads instead use the
published registry without starting or joining a scan: a new zone stays unknown
until published, and removal takes effect through a later publication.
This is polling, not instant filesystem notification or a
response-time guarantee; overview history and free-space reads can still block.
If an expected zone or registered file stays missing, use
[troubleshooting](troubleshooting.md) rather than repeatedly writing the file.

Retention limits the managed history and can delete older items after service
publication. A copied reference does not pin its file. See
[operations](operations.md) for retention and backups.

## Recipes

- [Personal staging](recipes/personal-staging.md): return to an artifact from another working context.
- [Documents and data](recipes/documents.md): stage PDFs, workbooks, and outputs without a document-specific integration.
- [Register an existing file](recipes/register-file.md): publish metadata without uploading or rewriting the data.
- [Script output](recipes/script-output.md): give ordinary tool output a browsable UI through explicit publication.
- [Agent output](recipes/agent-output.md): publish through the same interfaces available to other tools.
- [Project zones](recipes/project-zones.md): discover exchange points as environments are created.
- [Optional zone workflow](recipes/zone-workflow.md): move artifacts between freely chosen stages.
- [Current result](recipes/current-result.md): replace a named managed result explicitly.
- [Selection bundle](recipes/selection-bundle.md): retrieve a working set as references or a ZIP.
