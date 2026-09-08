# Give Script Output a UI

**Need:** Browse and retrieve reports, logs, images, or archives from a tool
that already produces files, without building a separate output gallery.

**Prerequisites:** The tool produces completed regular files outside the
managed target names. A running daemon exposes the intended zone; the caller
can read the output files and authenticate if required. The example assumes
`./out/report.txt` and `./out/diagnostics.zip` exist and those target names are
unused.

## Steps

1. Run the tool and wait for successful completion. Keep partial outputs outside the exchange zone.
2. Publish the finished files explicitly:

```sh
pasteberth drop --config /absolute/path/config.toml \
  /srv/workspaces/alpha/work/exchange \
  ./out/report.txt ./out/diagnostics.zip
```

3. Check the exit status and each returned reference. A nonzero status may follow partial success; inspect the zone before retrying.
4. Open the zone after its next refresh. Inspect supported content, download the archive, or add a short comment explaining the run.

## Observable Result

The UI lists the published files and exposes applicable previews, copy actions,
downloads, comments, and selection. The original tool output files remain
unchanged. The script can also pass returned paths to a consumer with access to
the server's filesystem namespace.

## Pitfalls

- Pasteberth does not monitor arbitrary tool output. Every run needs explicit publication, or safe in-place creation followed by `register`.
- Reusing a basename without `--replace` is refused. Use distinct names for separate runs, or deliberately choose the current-result pattern.
- Daemon retention can remove older output. Multiple files from one run are not an atomic bundle and are not guaranteed to remain together.
- Rich previews are format-dependent; accepting a ZIP does not expose its contents as a directory tree.

See [integrations](../integrations.md), [current result](current-result.md),
[registration](register-file.md), and [selection bundle](selection-bundle.md).
