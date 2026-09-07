# Release And Deployment Traceability

The release version has two source-of-truth locations:

- `PasteBerth/runtime/__init__.py` for the runtime and Web UI version;
- `pyproject.toml` for packaging metadata.

Use the same `X.Y.Z` value in both files, add the user-visible changes to
`CHANGELOG.md`, commit the result, and create the matching `vX.Y.Z` tag. A
working tree or a tag that does not point at the release commit is not a
release.

The tracked `PasteBerth/` directory is the deployment unit. After checking out
the tagged commit, synchronize it to the destination without copying
configuration, credentials, storage, or service state:

```sh
rsync -a --delete PasteBerth/ "$HOME/PasteBerth/"
python3 PasteBerth/support/deploy/write_build_info.py \
  --source PasteBerth \
  --destination "$HOME/PasteBerth"
pasteberth --version
cat "$HOME/PasteBerth/BUILD_INFO.json"
```

`BUILD_INFO.json` is generated only in the deployment directory. It records the
runtime version, source commit, exact tag, bundle integrity, checkout state, and
the SHA-256 digest of every source bundle file. The script refuses to write the
record when the destination does not match the source bundle or when the
bundle differs from the tagged Git tree, including regular-file modes. Git
checkout status is fail-closed; unrelated files elsewhere in the checkout are
included in `source_dirty`.

Restart the service only after the manifest has been written, then verify the
active process and repeat `pasteberth --version` against the deployed wrapper.
