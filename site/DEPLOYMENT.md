# Static Site Export

This exports public documentation for a static host. It does **not** deploy the
Pasteberth daemon. See [the product deployment guide](../docs/deployment.md) for
server configuration, authentication and storage.

## Do Not Publish The Checkout

**Never proxy or publish a server rooted at the full repository checkout.** Even
a loopback-only `python3 -m http.server` exposes everything beneath that directory
to local clients that can reach its port: `.git`, `work/`, configuration files,
test data and other private or ignored files. Binding to loopback is not an
authentication or file-selection mechanism. Symlinks may expose additional files.

For offline review, use `site/preview.html`. For HTTP review or deployment, use a
fresh allowlisted export. Do not use `cp -r .`, a whole-checkout archive, or an
automatic "all tracked files" export as a publication recipe.

## Export An Allowlist

`site/publish-files.txt` lists exact repository-relative public files, one per
line, with no directory entries or wildcards. Review every addition; do not derive
this list from all files in the checkout. In particular, do not add `work/`,
configuration, `.git`, `.venv`, `qa/`, generated preview screenshots or real storage.

From the repository root, after a successful rebuild and tests, run this Bash/GNU
coreutils recipe. It prevalidates all listed inputs, rejects symlinked paths, then
creates a new ignored directory below `site/`. It never deletes or reuses an
existing export. Use a trusted checkout without concurrent filesystem changes.

```sh
(
  set -eu
  REPO="$(pwd -P)"
  test -f site/publish-files.txt
  test -d site
  test "$(realpath -e -- site)" = "$REPO/site"
  while IFS= read -r file; do
    test -f "$file"
    test "$(realpath -e -- "$file")" = "$REPO/$file"
  done < site/publish-files.txt

  EXPORT="$(mktemp -d "$REPO/site/export.XXXXXX")"
  while IFS= read -r file; do
    cp --parents --no-dereference -- "$file" "$EXPORT/"
  done < site/publish-files.txt
  cp -- site/.nojekyll "$EXPORT/.nojekyll"
  printf 'Export ready: %s\n' "$EXPORT"
)
```

The extra root `.nojekyll` disables GitHub Pages/Jekyll processing for the export.
Review the resulting directory before publishing **only that directory's contents**.
For local HTTP review, replace the example export path with the path printed above:

```sh
python3 -m http.server 8080 --bind 127.0.0.1 --directory site/export.ABC123
```

Open `http://127.0.0.1:8080/site/`. Stop the server after review. No upload, remote
publication, proxy configuration or destructive cleanup is part of this recipe.

## Preserve Markdown Paths

Keep `site/`, `GUIDE.md`, `README.md`, `CHANGELOG.md`, `LICENSE`, `docs/` and the
allowlisted `contrib/fetch_pasteberth_item.py` at
the same relative locations within the export. Site links such as
`../docs/using-pasteberth.md` remain plain Markdown navigations. A static server
may display or download Markdown; it does not become an HTML portal. GitHub's
repository view can render the documents. Configure your host's content types
deliberately; do not advertise generated HTML routes that do not exist.
The external-consumer recipe and Python example describe Unreleased work after
2.1.21. Serve the script as downloadable source, never as server-executed code.

This is a documentation export, not a full source checkout. Relative links in
technical documents to implementation files outside the allowlist are not shipped;
use the public [source repository](https://github.com/Fade78/pasteberth) for those.
When another documentation page is added, review and add its exact path to the
allowlist.
Site maintenance and backend-dependent checks require the original repository.

The product screenshot provenance remains historical even after exporting. See
[PROVENANCE.md](PROVENANCE.md). An export creates neither new screenshots nor
new verification evidence. Current check reports remain in the source checkout's
`site/qa/`; they are deliberately not copied into the deployment payload.
