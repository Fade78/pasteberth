# Personal Staging

**Need:** Keep an artifact available while you switch applications, machines,
or working sessions. No second participant is required.

**Prerequisites:** A reachable Pasteberth service and an available zone. Know
its retention policy; this is a rolling staging area, not a permanent archive.

## Steps

1. Select a zone for this project or topic.
2. Paste a screenshot or text, or upload a file using drag-and-drop or the file picker.
3. Confirm the history entry. Optionally add a short comment such as "Reference for the settings change."
4. Return later and select the item. Copy supported image/text content into another application, download the file, or copy its reference for a tool that can read the server-side path.

## Observable Result

The same managed item can serve another working context without repeating the
original capture. A local filesystem tool can read the data file; another
machine can retrieve it through an authenticated browser download.

## Pitfalls

- `Copy link` copies a filesystem reference, not a public URL or a mount of the server's storage.
- Browser clipboard permissions can block copy even after a successful deposit. Inspect the copy result or download instead.
- Rich clipboard copy is content-dependent. PDFs and workbooks remain files; HTML copy is sanitized and need not preserve arbitrary formatting.
- Later publication can evict older items through retention. A comment or copied reference does not pin an item.

See [using Pasteberth](../using-pasteberth.md), [concepts](../concepts.md), and
[documents](documents.md).
