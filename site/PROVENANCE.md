# Site Provenance

## Current Source

This is the repository integration of the approved **presentation site 2.1**,
not a new Pasteberth release. Its interactive demo uses the repository's
**working-tree frontend, including Unreleased changes**, not the exact published
2.1.21 frontend. Runtime `__version__` and `pyproject.toml` still agree on
**2.1.21**, the latest published version; no release bump is part of this work.
Transfers, asynchronous discovery and `first-directory` labels shipped in 2.1.19.

The Unreleased frontend adds a date to card timestamps when stored `created_at`
is more than 24 hours old, and handles missing or invalid timestamps as
`Unknown time`. `created_at` is preserved across copies and moves between zones;
it is not arrival time in the current zone. This is actual product frontend code,
copied unchanged into the demo, not a preview-only date adjustment.
It also consumes `overview.max_archive_files`, refusing an oversized ZIP selection
with an error toast before form submission. The separate demo seed advertises 64
files per ZIP, and the adapter rejects a larger request before reading file blobs.

The Unreleased scanner reuses filesystem observations within one discovery pass,
not across refreshes. Background discovery remains request-triggered. Visible-tab
polls still run every 10 seconds, but a poll becomes eligible to start a new scan
only after completion plus `max(10 seconds, last full refresh duration)`, including
scan and registry installation. Failed and foreground refreshes also establish
that cooldown; foreground requests can refresh without waiting for it. No hard
filesystem-call timeout or autonomous watcher is added. The memory demo does not
run this scanner or certify its timing; these claims come from the current source,
not published 2.1.21 behavior or daemon screenshots.

Unreleased preview/download GET and HEAD and ZIP requests use the published zone
registry without starting discovery or joining a scan. A short shared filesystem
lock phase captures selected metadata and open payload handles, not whole-history
payloads or unrelated ordinary sidecars. It still enumerates names and reads
transaction journals. Headers, compression and network output occur after zone
locks are released; retained handles allow opened versions to stream alongside
cooperating managed replacement/deletion. Filesystem calls can still block: there
is no direct O(1) lookup, hard response deadline, arbitrary external in-place-write
protection or new native Windows support guarantee.

New archive defaults are 64 files and four active archives per process. HTTP ZIP
acquisition may return `423 zone_busy` on writer contention, whereas a full archive
slot pool returns `503 server_busy` with `Retry-After: 1`. Preview acquisition still
waits for a writer by default. The demo enforces only its advertised file-count
limit, not this slot pool, locking or streaming lifecycle. These are current-source
Unreleased backend changes, not published 2.1.21 behavior or site QA certifications.

`source-manifest.json` pins four byte-for-byte frontend copies and records the
source commit as context. The hashes, rather than a clean-tree assertion, identify
the bytes. `tools/rebuild.py` fails on frontend or version drift until an explicit
review and `--sync-product` refresh. It never edits the product source.
All generated destinations, and all sync-copy/manifest destinations when syncing,
are checked before any writes. Escaping and other symlinked output paths are
rejected. This assumes a trusted workspace without concurrent path replacement,
not an adversarial-filesystem transaction guarantee. MIME mappings for shipped
assets are fixed in the builder rather than taken from the host registry.

| Site content | Current repository reference |
| --- | --- |
| Files, sidecars, registration and publication | `../GUIDE.md`, `../PasteBerth/runtime/storage.py`, `service.py`, `cli.py` |
| Original English product UI | `../PasteBerth/runtime/static/{app.js,style.css,favicon.svg}`, `templates/index.html` |
| Collections, eligibility, labels and IDs | `../PasteBerth/runtime/config.py`, `zone_collection.py`, `../docs/zone-collection-contract.md` |
| Unreleased scanner reuse and completion-based cooldown | `../PasteBerth/runtime/zone_collection.py`, `service.py`; unchanged visible-tab poll interval in `static/app.js` |
| Unreleased card date display | `../PasteBerth/runtime/static/app.js`: `fmtTime(item.created_at)` |
| Unreleased published-registry reads and unlocked streaming | `../PasteBerth/runtime/service.py`, `storage.py`, `webapp.py`, `platformfs/` |
| Unreleased archive budgets and UI count preflight | `../PasteBerth/runtime/config.py`, `service.py`, `static/app.js`; demo limit in `tools/demo-seed.json`, `assets/demo-adapter.js` |
| Upload and retention defaults | `../PasteBerth/runtime/config.py`: 20 MiB upload, collection retain 10 |
| Product status | `../CHANGELOG.md`, runtime `__init__.py`, `../pyproject.toml` |

The site generator deliberately selects `retain = 100` and `first-directory`.
It is a documentation convenience, not a new runtime feature or a substitute for
`pasteberth audit`. Workflow prose describes optional combinations of existing
primitives, not permissions, messaging, task states or enforced processes.

## Imported Design

Source: `work/exchange/pasteberth-v2.1.zip`, SHA-256
`c775a1bae9ade45089780bd1f1eb50d5eaa6872d9e09bb1943e29e651195414e`.
The source archive's README, PROVENANCE, CHANGES and all six Python tools were
reviewed before executing imported scripts. Product guidance came from
`work/exchange/pasteberth-doc-agent-product-notes.md`, checked against current code.

Imported: the presentation HTML/CSS/JS, memory adapter, synthetic seed and six
example files, three historical captures, rebuild/QA tools, license and `.nojekyll`.
The stale frontend copies were replaced from the current repository. Generated
HTML and example data were rebuilt. Old `previews/` and QA results were not imported.
The archive and product notes are not required to rebuild or test this site.

The palette preserves the icon's blue `#4da3ff`, raspberry `#c33d60` and mint
`#36c98b`. System fonts only; no remote fonts, telemetry or automatic remote
requests. Documentation links are explicit navigations to Markdown, with a GitHub
fallback, not dynamically fetched or converted to a portal.

## Historical Screenshots

**The three `assets/interface-*.png` files are historical, not current 2.1.21
screenshots.** Their bytes are pinned in `capture-manifest.json`. They were retained
unchanged from the archive dated **8 September 2026**. That date comes from ZIP
entries; an exact original capture timestamp was not independently established.

The archive reports that these captures used a genuine local daemon and the
frontend displaying 2.1.18, with synthetic Atlas, Orbit and Studio files/sidecars.
It reports Chromium `set_content` with real loopback responses relayed by Python,
not the memory demo adapter. The original product archive's reported SHA-256 is
preserved in the capture manifest. These are inherited provenance statements,
not a new daemon-capture verification. The original daemon capture script/setup
was not included, and no replacement daemon captures were made in this integration.

The hero caption, demo notes and enlargement dialog identify this history.
Rebuilding the interactive demo does not update screenshots. Any new captures must
use a real isolated daemon and synthetic data, record the exact source and method,
and update this provenance and the visible labels together. Do not relabel a
memory-adapter capture as a daemon screenshot.

## Demo And Checks

The separate adapter replaces transport only; copied product sources remain
unchanged and English. Demo data lives in memory. The adapter's upload, comments,
deletion, copy/move and ZIP behavior does not certify production semantics, locks,
transactions, authentication, retention or filesystem guarantees.

Independent review led to site-only corrections: generated/sync output path
validation before writes, guarded data/sidecar names and GNU `cp -T`, preservation
of declared text MIME and anonymous extensions, unique anonymous demo names, and
a quota/conflict recheck after all asynchronous reads/decoding. Replacement quota
uses net byte growth; transfers and final upload commits do not yield. These
changes are in the builder, presentation examples and memory adapter, **not** the
copied product frontend or the daemon. Historical screenshots and their hashes
remain unchanged.

The earlier independent-review regression cases were run against the unfixed site
first and exposed the reported problems. Current evidence includes nine
source/build/allowlist/scratch tests, eight demo regression tests, 70 interaction
checks, 72 language/palette/source checks, 18 parser/discovery checks and 34
static-HTTP checks. See `README.md` for
commands, test-harness corrections and limits; reports pin the tested artifacts.
This refresh adds two scratch-path and two ZIP-count regressions to the prior
207 checks, for 211 passing checks. Historical capture hashes remain unchanged.

Current results belong in `qa/`, with the actual commands and scope in `README.md`.
Explicit QA scratch now defaults to repository `work/tmp/site`, with symlink and
non-directory components rejected before creation. Commands set `TMPDIR` to
repository `work/tmp` for child-process scratch and disable Python bytecode writes.
Old `qa/work/` contents are neither moved nor deleted. Source-confinement fixtures
live in the new scratch location; refused symlink builds must still make no writes,
even inside the fixture. Real generated assets and product copies stay in `site/`.
Optional generated `previews/` are ignored and depict the **site and simulated
demo**, not a new daemon run. No private screenshots or identifiers were imported.
No deployment, publication, commit or backend modification is part of this work.

`DEPLOYMENT.md` warns that a loopback server rooted at the checkout exposes private
files to local clients. It provides a fresh, explicit `publish-files.txt` export
recipe instead of publishing the checkout. That allowlist was validated, but the
shell export recipe was not executed or published in this review. Navigation in
an export remains plain Markdown; no HTML documentation portal is generated.

## License

The product frontend and distributed site retain the project's
AGPL-3.0-or-later licensing. `LICENSE-Pasteberth.txt` is the unmodified license text.
Unminified frontend copies, template, adapter and build tools are included.
