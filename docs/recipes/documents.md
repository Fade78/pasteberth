# Stage Documents and Data

**Need:** Make `input.xlsx` and `requirements.pdf` available for work, then
retrieve `analysis.xlsx` and `report.pdf`. The work may be done by the same
person later, another trusted participant, a script, or an agent.

**Prerequisites:** A zone accepting the files' content types and sizes, enough
retention for the intended working set, and tools capable of reading the
documents. Pasteberth does not interpret spreadsheet cells or edit PDFs.

## Steps

1. Upload the input files to the chosen project or topic zone. Check each upload result.
2. Optionally comment on an input: "Use sheet 2; compare totals with the PDF."
3. Retrieve the files by download or pass references to a tool with access to the server-side paths.
4. Complete the work in the appropriate document or data tool, outside Pasteberth.
5. Publish the completed outputs explicitly, using the Web UI, `drop`, API, MCP, or registration of files safely written in place.
6. Confirm the output entries, then download them individually or select them for a ZIP.

## Observable Result

Inputs and outputs are available as managed files in the selected zone. Their
names and short comments provide context; the service does not require a
particular sequence or distinguish who used which interface.

## Pitfalls

- A file being accepted does not imply a document-specific preview or rich clipboard copy action.
- Several uploads are separate operations, not an atomic input/output bundle. Check every result.
- Output files written directly into a directory stay foreign until explicitly registered.
- Retention may remove inputs as outputs arrive. Preserve essential inputs elsewhere and choose fresh output names unless replacement is deliberate.

See [script output](script-output.md), [agent output](agent-output.md),
[registration](register-file.md), and [selection bundle](selection-bundle.md).
