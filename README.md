# Pasteberth

**The bridge between a graphical clipboard, a filesystem, and a CLI/TUI
harness.**

Pasteberth is designed for work shared between a graphical environment and
terminal-based tools — OpenCode and similar harnesses. A browser can send an
image, text, or file to the harness machine, while an agent or script can copy
an artifact into a configured zone for people to download in the browser.

The transfer remains deliberately simple: each zone is a filesystem directory
with a small JSON sidecar for ownership and metadata. The browser receives
clipboard content or a dropped file, and `filesystem-drop` sends a file from a
local process into the same storage path. The Web UI and filesystem command
are two independent clients of the same service.

You take a screenshot or copy content on your workstation, paste it (Ctrl+V)
into the area for the right project in your browser, or drop a file there, and
retrieve a filesystem reference ready to paste into the harness:

```
@/absolute/path/to/PasteBerth/captures/project-alpha/example.png
```

```
WORKSTATION                              HARNESS MACHINE
capture ──▶ browser ── HTTPS ──▶ Pasteberth ──▶ captures/<zone>/
                  ▲                         │
                  └──── copied reference ◀──┘
                                               │
                                               ▼
                                      terminal / CLI-TUI harness

agent/script ── filesystem-drop ─────▶ captures/<zone>/ ──▶ browser download
```

The browser **never** needs to access the returned path. This is the path seen
by the harness, on the machine where Pasteberth runs.

The current version is `1.5.0`.

---

## Contents

1. [Use Cases](#use-cases)
2. [How It Works](#how-it-works)
3. [Filesystem Operations](#filesystem-operations)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [Password](#password)
7. [Launching & systemd service](#launching--systemd-service)
8. [HTTPS & reverse proxy](#https--reverse-proxy)
9. [API](#api)
10. [Security](#security)
11. [Tests](#tests)
12. [Limitations & V2](#limitations--v2)

---

## Use Cases

Pasteberth is useful when the two sides of the work are not in the same
environment:

- an image, text, or file is produced in a graphical environment, possibly on
  another workstation;
- the harness works in a terminal or a remote session;
- the harness can read the filesystem on its machine, but cannot receive
  clipboard content directly from the graphical workstation;
- an agent, script, or another local process produces a file that a person
  needs to download from the browser;
- you want to keep a few contents per project without creating a
  general-purpose file-sharing service.

Pasteberth is not public storage, a CDN, or a synchronization tool between
users. It is a local, targeted gateway between graphical clients, filesystem
processes, and terminal tools.

## How It Works

- **One zone per project**: each zone has an independent identifier, label,
  color, directory, and retention policy. Click a zone or its selection button
  to make it the active paste target, then use Ctrl+V or drag and drop. Moving
  focus to an action or history item does not change that target. The
  filesystem command targets a zone by its configured server-side directory,
  not by its UI label or identifier.
- **Image, text, and file content**: Ctrl+V preserves clipboard content; a
  dragged file is stored under its original name. If it replaces a Pasteberth-
  managed file with the same name, the browser first asks for confirmation.
  The API and filesystem command require the equivalent explicit replacement
  flag. Content and sidecar are replaced atomically. Foreign files are never
  overwritten.
- **Mixed clipboard paste**: a paste carrying both an image and plain text is
  stored as a single `.html` document — the text with the images embedded at
  their positions. `Copy Text` restores both flavors (`text/html` and
  `text/plain`): rich content in editors, clean text in terminals. A copied
  web image (HTML flavor only, no readable text) stays a plain image.
- **Selected content panel**: after an upload, the new content is selected at
  the top with its name, reference, and a matching content action: `Copy Image`
  or `Copy Text` where relevant, plus `Download EXT` for every type. `Copy link`,
  `Clear`, and image/text `Preview`/`Zoom` remain available according to the
  content; binary files are downloaded directly.
- **Content index**: the items at the bottom form the zone's complete history,
  newest first. Images are shown in thumbnail slots backed by the server's
  preview URL; the server does not generate resized thumbnails. Text and files
  have a type marker. Click an item to select it in the upper panel; Shift-click
  extends the selection to a range and Ctrl/Command-click adds or removes an
  item. The selected items can be copied as one reference list, downloaded as
  a ZIP, or deleted together.
- **Multiple file drop**: dropping several files starts a sequential queue of
  independent uploads. One invalid file does not cancel the others. A managed
  filename collision asks for replacement before that file is written.
- **Clipboard**: after an upload, Pasteberth tries to copy the exact reference
  to the clipboard. `Copy link` copies that reference, `Copy Image` copies the
  image itself, `Copy Text` copies text, and `Clear` replaces the clipboard
  contents with empty text, within the limits of the browser's Clipboard
  permissions.
- **Exact references**: the server builds and returns the path; the frontend
  copies each reference as-is, never reconstructing an item reference. The
  item prefix/suffix and copied-list prefix/suffix/separator are configurable.
- **Circular retention per zone** (`retain = N`): beyond N stored contents, the
  oldest entries are deleted — only files created by Pasteberth with their JSON
  sidecar.
- **Persistent page**: intended to remain open for hours; image previews come
  from the server, no Blob URL accumulates, and the history is resynchronized
  every 45 seconds and when returning to the tab.

For OpenCode's `@` selector to find stored images or files directly, place the zone in the
workspace opened by OpenCode, or open a workspace that contains both the
project and the capture directory. Otherwise, the path remains valid for an
explicit read by the harness.

## Filesystem Drop

`filesystem-drop` copies one or more regular files into a configured zone and
creates the matching Pasteberth sidecars. The source files are left unchanged.
The target is the exact absolute directory configured for the zone as seen by
the machine running Pasteberth:

```sh
pasteberth filesystem-drop --config config.toml \
  /absolute/path/to/PasteBerth/captures/project-alpha \
  /tmp/report.txt /tmp/screenshot.png
```

The command prints one returned reference per successful source. Files are
processed independently; a failure makes the command return non-zero while
other sources can still be processed. The initial implementation is flat: the
source basename is stored directly in the target zone, not in a subdirectory.

An existing Pasteberth-managed name is refused by default. Use `--replace` to
make the replacement explicit:

```sh
pasteberth filesystem-drop --replace --config config.toml \
  /absolute/path/to/PasteBerth/captures/project-alpha /tmp/report.txt
```

The Web drop uses the same server-side rule through a confirmation dialog. Two
confirmed replacements are serialized by the zone lock; the last transaction
to commit wins. A file already present without a valid matching sidecar is a
storage conflict and is never overwritten, including with `--replace`.

There is no directory watcher in V1. An agent or script must invoke the
command when it wants to publish a file.

## Filesystem Operations

The filesystem client can rename or delete a managed pair without bypassing
its JSON sidecar. Names are basenames inside the exact configured zone; paths,
symlinks, foreign files, and occupied rename targets are refused.

Rename a stored item:

```sh
pasteberth filesystem-rename --config config.toml \
  /absolute/path/to/PasteBerth/captures/project-alpha \
  report.txt report-final.txt
```

The command moves the data file and updates the sidecar as one durable
operation. It prints the new reference on success and never replaces the
target.

Delete one or more stored items:

```sh
pasteberth filesystem-delete --config config.toml \
  /absolute/path/to/PasteBerth/captures/project-alpha report-final.txt
```

Normal deletion requires a structurally valid sidecar whose recorded size
matches the data file. If a managed file was changed by an external process and
is therefore hidden from the history, deletion can be made explicit with
`--force`; this still refuses malformed sidecars and files without a matching
Pasteberth sidecar:

```sh
pasteberth filesystem-delete --force --config config.toml \
  /absolute/path/to/PasteBerth/captures/project-alpha report-final.txt
```

## Installation

Prerequisite: **Python ≥ 3.11** and no third-party runtime dependencies (the
server uses the standard library only). The repository is the installation
itself; no installation script or root access is required.

```sh
git clone https://glb.didierb.name/didier/pasteberth.git
cd pasteberth
./bin/pasteberth --generate-config
# edit config.toml: zones, paths, and deployment options
./bin/pasteberth passwd
./bin/pasteberth audit --config config.toml
./bin/pasteberth --config config.toml
```

The audit in this workflow checks the environment before the first start. An
audit that reports only warnings returns `1`; configuration errors return
`2`.
It is safe to run while an instance is already listening: an occupied configured
port is reported as a warning, while other bind failures remain configuration
errors.

To use the command from any directory, add `bin/` to the `PATH` or use
`./bin/pasteberth` directly.

```sh
export PATH="$PWD/bin:$PATH"
pasteberth
```

## Configuration

After generation, the default local file is `config.toml` at the repository
root and is ignored by Git. An explicit configuration can also be provided with
`--config PATH` or `$PASTEBERTH_CONFIG`; custom paths are not automatically
ignored by Git and must be protected manually. An older XDG configuration at
`~/.config/pasteberth/config.toml` remains recognized. See the commented
[`config.example.toml`](config.example.toml).

At runtime, configuration discovery has this priority: explicit `--config`,
`$PASTEBERTH_CONFIG`, repository-root `config.toml`, XDG
`~/.config/pasteberth/config.toml`, then the built-in minimal mode. The
`--generate-config` command writes to the explicit path or environment path,
then to the repository-root `config.toml`; it does not generate into the XDG
path. Consequently, generating a repository config can shadow an existing XDG
config on the next run. Use the same `--config PATH` for `passwd`, `audit`,
the server, and filesystem commands when more than one configuration exists.

Without a discovered configuration, `pasteberth` intentionally starts in
minimal mode, loopback-only, with storage at `<repository>/storage/default`
and no authentication. A warning is displayed at every start. The same warning
appears if a modified configuration continues to target this default storage.
This mode is for a first local trial, not for exposure through a reverse proxy.

`pasteberth --generate-config` generates a secure configuration with
authentication enabled. Then manually edit `config.toml` according to the
desired zones and paths. By default, `--generate-config` refuses to replace an
existing file; `--force` overwrites the target, so review the path before using
it. The default repository config is ignored by Git, but a custom config path is
not automatically ignored.

Configuration is loaded at startup. Restart the server after changing zones,
listeners, TLS, proxy trust, host allowlists, upload limits, groups, or auth
settings. The password hash is the exception: its contents are reloaded on each
authentication attempt.

| Key | Default | Role |
|---|---|---|
| `listen_address` | `"127.0.0.1"` | listening address; non-loopback requires TLS or an explicit private-network HTTP opt-in |
| `port` | `8765` | TCP port |
| `max_upload_size` | `"20MiB"` | per-upload limit (20 MiB by default, 50 MiB maximum) |
| `max_image_pixels` | `25000000` | structural image pixel limit (25 MP by default, 50 MP maximum) |
| `accept_img` | `true` | accept structurally valid PNG, JPEG, and WebP images |
| `accept_doc` | `true` | accept valid UTF-8 text content |
| `accept_bin` | `true` | accept opaque binary content |
| `trusted_proxies` | `[]` | only these peers may set `X-Forwarded-*`; configure the actual reverse proxy IPs explicitly. Trusting loopback trusts any local process that can connect |
| `allowed_hosts` | `[]` | hostnames accepted by Host/Origin checks; empty = wildcard (audit warns). Prefer a non-empty list for exposed deployments |
| `allow_unauthenticated_local` | `false` | explicit opt-in for anonymous loopback/proxy mode |
| `allow_unauthenticated_remote` | `false` | explicit unlock (discouraged) |
| `allow_insecure_http_remote` | `false` | separate opt-in for non-loopback HTTP (private network only) |
| `log_level` | `"INFO"` | DEBUG/INFO/WARNING/ERROR |
| `[tls] enabled` | `false` | terminates TLS directly with `certificate` and `private_key` |
| `[auth] enabled` | `true` | password protection |
| `[auth] session_ttl_hours` | `72` | server session lifetime |
| `[auth] password_file` | next to `config.toml` | absolute path to the `passwd` hash (regular 0600 file) |
| `[[zones]] …` | `default` | `id`, `label`, `type=local`, `directory`, `retain`, `reference_prefix`, `reference_suffix`, `reference_list_prefix`, `reference_list_suffix`, `reference_separator`, `allow_zip_download`, `color` (#RRGGBB), `create_directory`, `min_free_percent` |
| `[[groups]] …` | none | group `selection` (`all`, `pattern`, `other`), `pattern` required for `pattern`, `layout` (`area`, `tab`), `hide_empty`, `show_count` |

`directory` is an **absolute path as seen by the server** — this is where
OpenCode or the harness reads the stored files, not your browser. It is also
the target path accepted by `filesystem-drop`.

To copy a reference enclosed in backticks, for example `` `/path/image.png` ``:

```toml
reference_prefix = "`"
reference_suffix = "`"
```

For three projects, repeat `[[zones]]`; each block describes an entry in the
`zones` list:

```toml
[[zones]]
id = "project-alpha"
label = "Project Alpha"
type = "local"
directory = "/absolute/path/to/PasteBerth/captures/project-alpha"
retain = 10
reference_prefix = ""
reference_suffix = ""
color = "#304237"

[[zones]]
id = "project-beta"
label = "Project Beta"
type = "local"
directory = "/absolute/path/to/PasteBerth/captures/project-beta"
retain = 10
reference_prefix = ""
reference_suffix = ""
color = "#9e3451"

[[zones]]
id = "pasteberth"
label = "PasteBerth"
type = "local"
directory = "/absolute/path/to/PasteBerth/captures/pasteberth"
retain = 10
reference_prefix = ""
reference_suffix = ""
color = "#394252"
```

The default integrated storage is `<repository-root>/storage/default`. The
`storage/` directory is ignored by Git, but must be backed up separately if the
stored contents are valuable. An external path can be specified manually in
`config.toml`.

Groups are loaded when the service starts. A group can use one of three
selection modes: `all` contains every zone; `pattern` contains every zone whose
ID matches at least one Python regular expression in `pattern`; `other` contains
zones not selected by any `pattern` group. Expressions use Python `re.search`
semantics and are case-sensitive; use `^` and `$` when anchoring is needed. `all`
groups are deliberately ignored when calculating
`other`, so explicit `All` and `Other` groups can overlap; `pasteberth audit`
warns about that configuration. A zone can belong to several groups. When
groups are configured, the interface displays only the zones in the selected
group. A zone that matches no group is therefore absent from every group view.
With no `[[groups]]` section, all zones are displayed through an implicit `All`
fallback and no group tab is shown. The example configuration contains an
explicit `All` group with `selection = "all"`.

Several `all` or `other` groups are accepted but redundant and are reported by
the audit. A `pattern` field on an `all` or `other` group is ignored and also
reported. Multiple pattern groups are useful when their patterns select
different zones; equivalent effective selections are reported as redundant.

The group controls expose local display preferences for empty groups, zone
counts, and the selected group's layout. `layout = "area"` keeps the current
zone grid. `layout = "tab"` shows zone names in a column and opens selected
zones in the main view. In area view, clicking a zone card or its zone button
makes it the paste target. In tab view, hovering or focusing a zone name makes
it the next paste target; clicking opens or closes it. Shift-click adds or
removes a zone without closing the other open zones. `Ctrl+V` or `Command+V`
pastes to the active target; focusing action buttons or history items does not
change it, while a direct drop always targets the zone under the drop. In tab
view, `A` opens all visible zones in the current group and `U` closes them;
these shortcuts do not change the active paste target. Clicking a thumbnail
selects the content shown in that zone's upper panel. The active zone is
cleared when changing groups.

Zones must be distinct, writable target directories. Private mode `0700` is
recommended; a more open mode produces an audit warning but does not prevent
startup, allowing controlled sharing between multiple users. Each zone refuses a new upload if the free space expected after writing would fall below
`min_free_percent` (default `2.0`). The measurement applies to the directory's
filesystem, not just the folder; multiple zones can therefore share a
filesystem, but then they also share its free-space reserve.

Pasteberth recognizes a stored item only when its regular file and matching
JSON sidecar are readable by the service user and the metadata is coherent.
Foreign files and orphan sidecars are left untouched; transaction-owned
temporary files can be reconciled after an interrupted write.
Shared writable directories therefore imply trust between the users who can
modify their entries; use `0700` when that trust is not appropriate.

Images are limited to `16 384 × 16 384` pixels and a structural pixel budget of
`25 MP` by default, which covers usual 4K to 6K displays. 8K images exceeding
`25 MP` require an explicit limit increase, up to the hard maximum of `50 MP`.
This check is bounded structural validation; it does not fully decode the image
bitstream.

For a dropped file, `preserve_name=1` can retain its original filename. Names
are limited to 200 characters and 240 UTF-8 bytes; `/`, `\`, NUL, CR/LF, `.`,
`..`, `.pasteberth.lock`, and Pasteberth's temporary prefixes are reserved or
rejected. An invalid name returns `400`. Names starting with a dot, such as
`.env`, are supported and appear in the history.

## Password

```sh
pasteberth passwd --config config.toml  # prompt + confirmation, salted scrypt hash
                                        # writes to password_file or next to config.toml (0600)
```

The password is never stored in plaintext or written to config.toml; the hash
is verified with `hashlib.scrypt` plus a constant-time comparison. A change
takes effect immediately (reloaded on every attempt), without restarting the
service, and invalidates existing sessions. The server refuses to start if
authentication is enabled without a readable, valid `passwd` file. If the
selected configuration is invalid, `passwd` stops without writing any hash;
fix the configuration and run `audit` first.

## Launching & systemd service

```sh
pasteberth audit --config config.toml              # check without modification
pasteberth --config config.toml                    # foreground
```

The supplied unit (`deploy/pasteberth.service`) is optional and requires no
root. Install it only after the configuration has been generated, edited,
authenticated with `passwd`, and checked with `audit`. Adapt its
`WorkingDirectory`, `ExecStart`, and `--config` path to the actual repository
before enabling it. The template uses `~/PasteBerth` as an example:

```ini
[Service]
WorkingDirectory=%h/PasteBerth
ExecStart=%h/PasteBerth/bin/pasteberth --config %h/PasteBerth/config.toml
```

Then install the unit in the user manager:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/pasteberth.service ~/.config/systemd/user/pasteberth.service
# edit ~/.config/systemd/user/pasteberth.service if the paths differ
systemctl --user daemon-reload
systemctl --user enable --now pasteberth.service
journalctl --user -u pasteberth -f
```

The unit uses `PrivateTmp=true`: do not configure a zone below `/tmp` or
`/var/tmp` unless you deliberately disable that isolation, because the service
would see a private temporary directory that other host processes cannot use.
If you enable the commented `ProtectSystem`/`ProtectHome` hardening, add every
zone directory and every parent that `create_directory = true` may create to
`ReadWritePaths`; also include the configured password path if the service must
write it. These paths must match the installed unit, not just the example.

A startup refusal protects against accidental exposure:
**authentication disabled without explicit opt-in = stop with an explicit
message** (`allow_unauthenticated_local` or `allow_unauthenticated_remote`, as
appropriate). A non-loopback HTTP listener also requires
`allow_insecure_http_remote = true`; the recommended configuration remains an
authenticated loopback backend behind an HTTPS reverse proxy. Changing any
configuration used by the unit requires `systemctl --user restart
pasteberth.service`; changing only the password hash does not.

For the user service to survive the last logout and start at boot, enable linger
for the relevant account:

```sh
loginctl enable-linger "$USER"
```

This option keeps a user systemd manager active even without an interactive
session; enable it only if this persistence is desired.

## HTTPS & reverse proxy

Pasteberth transports passwords, sessions, and private paths:
**all untrusted network access must go through HTTPS.** A reverse proxy remains
recommended, but the server can also terminate TLS directly:

```toml
[tls]
enabled = true
certificate = "/absolute/path/cert.pem"
private_key = "/absolute/path/key.pem"
```

For TLS terminated by Caddy or nginx, keep Pasteberth bound to loopback and
proxy to it over the local connection. A non-loopback Pasteberth listener must
use direct `[tls] enabled = true`; an external HTTPS reverse proxy does not make
a separate non-loopback HTTP listener safe. The
`allow_insecure_http_remote = true` option is an explicit exception for a
controlled private network only.

Authentication is a separate choice. An unauthenticated loopback backend
requires `allow_unauthenticated_local = true`, including when a reverse proxy
exposes it to remote clients. An unauthenticated non-loopback listener requires
`allow_unauthenticated_remote = true` as well.

By default `allowed_hosts` is empty, which disables the Host check (wildcard):
any Host header is accepted, and the browser Origin must still match it. For an
exposed deployment, list every hostname clients may use (hostname only,
without scheme, port, or path) — `pasteberth audit` warns while the list is
empty. Leaving it empty is a deliberate tradeoff only when an upstream proxy
independently enforces the canonical hostnames:

For a multi-station deployment, include all public hostnames in the list. Do not
replace a public hostname with local-only names: remote clients then receive
`403 forbidden_host`.

```toml
listen_address = "127.0.0.1"
trusted_proxies = ["127.0.0.1"]  # only if the proxy is the only trusted local client
allowed_hosts = ["pasteberth.example.internal"]
```

`trusted_proxies` is checked by the peer IP of the connection. Trusting
`127.0.0.1` therefore trusts every local process that can connect to the
Pasteberth listener, not just Caddy or nginx. Use it only when local clients
are trusted or the proxy-to-backend path is otherwise isolated; leave it empty
if that trust boundary cannot be guaranteed.

### Caddy (recommended)

```caddy
pasteberth.example.internal {
    reverse_proxy 127.0.0.1:8765
}
```

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name pasteberth.example.internal;
    # ssl_certificate …; ssl_certificate_key …;
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        client_max_body_size 50m;
    }
}
```

In both cases: listen on `127.0.0.1` for Pasteberth, and let
`trusted_proxies` contain only the proxy's IP. It is empty by default, so a
direct local client cannot spoof an IP with `X-Forwarded-For`; configure it
only when the listener is reachable exclusively through the listed proxy and
all processes in that address scope are trusted. The proxy body limit should
be at least the configured `max_upload_size` (20 MiB by default, 50 MiB maximum).
`X-Forwarded-Proto/For` headers from an unlisted peer are **ignored**.

## API

Same-origin only (no CORS in V1; session cookies). The API is useful if the
graphical client is not the supplied browser, or if a script wants to deposit
validated content in a zone.

| Method | Path | Role |
|---|---|---|
| GET | `/api/health` | probe (public) |
| GET | `/api/zones` | zones + counts + group memberships |
| GET | `/api/groups` | configured groups + selections, layouts, and matching zone IDs |
| GET | `/api/zones/{id}/images` | history of all content, newest first |
| POST | `/api/zones/{id}/images` | upload (multipart `image` field; `preserve_name=1` keeps a dragged file's name; `replace=1` explicitly permits replacing a managed name) |
| DELETE | `/api/zones/{id}/images/{file}` | delete a managed content file |
| GET | `/previews/{id}/{file}` | serve stored content for preview/download (protected) |

`/api/groups` returns `groups` in configuration order. Each entry contains
`name`, `selection`, `pattern`, `layout`, `zone_ids`, `zone_count`, `hide_empty`,
and `show_count`.
The `/api/zones` response includes the matching group names in each zone's
`groups` field. Both endpoints are protected when authentication is enabled;
there is no CORS support.

Each zone entry also reports `busy`, `reference_list_prefix`,
`reference_list_suffix`, `reference_separator`, and `allow_zip_download`.
Long-running batch deletion and ZIP operations hold an exclusive server-side
zone lock. Conflicting zone requests receive `423 zone_busy` with
`Retry-After: 1`; clients should refresh the zone and retry after the lock is
released.

| Method | Path | Role |
|---|---|---|
| POST | `/api/zones/{id}/images/batch-delete` | delete several managed content files; the response separates `deleted` and `failed` entries |
| POST | `/api/zones/{id}/images/archive` | stream selected managed content as a ZIP without a temporary server file; accepts repeated `filename` form fields or a JSON `filenames` array |

Despite the route name, `/api/zones/{id}/images` and its `images` response key
cover images, UTF-8 text, and opaque binary content. Raw requests accept the
supported image, text, JSON, XML, and YAML MIME types, `application/octet-stream`,
or no declared type. Unsupported declared types return `415`.

Example:

```sh
curl -b cookies.txt -F image=@capture.png \
     https://pasteberth.example.internal/api/zones/default/images
```

```json
{
  "id": "2026-08-25_01-22-31_a81c42.png",
  "filename": "2026-08-25_01-22-31_a81c42.png",
  "created_at": "2026-08-24T23:22:31.412000+00:00",
  "width": 1920, "height": 1080, "size": 9283, "format": "png",
  "kind": "image", "mime": "image/png",
  "preview_url": "/previews/default/2026-08-25_01-22-31_a81c42.png",
  "reference": "@/path/to/repository/storage/default/2026-08-25_01-22-31_a81c42.png"
}
```

For images, formats are PNG, JPEG, and WebP — determined by **content** (magic
bytes + bounded structure), never by the declared MIME type. An image-looking
upload that fails this structural check is retained as opaque binary instead of
being previewed; it is rejected when `accept_bin = false`. Empty uploads and
unknown content still follow the normal text/binary classification. Pixel
decompression remains the browser/harness responsibility. Valid UTF-8 content
without NUL bytes is text; other bytes are opaque binary. The response uses
`kind` values `image`, `text`, or `binary`; non-image items have `null`
dimensions and format.

Without `preserve_name=1`, names are generated server-side
(`YYYY-MM-DD_HH-MM-SS_<6 hex>.ext`, creation with `O_EXCL`: no overwriting).
With `preserve_name=1`, a valid multipart filename is retained. If a file with
that name is already managed by Pasteberth, `replace=1` is required; without it
the API returns `428 replacement_required` before writing anything. With the
flag, its content and sidecar are replaced atomically. A foreign file is never
overwritten and returns `409 storage_conflict`. Free space below the threshold
returns `507 storage_low`. A retention error returns `503 retention_error` after
the content is created; the client must therefore reload the history before
blindly retrying.

## Security

- Passwords: salted scrypt (N=16384), constant-time comparison, a `passwd` file
  0600 next to the configuration by default (or at `[auth] password_file`) and
  ignored by Git only for the repository-default filename; delay + progressive
  per-IP lockout (honoring XFF only through a trusted proxy).
- Login requests are limited to 4 KiB, scrypt checks are globally bounded, and
  uploads share a 128 MiB memory budget; uploads remain limited to 20 MiB by
  default and 50 MiB maximum.
- Server-side, revocable sessions (logout takes effect), 256-bit token,
  `HttpOnly; SameSite=Lax` cookie + `Secure` as soon as the effective scheme
  is HTTPS.
- CSRF: SameSite=Lax + every unsafe request with `Origin`/`Referer` must match
  the served host (403 otherwise). No `Access-Control-*`.
- Strict CSP without inline content, `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store` on the UI/API.
- Previews and protected API routes require the session; `/api/health` is public.
  User filenames reject path separators, NUL, CR/LF, reserved Pasteberth names,
  and exceed neither 200 characters nor 240 UTF-8 bytes; preview membership is
  still required.
- Only files with a valid, coherent Pasteberth sidecar can be read, deleted,
  renamed, or replaced. Explicit filesystem deletion can remove a structurally
  valid pair whose recorded size is stale; files without a durable ownership
  marker, including names that merely look generated, are preserved.
- Writable target directories warn on non-private modes; private mode
  (`0700`) is recommended. Private stored files/sidecars (`0600`), symbolic links
  refused, and temporary files
  reconciled after a crash. Read-only shared directory modes are warned about.
- Bounded structural validation of PNG/JPEG/WebP, dimensions and pixel budget;
  image-looking data that fails validation is retained as a binary attachment,
  not served as an image preview.
- **Image validation is structural, not a full codec decode**: containers,
  dimensions, chunk/segment structure and pixel budgets are checked without
  decoding the bitstream. A structurally valid but undecodable file (e.g. a
  truncated WebP or a minimal JPEG) can be stored and produce a broken preview;
  it is never executed server-side. Full decode validation is a V2 candidate.
- Retention under a per-zone lock: deterministic ordering, safe concurrent
  uploads (dedicated tests).

## License

Pasteberth is licensed under the **GNU Affero General Public License v3.0 or later** (AGPL-3.0-or-later).

The full license text is available in [`LICENSE`](LICENSE) and at <https://www.gnu.org/licenses/agpl-3.0.html>.

Key implication: if you run a modified version of Pasteberth on a publicly accessible server (including behind a reverse proxy), you must offer the corresponding source code to users of that server. See section 13 of the AGPL.

## Tests

```sh
npm ci
npm run test:all              # Python + browser in parallel
# targeted: python3 -m unittest discover -s tests -v
# targeted: npm run test:e2e
```

The suite covers: image validation (PNG/JPEG/WebP, structural corruption, spoofing),
image/text/binary classification, filename preservation/replacement/downloads,
filesystem-drop/rename/delete, configuration & startup policy, storage/retention/ownership,
auth/sessions/anti-brute-force, multipart parser, full HTTP integration
(auth, CSRF/Origin, proxies, headers, secret leakage), concurrency
(parallel uploads in the same/multiple zones, readers during writes, named
replacement conflicts, zone locks, multi-delete and streamed ZIP archives),
CLI (passwd, refusal of dangerous configuration), frontend contracts, and
Playwright browser scenarios on a real Pasteberth server: loading and keyboard
selection, group filtering and area/tab layouts, hover/focus paste targeting,
Shift-click multi-selection, paste without a zone, image/text/binary upload,
preview, selection in the index, drag and drop, replacement confirmation, and
exact-name downloads.
Browser tests use Chromium by default; `E2E_BROWSER=firefox` is available if the
corresponding Playwright browser is installed.

## Limitations & V2

- Single `local` destination type; each zone uses an absolute path as seen by the
  server. The `Destination` abstraction is ready for an `SshDestination` (SFTP,
  with credentials remaining on the server).
- No browser extension: the API can be used as-is, but dedicated CORS will be
  added explicitly when the time comes.
- In-memory sessions: a restart disconnects users (deliberate, simple). Manual
  deletion of stored content outside retention is available from the UI.
- Single-password authentication in V1; filesystem permissions can nevertheless
  organize multiple zones or users.
- TLS is delegated to a reverse proxy with a loopback Pasteberth backend, or
  terminated directly by Pasteberth on a non-loopback listener.
- Mixed pastes are serialized as HTML with base64-embedded images (~33% larger
  than the raw image); very large screenshots can hit `max_upload_size` where
  an image-only upload would fit.
