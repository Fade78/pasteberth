# Documentation Maintenance

This documentation describes runtime **2.1.21**. A useful recipe is a promise
that its stated prerequisites, commands, and observable outcome match the
implementation. Verify that promise before shortening it into site copy.

## Ownership

| Material | Purpose and editing rule |
|---|---|
| [README](../README.md) | Product introduction, representative uses, first trial, and orientation. Keep exact contracts in references. |
| [Guide](../GUIDE.md) | Task map and legacy guide anchors. Preserve the old headings as pointers when moving content. |
| [Concepts](concepts.md), [usage](using-pasteberth.md), [recipes](recipes/personal-staging.md) | Explain the model and complete tasks, including prerequisites, observable outcomes, and relevant limits. |
| [Integrations](integrations.md), [provisioning](provisioning.md) | Help tool authors and environment builders choose and verify an approach. |
| [Deployment](deployment.md), [operations](operations.md), [troubleshooting](troubleshooting.md) | Installation and service lifecycle, separate from using an existing instance. |
| [References](reference/configuration.md), [collection contract](zone-collection-contract.md) | Exact settings, supported operations, failure behavior, and storage boundaries. |
| [Bundle help](../PasteBerth/support/README.md) | Short standalone aid shipped with the code-only deployment. |
| [Presentation site](../site/README.md) | Bilingual introduction, demonstrations, examples, and links to canonical Markdown. Not an independent behavior specification. |

Keep lists flat and examples concrete. Explain an artifact with familiar files
before relying on the term. Do not assign interfaces to humans or agents.
Zones may represent projects, topics, or freely chosen stages. Workflow and
short comments are useful compositions, not the product's main purpose or
evidence of task management, approval, messaging, or access-control features.

## Evidence Map

Use these implementation paths and tests to review a claim. Tests exercise
specific cases; a passing suite is not a proof of all filesystem or browser
behavior. Resolve contradictions by reading the current implementation and
running a focused probe, not by trusting an older paragraph.

| Claim or boundary | Implementation | Existing regression coverage |
|---|---|---|
| Accepted content, size limits, named publication | `runtime/service.py`: `_prepare_upload`, `_store_prepared_upload`; `runtime/content.py`, `runtime/images.py` | `tests/test_webapp.py`, `tests/test_content.py`, `tests/test_images.py` |
| `drop` always calls daemon; `register` is local and leaves data unchanged | `runtime/cli.py`: `_cmd_drop`, `_cmd_register_path`; `runtime/storage.py`: `_register_named` | `tests/test_cli.py`, `tests/test_storage.py` |
| Collection eligibility, IDs, labels, and groups | `runtime/zone_collection.py`, `runtime/config.py` | `tests/test_zone_collection.py`, `tests/test_config.py` |
| Request-triggered scans and busy overview behavior | `runtime/service.py`: `_refresh_zone_collections`, `overview`, `history` | `tests/test_zone_collection.py`, `tests/test_webapp.py` |
| Transfer metadata, partial outcomes, and retention | `runtime/service.py`: `transfer`; `runtime/storage.py`: `apply_retention` | `tests/test_transfer.py`, `tests/test_storage.py` |
| MCP offers only the `drop` application tool | `runtime/mcp.py`: `DROP_TOOL`, `McpServer._dispatch`; `runtime/cli.py` | `tests/test_mcp.py`, `tests/test_cli.py` |
| Shared authentication, password rotation, and request boundaries | `runtime/auth.py`: `SessionStore`; `runtime/webapp.py`; `runtime/cli.py`: server setup | `tests/test_auth.py`, `tests/test_webapp.py` |
| Clipboard sanitization and explicit raw copying | `runtime/static/app.js`: `sanitizeHtml`, `copyHtmlContent` | `tests/test_frontend.py`, `tests/e2e/app.spec.js` |
| Runtime version and supported platforms | `runtime/__init__.py`, `pyproject.toml`, `runtime/platformfs/`, `.gitlab-ci.yml` | Platform tests distinguish Linux, Wine, and opt-in native jobs |

`runtime/...` paths are relative to `PasteBerth/`. Verify names and references
when code moves. The initial product brief and site archive informed the
editorial direction, but are not required to build or test documentation.

## High-Risk Wording

- **Discovery versus registration:** collections expose eligible directories; they do not publish every file in them or create project directories.
- **Local versus daemon policy:** `register` uses its selected local validation. It does not enforce the daemon's zone retention or per-zone free-space reserve.
- **Snapshot:** the overview uses a registry snapshot and per-zone history reads, not an atomic content snapshot across zones. A busy zone's empty history is not proof of deletion.
- **Retention:** the current publication is protected during its own retention pass. Later publications may evict earlier items, including transfers in the same batch.
- **Metadata:** a digest is not a signature, source metadata is not authenticated identity, and `changed_at` is currently null rather than a usable modification timestamp.
- **Replacement and transfer:** a stable name is not versioning. A multi-file operation may partly succeed; a target may be published before a subsequent step fails.
- **Security:** groups do not isolate users. Direct-drop routes have an immediate-loopback-peer exception; a public loopback proxy needs the documented access policy.
- **HTML:** ordinary rich copying is sanitized, but explicit raw copying and downloads preserve original content. The receiving application has its own behavior.
- **Support:** Linux validation, Wine coverage, native platform support, browser simulation, and deployed-host checks are different evidence.

## Review Procedure

1. Fix the runtime version being documented and inspect release metadata. Do not infer release status from a screenshot or the site's own version number.
2. Trace each changed capability to the implementation, its callers, and relevant tests. Record gaps instead of extending guarantees by analogy.
3. Exercise copyable CLI, HTTP, and TOML examples against disposable data. Identify which machine runs a command and substitute only declared paths, credentials, ports, and hostnames.
4. Check failure paths that matter: occupied names, unsupported formats, missing permissions, partial transfers, retention, or a busy zone. Do not test destructive examples against a live user zone.
5. Update the reference first, then guides, recipes, README, site copy, and generated outputs. Keep introductory wording brief but compatible with the exact contract.
6. Run link/example tests and relevant product tests. Request an independent review of claims, not only spelling and layout.
7. State the checks actually run and their limitations. Never reuse historical QA success as evidence for changed artifacts.

The documentation examples use illustrative paths and hosts. Authentication
secrets must be injected, not checked in. A guarded shell copy rejects common
collisions but does not reserve filenames against concurrent external writers;
prefer daemon publication when its policy and coordination are required.

## Checks

From the repository root:

```sh
python3 -m unittest discover -s tests -p test_documentation.py -v
npm run test:all
```

The documentation tests check local Markdown links/fragments, navigation,
versioned examples, collection configuration, and safe registration against
disposable files. They do not execute every fenced shell command or certify
deployment permissions. Product tests run Python and Chromium by default;
run `E2E_BROWSER=firefox npm run test:e2e:all` for Firefox as well.

For the site, follow the complete commands and dependency setup in
[site maintenance](../site/README.md). Rebuild before browser QA; the native
configuration check consumes the snippets generated by browser QA. Source
tests check frontend equality, deterministic generated bytes, output-path
guards, historical capture hashes, and publication allowlists. Browser checks
cover the memory demo, not daemon transactions or native clipboard integration.

## Site And Publication

The canonical manual is English. The presentation is English by default with
selectable French; changes to its static and dynamic text must update both.
This does not mean the product UI or full technical manual is translated.

Review runtime changes before using `site/tools/rebuild.py --sync-product`.
The four copied product files must remain byte-identical to the repository
frontend; adapt the demo transport separately. Keep original examples and
site sources distinct from generated `demo.html`, `preview.html`, and
`assets/example-data.js`.

Screenshots have separate provenance. The imported 2.1.18 captures are labelled
historical; the interactive demo uses the 2.1.21 frontend and a memory backend.
Do not relabel screenshots when rebuilding JavaScript. New genuine-product
captures need an isolated daemon, synthetic data, and recorded source/method.

Never publish the full checkout: ignored directories, credentials, and private
files are not protected by Git ignore rules when served over HTTP. Follow the
[allowlisted site export](../site/DEPLOYMENT.md), review new entries in
`site/publish-files.txt`, and validate the resulting public files. Markdown
links remain Markdown; no HTML manual is generated automatically.

Documentation work does not authorize deployment, publishing, tagging, or a
new product version. Use the [release process](release-process.md) when those
actions are explicitly requested.
