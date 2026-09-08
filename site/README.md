# Pasteberth Presentation Site

Static presentation **site 2.1**, integrated with the repository's **2.1.21 runtime
frontend**. English by default, French selectable; the actual product UI remains
English. No framework, backend changes, telemetry, remote fonts or deployment.

## Serve

For offline review, open `site/preview.html`. For HTTP review or deployment,
prefer a fresh [allowlisted export](DEPLOYMENT.md), which preserves the public
documentation layout without serving private checkout files.

**Never proxy or publish the full checkout.** Even a loopback-only server exposes
`.git`, `work/`, configurations, ignored files and other checkout contents to local
clients that can reach its port. Loopback binding is not authentication or an
allowlist; filesystem symlinks can expose still more files. The following command
is only a short-lived convenience on a trusted local machine with a reviewed
checkout, not a safe publication recipe:

```sh
python3 -m http.server 8080 --bind 127.0.0.1
```

Open `http://127.0.0.1:8080/site/` and stop the server after review. In an export,
preserve `site/` alongside the allowlisted guide/docs files, not inside a copy of
the whole repository. Links to `../GUIDE.md` and `../docs/*.md` are real Markdown
files, not HTML portal routes: GitHub renders them, while a static server may
display or download their source. The documentation section includes a GitHub
fallback. Hosting only `site/` requires revising those relative documentation links.

`preview.html` is an optional self-contained viewing artifact with the demo and
assets embedded. Open it in a full browser, not an attachment viewer that disables
scripts. Its relative documentation links still require the repository layout;
the GitHub link is available when browsing a detached copy. `index.html` is the
preferred, smaller modular entry point.

## Edit And Rebuild

Sources are `index.html`, `assets/site.css`, `assets/site.js`, the separate
`assets/demo-adapter.js`, `tools/demo-seed.json`, and `assets/examples/`.
The four files under `assets/product/` are **unchanged runtime copies**. Do not
patch them to make the demo work; adapt the transport separately.

```sh
python3 site/tools/rebuild.py
```

This stdlib-only command works from any directory and rebuilds `demo.html`,
`assets/example-data.js` and `preview.html`. It checks runtime/package version
agreement and byte equality with `source-manifest.json` and repository frontend
sources. It fails rather than silently using stale frontend files.

After reviewing a runtime change, update the site's version claims and examples,
then refresh copies and hashes explicitly:

```sh
python3 site/tools/rebuild.py --sync-product
```

The manifest's commit is source context, not proof of a clean worktree. Its SHA-256
entries pin the actual copied bytes. Rebuilds need this repository, not the original
ZIP or an absent `pasteberth-main` directory. Before **any output write**, the
builder validates the complete generated destination set and, for `--sync-product`,
all frontend-copy and manifest destinations. It rejects escaping, dangling or
other symlinked output paths, including symlinked parent directories. Direct build
helpers apply the same checks. This is preflight protection for a trusted
workspace, not protection against another process swapping filesystem entries
during the build; do not rebuild amid concurrent filesystem changes.

Shipped MIME types are pinned in `tools/rebuild.py`, independent of host MIME
registries; unknown extensions use `application/octet-stream`. Add a deliberate
mapping when introducing another example format. Screenshots are **not** rebuilt.

## Test

Python browser tests require Playwright and Chromium. Keep optional dependencies
local to the site; do not change the repository package or application dependencies:

```sh
python3 -m venv site/.venv
site/.venv/bin/python -m pip install playwright
site/.venv/bin/python -m playwright install chromium
```

If `venv`/ensurepip is unavailable and `uv` is installed, use `uv venv site/.venv`
and `uv pip install --python site/.venv/bin/python playwright` instead.
`CHROMIUM` selects an existing executable. The integration used the repository's
already cached Node Playwright Chromium, without downloading another browser:

```sh
export CHROMIUM="$(node -e "process.stdout.write(require('playwright').chromium.executablePath())")"
python3 site/tools/rebuild.py
python3 site/tools/test_sources.py
site/.venv/bin/python site/tools/qa_site.py
site/.venv/bin/python site/tools/qa_brand_language.py
site/.venv/bin/python site/tools/qa_demo.py
python3 site/tools/check_config.py
site/.venv/bin/python site/tools/qa_http.py
node --check site/assets/site.js
node --check site/assets/demo-adapter.js
```

Run `qa_site.py` **before** `check_config.py`: it writes freshly browser-generated
snippets to ignored `qa/work/generated-configs.json`. The native checks parse these
with the current repository's parser and test temporary directory discovery,
`first-directory` labels without Git, retention 100, leaf eligibility and invalid
IDs. Their temporary filesystem stays in `site/qa/work/`; they do not create a
service, modify real zones or write into the runtime package.

`test_sources.py` checks frontend equality, reproducible generated outputs,
an empty host MIME database, all output escape cases (26 symlink scenarios),
historical screenshot hashes, local links/anchors, the public export allowlist,
and the copy example's data/sidecar guard (nine destination scenarios, GNU cp 9.7
tested, `register` stubbed). Symlink cases use isolated site fixtures and spy on
all write calls, so the expected refusal must precede even an in-site write.
The suite rebuilds generated outputs in place
and fails if committed generated bytes were stale. `qa_http.py` starts and stops
an ephemeral loopback-only static server, serves the repository read-only, and
checks real URL/Storage behavior, Markdown bytes, modular demo loading, mobile
navigation and root/mounted paths. It does not start a Pasteberth daemon.
Its short-lived loopback server also exposes the checkout to local clients during
the run; apply the local-machine warning above.

`qa_demo.py` uses the unchanged product UI with synthetic HTML-only and mixed
image/text paste events and a mocked rich clipboard. It verifies the frontend's
sanitizer and original HTML bytes separately. Direct adapter tests cover declared
text MIME/anonymous names, five concurrent 8 MiB uploads, concurrent replacements,
and a transfer racing an upload, including copy rejection and move at capacity.
These are browser-memory tests, not daemon storage or native clipboard tests.

Current JSON reports go in `qa/`; old archive reports were not imported. Large
rendered previews are opt-in and ignored:

```sh
site/.venv/bin/python site/tools/qa_site.py --screenshots
site/.venv/bin/python site/tools/render.py --width 390 --height 844 --lang en --demo
```

The renderer refuses output paths outside `site/`. Its captures show the site
and **simulated demo**, not a genuine daemon installation.

## Verification Scope

The integration reran the following checks on 8 September 2026; machine-readable
reports include UTC timestamps and tested artifact hashes. No archive test count
is evidence for this revision.

| Check | Actual outcome | Evidence |
| --- | --- | --- |
| Rebuild and JavaScript syntax | Passed | `rebuild.py`, `node --check` on site and adapter JS |
| Sources, write confinement, MIME, captures, links, export allowlist and copy guard | 7 passed, 0 failed | `qa/source-report.json` |
| In-memory interaction QA, EN/FR at nine widths (320-1920px) | 70 passed, 0 failed | `qa/report.json` |
| Locale, palette and current frontend equality | 72 passed, 0 failed | `qa/brand-language-report.json` |
| Demo clipboard, MIME, anonymous names and concurrent quota | 6 passed, 0 failed | `qa/demo-report.json` |
| Browser-generated TOML and real temporary discovery | 18 passed, 0 failed | `qa/native-config-report.json` |
| Actual static HTTP, Markdown, URL/Storage, root and mount | 34 passed, 0 failed | `qa/http-report.json` |

The QA run also generated fresh EN/FR desktop/mobile **site** previews with
`qa_site.py --screenshots`; they are ignored, not shipped historical artifacts.
Desktop EN and full-page mobile EN / desktop FR were visually reviewed. These
previews contain the labelled historical hero image and the current memory demo.

Environment adjustments: system `python` and Chromium were absent, and
`python3 -m venv` failed because ensurepip was unavailable. `uv` supplied the
isolated Python 3.13 environment with Playwright 1.62.0; the existing cached
Chromium executable was selected through `CHROMIUM`. A first native-config run
failed on an indentation error in the adapted test script; it was corrected and
the complete native check rerun successfully. No runtime fix was involved.

The independent-review regressions reproduced escaping output paths, directory
copy/sidecar conflicts, host-dependent MIME output, HTML becoming plain text,
and concurrent uploads exceeding 32 MiB before the fixes. The mixed-clipboard
test's initial asynchronous polling was replaced with DOM readiness; its mocked
rich-copy completion also uses a DOM marker because the demo CSP rejects eval.
The complete suites were rerun after correction. The allowlist was validated;
no export was published and the deployment shell recipe was not executed here.

Browser checks cover Chromium only;
clipboard success/refusal is mocked for deterministic tests, not native OS
clipboard interoperability. Palette checks cover specified color pairs, not a
complete accessibility audit. HTTP checks are local, not a deployed-host validation.

No backend regression suite, authentication/storage certification, Windows/macOS
native validation, production deployment or real-daemon screenshot refresh is
claimed by this site work.

## Provenance And Limits

See [PROVENANCE.md](PROVENANCE.md), [source-manifest.json](source-manifest.json)
and [capture-manifest.json](capture-manifest.json).

The approved design came from `work/exchange/pasteberth-v2.1.zip` and was checked
against the product notes and current code. Only useful sources, synthetic
examples, license and three small historical captures were retained. No old
preview images, old QA success reports or private screenshots were imported.

**The screenshots remain historical 2.1.18 captures from the archive dated
8 September 2026**, not screenshots of runtime 2.1.21. Their inherited capture
method and exact hashes are documented separately. The interactive demo, by
contrast, uses the current 2.1.21 frontend with a memory-only adapter.

Demo uploads are limited to 8 MiB per file and 32 MiB of files in total; this is
not a strict JavaScript memory ceiling or the server's configured upload limit
(20 MiB default). Reset/reload discards additions. Demo behavior does not reproduce
server authentication, locking, transactions or storage guarantees.
After asynchronous reads/decoding, the adapter rechecks filename conflicts and
net stored-byte growth immediately before a synchronous commit. Replacements
subtract the currently stored file's size. Transfer checks/commits do not yield;
pending uploads see their result. In-flight reads can still consume extra memory.

The TOML generator is a **site tool**, not a runtime feature. It generates a
collection/group snippet, not a complete secure service configuration. Its
`retain = 100` is an explicit example choice, not the default of 10. Verify
collection-ID uniqueness and actual filesystem permissions with `pasteberth audit`.
Discovery is request-triggered asynchronous scanning with visible-browser polling,
not a standalone perpetual watcher. Groups are views, not ACLs. The optional
review-workflow example adds no process enforcement, messaging or notifications.

The `cp + register` example deliberately uses a fresh name and GNU cp supporting
`-T --update=none-fail`, chained with `&&`. Explicit checks reject existing data
and sidecar paths, including directories, symlinks and dangling symlinks, before
copying. `-T` prevents `cp` from treating the destination as a directory. These
checks **do not reserve either name against concurrent writers**; use this local
recipe only when you control the target directory's writers. Older `cp`
implementations may reject the options; do not replace them with an overwriting
copy. Use daemon-backed `drop` for managed publication and
explicit `--replace` when replacement is intended. Local registration itself does
not replace data or enforce retention continuously.

The distributed license text is unchanged. The product and site use
AGPL-3.0-or-later; source copies and the adapter are included.
