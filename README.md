# Pasteberth

<p align="center">
  <img src="docs/images/pasteberth-icon.svg" alt="Pasteberth icon" width="96">
</p>

**The bridge between a graphical clipboard, a filesystem, and a CLI/TUI
harness.**

Pasteberth lets a person move screenshots, text, and files from a graphical
workstation to the filesystem read by a terminal-based tool such as OpenCode.
It also lets an agent or script publish a file into a project area for a person
to download in the browser.

![Pasteberth bridge between browser clients, project zones, and filesystem or CLI/TUI clients](docs/images/pasteberth-bridge.png)

## Why Pasteberth?

Graphical and terminal work often happen in different environments. A browser
can receive clipboard content, but a remote harness cannot normally read that
browser clipboard. Conversely, a script can create an artifact on the harness
machine, but a person may need a simple browser download.

Pasteberth is the small, targeted handoff layer between those two sides:

- the browser pastes or drops content into a named project zone;
- the server stores it in a real filesystem directory;
- the harness receives the exact path created by the server;
- scripts can use `drop` to publish files back to the Web UI.

A zone is a directory on the machine running Pasteberth. Every zone uses a
managed data file plus a JSON sidecar for metadata and ownership; dynamic
`[[autozone]]` rules only discover the directories. Files copied or moved into a
zone outside Pasteberth remain foreign and are ignored. The browser never
needs to access the returned filesystem path.

Pasteberth is not a public file host, CDN, cloud drive, or synchronization
service between independent servers.

## Use Cases

### Browser to a terminal harness

Paste an image, a text selection, or a file in the browser. Pasteberth stores a
managed file in the selected zone and returns the exact server-side path. A
terminal harness can read that path without giving the browser access to the
filesystem.

### Add project workspaces without editing configuration

Use one `[[autozone]]` rule for a project tree such as
`/home/me/Depots/*/work/exchange`. When a new project creates a matching,
readable exchange directory, Pasteberth discovers it on the next zone refresh;
a visible browser normally sees it within the next 10-second poll. No daemon
restart and no configuration edit are required. The new directory is exposed as
a zone and placed in the configured autozone group.

### Agent or script to a browser

Use `drop` from a script, CLI/TUI harness, or agent to publish a report or
artifact into a zone. The browser sees the new history item and can preview or
download it. Trusted local agents can use the optional MCP stdio adapter with
the same upload path and protections.

### Register an existing file

Use `register FILE` when a file already exists in a shared exchange directory.
It validates the file and creates or refreshes only its sidecar, leaving the
data file untouched. The daemon discovers the pair on its next refresh.

## Quick Start

The current public release is `2.1.11`. The documented v2.1.11 server runs on
Linux, requires Python 3.11 or newer, and has no third-party
Python runtime dependency. A native Windows backend is included but has only
been validated under Wine; macOS and native Windows remain outside the official
support matrix until real-OS validation is available.

The deployment bundle also contains `PasteBerth\pasteberth.cmd` for invoking
the runtime from Windows `cmd.exe` or PowerShell. Python 3.11 or newer must be
available as `py -3` or `python` on `PATH`.

```sh
git clone https://github.com/Fade78/pasteberth.git
cd pasteberth
cp -a PasteBerth "$HOME/PasteBerth"
mkdir -p "$HOME/.local/bin"
ln -s "$HOME/PasteBerth/pasteberth" "$HOME/.local/bin/pasteberth"
pasteberth --generate-config
# edit ~/.config/pasteberth/config.toml: set the zones and their absolute paths
pasteberth passwd
pasteberth audit
pasteberth
```

Open `http://127.0.0.1:8765/` and sign in with the password created by
`pasteberth passwd`.

For a first local trial, running without a configuration uses a loopback-only
minimal zone at `$XDG_DATA_HOME/pasteberth/storage/default` (normally
`~/.local/share/pasteberth/storage/default`). It has no authentication and must
not be exposed through a proxy or a non-loopback listener.

### Reverse Proxy And Mounted Paths

The generated configuration enables authentication and uses `allowed_hosts = []`
for a deployment-chosen public hostname. This wildcard is safe only with
authentication enabled; anonymous configurations must list their controlled
hosts explicitly.

To publish the service below `/paste`, keep the public path unchanged when
proxying to Pasteberth:

```toml
url_prefix = "/paste"
listen_address = "127.0.0.1"
trusted_proxies = ["127.0.0.1"]
allowed_hosts = []
```

The proxy must preserve `Host[:port]`, overwrite incoming `X-Forwarded-*`
headers, and be the only address listed in `trusted_proxies`. Do not strip
`/paste` before forwarding. The browser `Origin` remains the scheme and host
only, without `/paste`.

## What It Does

- Paste images, text, or files with `Ctrl+V`/`Command+V` or drag and drop.
- Avoid duplicate clipboard uploads server-side and record each new content's
  SHA-256 digest in its JSON sidecar.
- Keep independent zones per project with configurable retention.
- Discover repository directories dynamically with repeatable `[[autozone]]`
  rules while keeping the sidecar storage contract; matching projects appear on
  the next service read without a restart or configuration edit.
- Give autozone zones deterministic, distinct default colors within their group; an
  explicit `color` remains an intentional override for the whole rule.
- Return exact filesystem references such as
  `@/srv/workspaces/project/captures/example.png`.
- Preserve valid dropped filenames when requested, while protecting foreign
  files from accidental replacement or deletion; explicitly register an existing
  file by creating or refreshing only its sidecar.
- Select several items with click, `Shift`-click, or `Ctrl`/`Command`-click.
- Copy a reference list, download a selection as a streamed ZIP, or delete it
  as a group.
- Add a short Unicode comment to a managed item and edit it from the selected
  item panel.
- Hover the selected file or a history icon to see complete, untruncated item
  details.
- Publish files from scripts or agents with `drop`.
- Expose the same `drop` vocabulary to local agents through the optional `mcp`
  stdio adapter.
- Resolve dynamic zone directories from the CLI without editing configuration.
- Rename or delete managed files while keeping data and sidecars consistent.
- Run behind an HTTPS reverse proxy or terminate TLS directly.

## Browser Workspace

![Pasteberth browser workspace showing project zones and content actions](docs/images/pasteberth-ui.png)

Click a zone to make it the active paste target. The content index shows the
zone's history, newest first. The `C` shortcut copies the selected item's link;
number keys select visible zones, and tab-layout groups support `A` to open all
visible zones and `U` to close them.

The upper panel shows the current item and its copy, download, clear, and
preview, and comment actions. With multiple selected items it shows their names,
sizes, and stored dates, then provides group actions instead. In a tab-layout group,
`Shift`-click selects a contiguous range of zones and `Ctrl`/`Command`-click
adds or removes zones; Group options can show or hide the left zone column.

![Pasteberth history with the local NEW indicator and item details](docs/images/pasteberth-new-files.png)

The `show_full_path` configuration option controls whether absolute references
are displayed in the Web UI. It defaults to `true` for compatibility; set it to
`false` when server-side paths contain sensitive information. The API still
returns the exact reference needed by the harness and copy actions.

## Server Handoff

Multi-file CLI uploads go through the Pasteberth server. Use a target directory
to let the daemon resolve a static zone or eligible autozone, or use `--zone`
with a zone ID. `--server` overrides the configured server URL:

```sh
pasteberth drop --server https://pasteberth.example.internal \
  --insecure /srv/pasteberth/project-alpha /tmp/report.pdf /tmp/screenshot.png

pasteberth register /srv/pasteberth/project-alpha/existing.txt
```

The source files remain unchanged. With a loopback server and a writable target,
`drop` stages the data there and asks the daemon to create the managed data/
sidecar pair; otherwise it uploads through HTTP. The daemon performs the target
zone resolution, including canonical symlink handling, so a client-side config
is not required. Without a config or `--server`, the client tries
`https://127.0.0.1:8765`. A one-file `drop FILE` invocation is invalid; use
`register FILE` for an existing regular file. `register` creates or refreshes
only the sidecar, never rewrites, moves, or replaces the data file, and never
contacts the daemon. The local command prints the absolute data-file path; the
daemon will discover the sidecar on its next refresh. Remote or unwritable multi-file drops use the
server session, with `PASTEBERTH_PASSWORD` or `--password-stdin` available for
non-interactive use.

Direct staging still calls the daemon through the configured server URL. If a
trusted local HTTPS certificate is self-signed, add `--insecure`; this disables
certificate verification only.

Clipboard uploads and other uploads without a preserved filename are
deduplicated per zone by the server. Repeating the same bytes returns the
existing item with `duplicate: true` instead of creating a new item or
consuming retention. Named file drops remain independent, so two intentionally
different filenames may contain the same bytes.

### MCP stdio adapter

Run the optional adapter as a local MCP server:

```sh
PASTEBERTH_PASSWORD='your-password' pasteberth mcp --config config.toml
```

On Windows `cmd.exe`, use `set PASTEBERTH_PASSWORD=your-password` before
running `PasteBerth\pasteberth.cmd mcp --config config.toml`; in PowerShell use
`$env:PASTEBERTH_PASSWORD = "your-password"`. The MCP process can read any
regular local file readable by its account, so only connect trusted agents.

The adapter reads newline-delimited JSON-RPC from standard input and writes only
MCP responses to standard output. Its initial `drop` tool accepts existing local
file paths or UTF-8 content with a filename, then uses the same HTTP upload path,
zone checks, size limits, MIME classification, and managed-file protections as
the CLI. The password must come from `PASTEBERTH_PASSWORD`; standard input is
reserved for the MCP protocol. The stdio implementation supports modern MCP
discovery and requests from `2026-07-28` clients, while retaining the legacy
initialize handshake through `2025-06-18`.

## Documentation

[`GUIDE.md`](GUIDE.md) is the complete operator and integration guide. It
covers:

- installation, configuration, and the configuration discovery order;
- Web UI behavior, selection, clipboard handling, and content types;
- every CLI command, its syntax, and its exit codes;
- Bash completion in [`PasteBerth/support/completions/pasteberth.bash`](PasteBerth/support/completions/pasteberth.bash);
- filesystem layout, sidecars, retention, backup, and recovery;
- automatic-zone discovery, sidecar storage, permissions, and diagnostics;
- systemd, Caddy, nginx, TLS, authentication, and security boundaries;
- the HTTP API, batch operations, errors, and troubleshooting;
- tests, current support limits, and the future multiplatform direction.

User-visible release history is in [`CHANGELOG.md`](CHANGELOG.md).

The commented [`PasteBerth/support/config.example.toml`](PasteBerth/support/config.example.toml)
is the reference configuration template. The optional Linux user-service
template is [`PasteBerth/support/deploy/pasteberth.service`](PasteBerth/support/deploy/pasteberth.service).

## Support Status

| Area | v2.1.11 status |
|---|---|
| Python | 3.11 or newer |
| Server | Linux, officially tested |
| Destination | Local filesystem only |
| Browser | Chromium-tested; Firefox suite available |
| Windows/macOS server | Windows backend covered by Wine tests; native Windows/macOS not officially validated |
| Network/exotic filesystems | Not guaranteed without capability validation |

## License

Pasteberth is licensed under the **GNU Affero General Public License v3.0 or
later**. See [`LICENSE`](LICENSE).

If a modified Pasteberth is run as a publicly accessible network service, the
AGPL source-sharing requirements apply to users of that service.
