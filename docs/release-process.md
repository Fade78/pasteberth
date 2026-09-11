# Release And Deployment Traceability

[Documentation map](../GUIDE.md). For backup and upgrade sequencing, see
[operations](operations.md#upgrade); for verification commands and documentary
source authority, see [contributing](contributing.md).

## Release Checks

The release version has two source-of-truth locations:

- `PasteBerth/runtime/__init__.py` for the runtime and Web UI version;
- `pyproject.toml` for packaging metadata.

The live wrapper currently reports `pasteberth 2.1.24`. Verify rather than
copying that number into the next release:

```sh
PasteBerth/pasteberth --version
PasteBerth/pasteberth --help
python3 -m unittest discover -s tests -v
npm run test:e2e:all
E2E_BROWSER=firefox npm run test:e2e:all
```

Install development/browser dependencies as described in
[contributing](contributing.md#tests-and-development) before running browser
tests. Check every subcommand's `--help` against completion and command
examples, relative documentation links and fragments, tracked screenshots,
configuration templates, clone URL, and public support statements. Follow
[documentation maintenance](documentation-maintenance.md) and the
[site publication workflow](../site/README.md) so release-facing pages describe
the same behavior. Do not turn native Windows/macOS opt-in jobs into a support
claim without native validation.

Use the same `X.Y.Z` value in both files, add the user-visible changes to
`CHANGELOG.md`, commit the result, and create the matching `vX.Y.Z` tag. A
working tree or a tag that does not point at the release commit is not a
release.

## Deploy The Tagged Bundle

The tracked `PasteBerth/` directory is the deployment unit. After checking out
the tagged commit, synchronize it to the destination without copying
configuration, credentials, storage, or service state:

Stop the service and other writers first and take a state backup. Review the
destination before running `rsync --delete`: it removes destination files that
are absent from the source. This is safe only for a code-only deployment, never
a directory containing configuration, credentials, or zones. The example
assumes `$HOME/PasteBerth` is the intended deployment root.

```sh
rsync -ani --delete PasteBerth/ "$HOME/PasteBerth/"
```

Review the itemized additions, updates, and deletions. This dry run changes
nothing. Only after confirming the destination and proposed changes, run the
actual synchronization and manifest check:

```sh
rsync -a --delete PasteBerth/ "$HOME/PasteBerth/"
python3 PasteBerth/support/deploy/write_build_info.py \
  --source PasteBerth \
  --destination "$HOME/PasteBerth"
"$HOME/PasteBerth/pasteberth" --version
cat "$HOME/PasteBerth/BUILD_INFO.json"
```

`BUILD_INFO.json` is generated only in the deployment directory. It records the
runtime version, source commit, exact tag, bundle integrity, checkout state, and
the SHA-256 digest of every source bundle file. The script refuses to write the
record when the destination does not match the source bundle or when the
bundle differs from the tagged Git tree, including direct blob content and
regular-file modes. Git symlinks and other non-regular bundle entries are
rejected, and checkout status is fail-closed. Generated `__pycache__/` and
`.pyc` files are intentionally excluded from the bundle digest. Unrelated
files elsewhere in the checkout are included in `source_dirty`.

Restart the service only after the manifest has been written, then verify the
active process and repeat `"$HOME/PasteBerth/pasteberth" --version` against the
deployed wrapper. Audit its configuration before startup, and verify the
authenticated UI/API after restart. A matching manifest does not prove that
the running process has restarted or that runtime state was backed up.

The manifest writer is not a release creator, code builder, or storage
migration tool. A refused manifest is a reason to inspect checkout/tag and
bundle differences, not to edit `BUILD_INFO.json` by hand. Preserve the previous
bundle and state backup until the deployment is verified.
