[English](README.md) | [Français](README.fr.md)

# Pasteberth

**The bridge between a graphical clipboard and a CLI/TUI harness that cannot
easily receive images.**

Pasteberth is designed first and foremost for harnesses that work in a
terminal — OpenCode and similar tools. These tools are very good at reading a
file on their machine, but they do not always have access to the workstation's
graphical clipboard, nor a convenient way to receive a screenshot.

Pasteberth therefore keeps the transfer deliberately simple: the browser
receives the image, the server writes it to the filesystem of the harness
machine, then returns the exact path to paste into the terminal.

You take a screenshot on your workstation, paste it (Ctrl+V) into the area for
the right project in your browser, and retrieve a filesystem reference ready to
paste into the harness:

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
```

The browser **never** needs to access the returned path. This is the path seen
by the harness, on the machine where Pasteberth runs.

The current version is `1.0.2`.

---

## Contents

1. [Use Cases](#use-cases)
2. [How It Works](#how-it-works)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Password](#password)
6. [Launching & systemd service](#launching--systemd-service)
7. [HTTPS & reverse proxy](#https--reverse-proxy)
8. [API](#api)
9. [Security](#security)
10. [Tests](#tests)
11. [Limitations & V2](#limitations--v2)

---

## Use Cases

Pasteberth is useful when the two sides of the work are not in the same
environment:

- the screenshot is taken in a graphical environment, possibly on another
  workstation;
- the harness works in a terminal or a remote session;
- the harness can read the filesystem on its machine, but cannot receive an
  image directly from the graphical clipboard;
- you want to keep a few captures per project without creating a general-purpose
  image-sharing service.

Pasteberth is not public storage, a CDN, or a synchronization tool between
users. It is a local, targeted gateway between a graphical interface and a
terminal process.

## How It Works

- **One zone per project**: each zone has an independent identifier, label,
  color, directory, and retention policy. Click a zone to make it active, then
  use Ctrl+V or drag and drop.
- **Selected image panel**: after an upload, the new image is selected at the
  top with its name, reference, and the `Copy link`, `Copy image`, `Clear`, and
  `Zoom` actions.
- **Image index**: the thumbnails at the bottom form the zone's complete
  history, newest first. Click a thumbnail to select it in the upper panel; the
  selected thumbnail is marked.
- **Clipboard**: after an upload, Pasteberth tries to copy the exact reference
  to the clipboard. `Copy link` copies that reference, `Copy image` copies the
  image itself, and `Clear` replaces the clipboard contents with empty text,
  within the limits of the browser's Clipboard permissions.
- **Exact references**: the server builds and returns the path; the frontend
  copies it as-is, never reconstructing it client-side. The prefix and suffix
  are configurable, for example to obtain `` `/path/image.png` ``.
- **Circular retention per zone** (`retain = N`): beyond N images, the oldest
  ones are deleted — only files created by Pasteberth with their JSON sidecar.
- **Persistent page**: intended to remain open for hours; thumbnails come from
  the server, no Blob URL accumulates, and the history is resynchronized every
  45 seconds and when returning to the tab.

For OpenCode's `@` selector to find images directly, place the zone in the
workspace opened by OpenCode, or open a workspace that contains both the
project and the capture directory. Otherwise, the path remains valid for an
explicit read by the harness.

## Installation

Prerequisite: **Python ≥ 3.11**, no third-party dependencies (standard library
only). The repository is the installation itself; no installation script or
root access is required.

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
| `listen_address` | `"127.0.0.1"` | listening address; non-loopback requires explicit HTTPS |
| `port` | `8765` | TCP port |
| `max_upload_size` | `"20MiB"` | per-upload limit (20 MiB by default, 50 MiB maximum) |
| `max_image_pixels` | `25000000` | decoding budget (25 MP by default, 50 MP maximum) |
| `trusted_proxies` | loopback | only these peers may set `X-Forwarded-*` |
| `allowed_hosts` | `[]` | hostnames accepted by browser Origin/Referer checks; required behind a public reverse proxy |
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
OpenCode reads the images, not your browser.

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
images are valuable. An external path can be specified manually in
`config.toml`.

Zones must be distinct, writable target directories. Private mode `0700` is
recommended; group/other-writable modes are rejected, while group/other-readable
or executable modes produce an audit warning and can be used for controlled
sharing between multiple users. Each zone refuses a new upload if the free space expected after writing would fall below
`min_free_percent` (default `2.0`). The measurement applies to the directory's
filesystem, not just the folder; multiple zones can therefore share a
filesystem, but then they also share its free-space reserve.

Images are limited to `16 384 × 16 384` pixels and `25 MP` by default, which
covers usual 4K to 6K displays. 8K images exceeding `25 MP` require an explicit
budget extension.

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

For a reverse proxy, list its public hostname in `allowed_hosts` (hostname only,
without scheme, port, or path):

```toml
listen_address = "127.0.0.1"
trusted_proxies = ["127.0.0.1"]
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
`trusted_proxies` contain only the proxy's IP. `X-Forwarded-Proto/For` headers
from an unlisted peer are **ignored** (an Internet client cannot force a
`Secure` cookie or spoof an IP with the rate limiter).

## API

Same-origin only (no CORS in V1; session cookies). The API is useful if the
graphical client is not the supplied browser, or if a script wants to deposit a
validated image in a zone.

| Method | Path | Role |
|---|---|---|
| GET | `/api/health` | probe (public) |
| GET | `/api/zones` | zones + counts |
| GET | `/api/zones/{id}/images` | history, newest first |
| POST | `/api/zones/{id}/images` | upload (multipart `image` field, or raw `image/*` / `application/octet-stream` body) |
| GET | `/previews/{id}/{file}` | thumbnail (protected) |

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
  "preview_url": "/previews/default/2026-08-25_01-22-31_a81c42.png",
  "reference": "@/path/to/repository/storage/default/2026-08-25_01-22-31_a81c42.png"
}
```

Formats: PNG, JPEG, WebP — determined by **content** (magic bytes + structure),
never by the declared MIME type. Rejections: empty, too large, unknown format,
incomplete or structurally malformed container. Pixel decompression remains the
browser/harness responsibility. Names are generated server-side
(`YYYY-MM-DD_HH-MM-SS_<6 hex>.ext`, creation with `O_EXCL`: no overwriting).
Free space below the threshold returns `507 storage_low`. A retention error
returns `503 retention_error` after the image is created; the client must
therefore reload the history before blindly retrying.

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
- Previews and API require the session; a filename cannot traverse (strict
  `[A-Za-z0-9._-]` + history membership required).
- Only files with a Pasteberth sidecar can be read or deleted. Files matching
  Pasteberth's exact capture naming (`YYYY-MM-DD_HH-MM-SS_<6hex>.<ext>`) that
  lack a sidecar and are older than one hour are removed during startup
  reconciliation (crash recovery); other personal files are never touched.
- Writable target directories reject group/other write bits; private mode
  (`0700`) is recommended. Private images/sidecars (`0600`), symbolic links
  refused, and temporary files
  reconciled after a crash. Read-only shared directory modes are warned about.
- Complete structural validation of PNG/JPEG/WebP, dimensions and pixel budget,
  and rejection of truncated containers.
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
configuration & startup policy, storage/retention/ownership,
auth/sessions/anti-brute-force, multipart parser, full HTTP integration
(auth, CSRF/Origin, proxies, headers, secret leakage), concurrency
(parallel uploads in the same/multiple zones, readers during writes),
CLI (passwd, refusal of dangerous configuration), frontend contracts, and six
Playwright browser scenarios on a real Pasteberth server: loading and keyboard
selection, paste without a zone, upload/preview, selection in the index, and
drag and drop.
Browser tests use Chromium by default; `E2E_BROWSER=firefox` is available if the
corresponding Playwright browser is installed.

## Limitations & V2

- Single `local` destination (relative to the server). The `Destination`
  abstraction is ready for an `SshDestination` (SFTP, with credentials remaining
  on the server).
- No browser extension: the API can be used as-is, but dedicated CORS will be
  added explicitly when the time comes.
- In-memory sessions: a restart disconnects users (deliberate, simple);
  manual deletion of an image outside retention remains to be provided in the
  UI.
- Single-password authentication in V1; filesystem permissions can nevertheless
  organize multiple zones or users.
- TLS delegated to the reverse proxy or terminated directly by Pasteberth.
