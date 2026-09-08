# Contributing

[Documentation map](../GUIDE.md).

## Scope And Workflow

Keep changes focused and reproduce behavior before changing its contract.
Add or update the relevant Python or browser tests with a behavior change.
Keep the tracked `PasteBerth/` directory independently deployable; development
tools and documentation stay outside its runtime except for the bundled
support files. Do not add a runtime dependency or claim a new supported
platform merely to make a development environment work.

Describe the reason for a change, its user-visible effects, the tests actually
run, and any gaps in verification. A passing frontend source-contract test is
not equivalent to a real browser test; Wine coverage is not native Windows
validation.

## Documentation Authority

Document what the current implementation and tests support, not intended
features. Use runtime/parser code for behavior and CLI options, configuration
defaults and the bundled example for settings, tests for exercised boundaries,
and the declared release metadata for the version. Resolve contradictions
against those sources rather than copying an older paragraph or screenshot.

The repository Markdown is the documentary source. Follow
[documentation maintenance](documentation-maintenance.md) for ownership,
cross-links, coverage, and coordinated updates; follow the
[site workflow](../site/README.md) for publishing. Do not let a generated site
become a second independent behavior specification.

Keep `GUIDE.md` a navigation map and preserve its legacy anchors. Put exact
options and response contracts in the reference pages; link task-led guides
and recipes to those contracts instead of silently changing their guarantees.
In particular, preserve the distinction between server-backed `drop` and local
`register`, the raw-HTML copy exception, the transaction/external-writer
boundary, and shared authentication versus presentation-only zone groups.

## Tests and Development

The project has no runtime dependency beyond Python's standard library. Browser
tests use the Node development dependency declared in `package.json`.

Install the Node development dependencies and Playwright browsers, then run
the combined Python and browser suites on Linux:

```sh
npm ci
npx playwright install chromium firefox
npm run test:all
```

Run individual suites when iterating:

```sh
python3 -m unittest discover -s tests -v
npm run test:e2e
E2E_BROWSER=firefox npm run test:e2e
npm run test:e2e:mounted
E2E_BROWSER=firefox npm run test:e2e:all
```

Browser installation may also require system libraries; use Playwright's
documented dependency installation for the host or the CI container image.
`test:all` runs Python and `test:e2e:all` in parallel. The browser defaults to
Chromium, so it does not by itself run both browser engines; set `E2E_BROWSER`
to run Firefox. `test:e2e:all` includes the `/paste` mounted deployment suite.

The tests cover storage ownership and recovery, image validation and content
classification, filenames and replacement, configuration and startup policy,
authentication and CSRF, proxy headers, concurrency and zone locks, filesystem
CLI operations, frontend contracts, and browser interactions.

The CI browser job runs the full suite as a Chromium/Firefox matrix. The
`native-input.spec.js` test exercises the browser's real file chooser through
Playwright's `setFiles`; synthetic clipboard and drag events remain separate
coverage because an operating-system drag source is not available in the Linux
CI runner.

Before a public release, verify at least:

- `pasteberth --help` matches the completion script;
- every command example uses an actual parser option;
- all README, GUIDE, documentation, and site relative links and fragments resolve;
- the public clone URL and current version are correct;
- all referenced images and configuration files are tracked;
- the full test suite is green.

Use the [release process](release-process.md) for version, tag, and deployment
manifest checks. Local testing does not authorize tagging, publishing, or
changing a user's live deployment.

## License

Pasteberth is licensed under the **GNU Affero General Public License v3.0 or
later** (AGPL-3.0-or-later). See [`LICENSE`](../LICENSE).

If a modified Pasteberth is run as a publicly accessible network service, the
AGPL source-sharing requirements apply to users of that service.
