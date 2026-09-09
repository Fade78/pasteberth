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
`PASTEBERTH_PASSWORD`/`--password-stdin`. `pasteberth mcp` uses newline-delimited
JSON-RPC on stdin/stdout and never prompts there; have its trusted launcher
supply `PASTEBERTH_PASSWORD`. The MCP `drop` tool accepts local file paths or
in-memory content and uploads through HTTP.

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

**Unreleased (runtime version still `2.1.21`):** zone and group overviews share
a background cooldown of `max(10 seconds, last full refresh duration)` from
completion, including registry installation. Startup, foreground, and failed
refresh attempts also set it. The next eligible poll can launch one job;
not every overview starts a scan. Explicit service actions bypass the cooldown
or wait for an in-flight refresh, without a duplicate refresh in the same
action. Scanner caches are shared across rules within one pass only. Overview
history and free-space checks remain synchronous and can block; there is no
hard discovery or response deadline. These changes are not in the `2.1.21`
release.

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
- [Configuration](https://github.com/Fade78/pasteberth/blob/main/docs/reference/configuration.md)
- [Deployment](https://github.com/Fade78/pasteberth/blob/main/docs/deployment.md)
- [Operations and recovery](https://github.com/Fade78/pasteberth/blob/main/docs/operations.md)
- [Troubleshooting](https://github.com/Fade78/pasteberth/blob/main/docs/troubleshooting.md)

For a tagged deployment, read those files at the matching tag rather than
assuming `main` documents the installed version. Run `pasteberth --version`
to identify the runtime.
