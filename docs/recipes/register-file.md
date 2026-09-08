# Register a File Already in Place

**Need:** Make a completed local file visible in Pasteberth without uploading
or rewriting its bytes.

**Prerequisites:** A regular, stable file in its final directory; read access
to the file; and read, write, and traversal access to its parent. For UI
visibility, the directory must be an active zone and the daemon must be able to
read the resulting pair. Coordinate with other writers so nobody changes this
file during registration.

## Steps

1. If the producer already created the file safely in place, skip the copy below and register that file directly.
2. If starting with `./report.pdf` elsewhere, choose a fresh target basename whose data and `.json` sidecar names are both unused. This Bash example checks the sidecar and uses noclobber creation for the data; it does not overwrite an existing managed file.

```sh
target=/srv/workspaces/alpha/work/exchange/report-2026-09-08.pdf
test ! -e "$target.json" && test ! -L "$target.json" &&
  (set -C; cat -- ./report.pdf > "$target") &&
  pasteberth register --config /absolute/path/config.toml "$target"
```

3. For a file already in place, the registration command alone is:

```sh
pasteberth register --config /absolute/path/config.toml \
  /srv/workspaces/alpha/work/exchange/report-2026-09-08.pdf
```

4. Check the command's exit status and printed path. Confirm that the item appears after the next Web refresh and can be downloaded.

## Observable Result

`report-2026-09-08.pdf.json` is created or refreshed beside the data file.
Registration leaves the data file's bytes and identity unchanged, prints its
resolved path without a zone reference prefix, and does not contact the daemon.

## Pitfalls

- Do not replace the guarded creation with a blind `cp` into an existing managed filename. For ongoing updates, use [daemon-backed `drop --replace`](current-result.md).
- The freshness checks are not coordination with another producer. Use a trusted directory and an agreed unused name. If copying fails, a partial foreign file may remain; inspect it rather than rerunning with clobber enabled.
- Registration applies validation limits from its local configuration or defaults. It does not consult the running daemon's limits, enforce the zone's free-space reserve, or run zone retention. It does not require a configured zone. Use `drop` when daemon publication policy must apply.
- Success proves the registering account's access, not the daemon's. Check parent traversal, file/sidecar readability, and the daemon's actual group membership.
- A later service publication can evict the registered item through retention. Registration is not a retention exemption or a backup.

See [integrations](../integrations.md), [CLI](../reference/cli.md),
[storage](../reference/storage.md), and [troubleshooting](../troubleshooting.md).
