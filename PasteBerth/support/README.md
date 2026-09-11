# PasteBerth Deployment Support

The executable for this deployment is `../pasteberth`. Link to that file from
the user's `PATH`; do not copy it away from this directory unless
`PASTEBERTH_HOME` is set to this deployment root.

Do not start the private runtime with `python3 -m PasteBerth.runtime` from
inside this directory. That command cannot discover the package parent; use
the wrapper below instead.

```sh
mkdir -p ~/.local/bin
ln -s /srv/PasteBerth/pasteberth ~/.local/bin/pasteberth
export PATH="$HOME/.local/bin:$PATH"
pasteberth --generate-config
# Edit the generated config: the example exchange directory must be a zone.
pasteberth passwd
pasteberth audit
pasteberth serve
```

The server runs in the foreground. In another terminal:

```sh
pasteberth drop /home/atelier/exchange report.md
pasteberth drop /home/atelier/exchange report.md screenshot.png
pasteberth register /home/atelier/exchange/existing.md
eval "$(pasteberth completion)"
```

These are example paths, not directories created by the launcher. The existing
file passed to `register` must already be present. Use one explicit
`--config /absolute/path/config.toml` consistently if your configuration is not
at the default location.

The deployment is code only. Configuration, passwords, TLS keys, zones, and
runtime state belong outside this directory. Use `--config PATH` or
`PASTEBERTH_CONFIG` for configuration. The default configuration and storage
locations are under the XDG configuration and data directories.

For target-directory drops, the client can omit its configuration when it
shares the machine and target filesystem with the daemon. Without a discovered
configuration or `--server`, it tries `http://127.0.0.1:8765`; the daemon resolves the supplied target
against its configured static zones and `[[zone_collection]]` candidates. Use
an explicit `--server URL` for a different endpoint; include any mount prefix,
such as `https://pasteberth.example.internal/paste`. TLS verification is enabled
by default. Use `--insecure` only for a separately trusted self-signed endpoint,
not as a routine upload flag. A one-file `drop FILE` is invalid because `drop` is always
server-backed. Use `register FILE` for a local-only sidecar operation; the
resulting sidecar must be readable by the daemon.

`register` validates the file with the CLI-selected configuration or local
defaults and refreshes metadata without changing the data file. It does not
contact the daemon, require a configured zone, or enforce the daemon's
retention and per-zone free-space reserve. A successful registration is not
proof that the daemon can see the file. Use `drop --zone ID FILE...` for a
remote upload without sharing a directory path with the service.

After an HTTP `401`, `drop` can prompt for a password or read
`PASTEBERTH_PASSWORD`/`--password-stdin`, or use a scoped bearer token from
`PASTEBERTH_TOKEN`/`--token-stdin`. `pasteberth mcp` uses newline-delimited
JSON-RPC on stdin/stdout and never prompts there; have its trusted launcher
supply `PASTEBERTH_TOKEN` or `PASTEBERTH_PASSWORD`. The MCP `drop` tool accepts
local file paths or in-memory content and uploads through HTTP.

`config.example.toml`, `deploy/pasteberth.service`, and
`completions/pasteberth.bash` are reference files. `pasteberth completion`
prints the completion script directly for shell evaluation.

The optional user service template uses `PrivateTmp=true`. Do not place shared
zones under `/tmp` or `/var/tmp` with that setting. Adapt its executable,
configuration, and optional writable-path hardening before enabling it.

For a shared POSIX zone, put the daemon account and every `register` writer in
the directory's group and use a `setgid` directory. The group must be present
in the running daemon process. After changing group membership, log out and in
again (or reboot) before restarting a `systemd --user` service; reloading the
unit alone does not refresh its supplementary groups. A successful `register`
can otherwise create a sidecar that the daemon cannot read.

A zone collection can cover a whole project tree, for example
`/home/me/Depots/*/work/exchange`. A visible browser polls every 10 seconds and
shows a new matching directory after a background scan observes it and a later
poll reads the completed registry. No configuration edit or service restart
is needed; the directory must still be readable, writable, and
traversable and satisfy the rule's depth and subtree constraints.

**Unreleased (runtime version still `2.1.22`):** zone and group overviews share
a background cooldown of `max(10 seconds, last full refresh duration)` from
completion, including registry installation. Startup, foreground, and failed
refresh attempts also set it. The next eligible poll can launch one job;
not every overview starts a scan. Mutations, directory resolution, and legacy
`/images` history reads bypass the cooldown or wait for an in-flight refresh,
without a duplicate refresh in the same action. Scanner caches are shared across rules
within one pass only. Overview
history and free-space checks remain synchronous and can block; there is no
hard discovery or response deadline. These changes are not in the `2.1.22`
release.

**Unreleased reads:** generic `/items` listings, GET/HEAD content on either
route, and ZIP use the published registry without starting discovery or waiting
for a scan. New zone IDs return `404`
until published; removals take effect through later registry publication.
Listing still reads the selected zone's history. For downloads, selected
metadata and file handles are acquired under shared filesystem locks,
then streamed without zone locks, so managed replacement/deletion can continue
even on those filenames. Acquisition still checks directory identity, enumerates
names, and reads journals; it can wait on storage or an exclusive writer.
This is not snapshot protection against external in-place writes, a sidecar
format change, or a new per-file lock protocol; older cooperating CLI writers
remain compatible.

New `[limits]` defaults are `max_archive_files = 64` and
`max_active_archives = 4` across all zones per process. Both accept positive
integers or `"unlimited"`. ZIP slot exhaustion returns `503 server_busy` with
`Retry-After: 1`; nonblocking ZIP acquisition can instead return `423 zone_busy`
for an exclusive writer. Generic content GET/HEAD and listing also use
nonblocking acquisition; legacy preview acquisition remains blocking.
The 256 MiB source-byte and 300-second ZIP streaming defaults remain; the
request deadline covers initial acquisition and becomes inactivity-based during
emission. Handles and slots are released on completion, timeout, disconnect,
or failure as the handler unwinds, not by forcibly interrupting filesystem I/O.

**Unreleased generic API:** use `/api/zones/{id}/items` for listing and upload,
its comment/delete/batch-delete/archive/regularize children, and
`/api/zones/{id}/items/{filename}/content` for GET/HEAD downloads. Upload uses
`file`; both routes accept either `file` or `image`, exactly one payload.
The bundled UI selects `schema=items` on `/api/zones` and `/api/transfers`;
omitting the selector or using `schema=images` preserves the legacy representation
without duplicate histories. Generic payloads use `content_url`, not
`preview_url`; legacy payloads keep `preview_url` and add identity fields and
`content_url`. Old routes share handlers and remain supported throughout 2.x;
removal will be no earlier than 3.0, announced in advance, with no date set.
Stored zones need no migration; Python `StoredImage`/`UnknownImageError` remain
aliases for `StoredItem`/`UnknownItemError`.

Known identity is stored lowercase SHA-256 and an exact quoted `"sha256-HEX"`
ETag; legacy unknown values are null. GET/HEAD on both content routes support
strong `If-Match` against the acquired version, returning 412 without file bytes
on mismatch and 400 for malformed conditions. Comments do not change the ETag,
and `changed_at` remains null. This is not hashing on read or protection against
external in-place writes: consumers must verify downloaded length and digest.
Unknown legacy identity cannot pin the version in a listing. The repository's
external-consumer example verifies before local replacement; exit 3 means the
file was published but stdout reporting failed, not rollback.

At a public loopback reverse proxy, block `/api/drop/resolve` and **both**
`/api/zones/{id}/images/regularize` and `/api/zones/{id}/items/regularize`, including
the configured mount prefix. Otherwise the immediate-loopback-peer exception
remains reachable through the unblocked alias. Ordinary uploads and remote
`drop --zone ID` need neither route. Keep cookie, Host/Origin, and TLS checks.

Shared Web authentication is not per-user authorization. Groups and collections
organize zones but do not grant or restrict access. Filesystem group permissions
are a separate local boundary, and transaction locks coordinate Pasteberth
operations rather than arbitrary external writers.

## Full Documentation

Linux with Python 3.11+ and supported local storage is the official server
platform. Native Windows/macOS validation is still outstanding. The deployment
has no third-party Python runtime dependencies.

The code-only bundle does not contain the repository's full documentation.
In a checkout, start at `GUIDE.md`; otherwise use these repository links:

- [Documentation map](https://github.com/Fade78/pasteberth/blob/main/GUIDE.md)
- [CLI reference](https://github.com/Fade78/pasteberth/blob/main/docs/reference/cli.md)
- [HTTP API](https://github.com/Fade78/pasteberth/blob/main/docs/reference/api.md)
- [External consumer (Unreleased)](https://github.com/Fade78/pasteberth/blob/main/docs/recipes/external-consumer.md)
- [Configuration](https://github.com/Fade78/pasteberth/blob/main/docs/reference/configuration.md)
- [Deployment](https://github.com/Fade78/pasteberth/blob/main/docs/deployment.md)
- [Operations and recovery](https://github.com/Fade78/pasteberth/blob/main/docs/operations.md)
- [Troubleshooting](https://github.com/Fade78/pasteberth/blob/main/docs/troubleshooting.md)

For a tagged deployment, read those files at the matching tag rather than
assuming `main` documents the installed version. Run `pasteberth --version`
to identify the runtime.
