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

The current version is `1.0.7`.

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
  color, directory, and retention policy. Click a zone to make it active, then
  use Ctrl+V or drag and drop. The filesystem command targets a zone by its
  configured server-side directory, not by its UI label or identifier.
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
  have a type marker. Click an item to select it in the upper panel; the
  selected item is marked.
- **Clipboard**: after an upload, Pasteberth tries to copy the exact reference
  to the clipboard. `Copy link` copies that reference, `Copy Image` copies the
  image itself, `Copy Text` copies text, and `Clear` replaces the clipboard
  contents with empty text, within the limits of the browser's Clipboard
  permissions.
- **Exact references**: the server builds and returns the path; the frontend
  copies it as-is, never reconstructing it client-side. The prefix and suffix
  are configurable, for example to obtain `` `/path/image.png` ``.
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
pasteberth filesystem-drop --replace \
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
./bin/pasteberth passwd
./bin/pasteberth
```

Before the first start, check the environment:

```sh
./bin/pasteberth audit --config config.toml
```

An audit that reports only warnings returns `1`; configuration errors return
`2`.

To use the command from any directory, add `bin/` to the `PATH` or use
`./bin/pasteberth` directly.

```sh
export PATH="$PWD/bin:$PATH"
pasteberth
```

## Configuration

After generation, the local file is `config.toml` at the repository root and
remains ignored by Git. An explicit configuration can also be provided with
`--config PATH` or `$PASTEBERTH_CONFIG`. An older XDG configuration at
`~/.config/pasteberth/config.toml` remains recognized. See the commented
[`config.example.toml`](config.example.toml).

Without `config.toml`, `pasteberth` intentionally starts in minimal mode,
loopback-only, with storage at `<repository>/storage/default` and no
authentication. A warning is displayed at every start. The same warning
appears if a modified configuration continues to target this default storage.
This mode is for a first local trial, not for exposure through a reverse proxy.

`pasteberth --generate-config` generates a secure configuration with
authentication enabled. Then manually edit `config.toml` according to the
desired zones and paths. The file remains ignored by Git.

| Key | Default | Role |
|---|---|---|
| `listen_address` | `"127.0.0.1"` | listening address; non-loopback requires TLS or an explicit private-network HTTP opt-in |
| `port` | `8765` | TCP port |
| `max_upload_size` | `"20MiB"` | per-upload limit (20 MiB by default, 50 MiB maximum) |
| `max_image_pixels` | `25000000` | decoding budget (25 MP by default, 50 MP maximum) |
| `accept_img` | `true` | accept structurally valid PNG, JPEG, and WebP images |
| `accept_doc` | `true` | accept valid UTF-8 text content |
| `accept_bin` | `true` | accept opaque binary content |
| `trusted_proxies` | `[]` | only these peers may set `X-Forwarded-*`; configure the actual reverse proxy IPs explicitly |
| `allowed_hosts` | `[]` | hostnames accepted by Host/Origin checks; empty = wildcard (audit warns). List hostnames to enforce a strict allowlist |
| `allow_unauthenticated_local` | `false` | explicit opt-in for anonymous loopback/proxy mode |
| `allow_unauthenticated_remote` | `false` | explicit unlock (discouraged) |
| `allow_insecure_http_remote` | `false` | separate opt-in for non-loopback HTTP (private network only) |
| `log_level` | `"INFO"` | DEBUG/INFO/WARNING/ERROR |
| `[tls] enabled` | `false` | terminates TLS directly with `certificate` and `private_key` |
| `[auth] enabled` | `true` | password protection |
| `[auth] session_ttl_hours` | `72` | server session lifetime |
| `[auth] password_file` | next to `config.toml` | absolute path to the `passwd` hash (regular 0600 file) |
| `[[zones]] …` | `default` | `id`, `label`, `type=local`, `directory`, `retain`, `reference_prefix`, `reference_suffix`, `color` (#RRGGBB), `create_directory`, `min_free_percent` |

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

Images are limited to `16 384 × 16 384` pixels and `25 MP` by default, which
covers usual 4K to 6K displays. 8K images exceeding `25 MP` require an explicit
budget extension.

For a dropped file, `preserve_name=1` can retain its original filename. Names
are limited to 200 characters and 240 UTF-8 bytes; `/`, `\`, NUL, CR/LF, `.`,
`..`, `.pasteberth.lock`, and Pasteberth's temporary prefixes are reserved or
rejected. An invalid name returns `400`. Names starting with a dot, such as
`.env`, are supported and appear in the history.

## Password

```sh
pasteberth passwd            # prompt + confirmation, salted scrypt hash
                             # writes to password_file or next to config.toml (0600)
```

The password is never stored in plaintext or written to config.toml; the hash
is verified with `hashlib.scrypt` plus a constant-time comparison. A change
takes effect immediately (reloaded on every attempt), without restarting the
service, and invalidates existing sessions. The server refuses to start if
authentication is enabled without a readable, valid `passwd` file.

## Launching & systemd service

```sh
pasteberth                         # foreground, default storage if needed
pasteberth audit                   # check without modification
systemctl --user enable --now pasteberth.service   # optional
journalctl --user -u pasteberth -f                 # logs
```

The supplied unit (`deploy/pasteberth.service`) is optional and requires no
root. Adapt its `WorkingDirectory`, `ExecStart`, and configuration path to the
actual repository before enabling it. Example if the repository is in
`~/PasteBerth` and the configuration is in `config/`:

```ini
[Service]
WorkingDirectory=%h/PasteBerth
ExecStart=%h/PasteBerth/bin/pasteberth --config %h/PasteBerth/config/config.toml
```

Then install the unit in the user manager:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/pasteberth.service ~/.config/systemd/user/pasteberth.service
systemctl --user daemon-reload
systemctl --user enable --now pasteberth.service
```

A startup refusal protects against accidental exposure:
**authentication disabled without explicit opt-in = stop with an explicit
message** (`allow_unauthenticated_local` or `allow_unauthenticated_remote`, as
appropriate). A non-loopback HTTP listener also requires
`allow_insecure_http_remote = true`; the recommended configuration remains a
loopback backend behind an HTTPS reverse proxy.

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

With a non-loopback listener, enable TLS or use an HTTPS reverse proxy. The
`allow_insecure_http_remote = true` option should only be used on a controlled
private network.

By default `allowed_hosts` is empty, which disables the Host check (wildcard):
any Host header is accepted, and the browser Origin must still match it. To
enforce a strict allowlist, list the hostnames you expose (hostname only,
without scheme, port, or path) — `pasteberth audit` warns while the list is
empty:

For a multi-station deployment, leave this key absent or set it to `[]` when
the public hostname is supplied by the deployment rather than fixed in the
configuration. Replacing the default with local-only hostnames causes remote
clients to receive `403 forbidden_host`. Use a non-empty list only when every
client reaches the service through one of the listed hostnames.

```toml
listen_address = "127.0.0.1"
trusted_proxies = ["127.0.0.1"]  # only if this is the actual proxy peer
allowed_hosts = ["pasteberth.example.internal"]
```

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
        client_max_body_size 25m;
    }
}
```

In both cases: listen on `127.0.0.1` for Pasteberth, and let
`trusted_proxies` contain only the proxy's IP. It is empty by default, so a
direct local client cannot spoof an IP with `X-Forwarded-For`; configure it
only when the listener is reachable exclusively through the listed proxy.
`X-Forwarded-Proto/For` headers from an unlisted peer are **ignored**.

## API

Same-origin only (no CORS in V1; session cookies). The API is useful if the
graphical client is not the supplied browser, or if a script wants to deposit
validated content in a zone.

| Method | Path | Role |
|---|---|---|
| GET | `/api/health` | probe (public) |
| GET | `/api/zones` | zones + counts |
| GET | `/api/zones/{id}/images` | history of all content, newest first |
| POST | `/api/zones/{id}/images` | upload (multipart `image` field; `preserve_name=1` keeps a dragged file's name; `replace=1` explicitly permits replacing a managed name) |
| DELETE | `/api/zones/{id}/images/{file}` | delete a managed content file |
| GET | `/previews/{id}/{file}` | serve stored content for preview/download (protected) |

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

- Passwords: salted scrypt (N=16384), constant-time comparison, `passwd` file
  0600 next to the configuration and ignored by Git; delay + progressive
  per-IP lockout (honoring XFF only through a trusted proxy).
- Login requests are limited to 16 KiB, scrypt checks are globally bounded, and
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
- Retention under a per-zone lock: deterministic ordering, safe concurrent
  uploads (dedicated tests).

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
replacement conflicts),
CLI (passwd, refusal of dangerous configuration), frontend contracts, and
Playwright browser scenarios on a real Pasteberth server: loading and keyboard
selection, paste without a zone, image/text/binary upload, preview, selection in
the index, drag and drop, replacement confirmation, and exact-name downloads.
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
- TLS delegated to the reverse proxy or terminated directly by Pasteberth.
- Mixed pastes are serialized as HTML with base64-embedded images (~33% larger
  than the raw image); very large screenshots can hit `max_upload_size` where
  an image-only upload would fit.
