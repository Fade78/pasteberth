# Configuration Reference

[Documentation map](../../GUIDE.md).

## Configuration discovery

For normal execution, an existing configuration is selected in this order:

1. explicit `--config PATH`;
2. `PASTEBERTH_CONFIG`;
3. `$XDG_CONFIG_HOME/pasteberth/config.toml` (normally
   `~/.config/pasteberth/config.toml`);
4. built-in minimal mode when no file exists.

An explicit or environment-selected path that does not exist is an error, not
a request to fall back to minimal mode. Minimal mode is loopback-only and
unauthenticated; the generated configuration enables authentication.

`--generate-config` writes to the explicit path or `PASTEBERTH_CONFIG`, then
to `$XDG_CONFIG_HOME/pasteberth/config.toml`. It never writes inside the
read-only deployment.
By default it refuses to replace an existing file; add the global `--force`
option only after checking the target path.

The active configuration, password file, TLS private key, and zone directories
must be outside `PasteBerth/`. These paths may contain symbolic links; Pasteberth
resolves the target before opening it and checks the target and its parents.

Use the same explicit `--config PATH` for `passwd`, `audit`, the server, and
filesystem commands when more than one configuration exists.

## Configuration

Start from [`PasteBerth/support/config.example.toml`](../../PasteBerth/support/config.example.toml).
Configuration is TOML and is loaded when the service starts. Restart the server after changing
listeners, TLS, proxy trust, host allowlists, upload limits, zones, groups, or
authentication settings. The password hash is reloaded for every login
attempt, so changing it does not require a restart.

### Top-level keys

| Key | Default | Meaning |
|---|---:|---|
| `listen_address` | `127.0.0.1` | Address on which the HTTP server listens. |
| `port` | `8765` | TCP listening port. |
| `max_upload_size` | `20MiB` | Maximum size of one upload; use `"unlimited"` for no application cap. |
| `max_image_pixels` | `25000000` | Structural image pixel budget; use `"unlimited"` to disable it. |
| `url_prefix` | `""` | Public path prefix such as `/paste`; the proxy must preserve it. |
| `show_full_path` | `true` | Display absolute file references in the Web UI; set `false` when paths are sensitive. |
| `trusted_proxies` | `[]` | IP addresses or CIDR networks allowed to provide `X-Forwarded-*`. |
| `allowed_hosts` | `[]` | Hostnames or IP addresses accepted by Host and Origin checks; empty means wildcard and triggers an audit warning. |
| `allow_unauthenticated_local` | `false` | Explicit opt-in for anonymous loopback or proxy mode. |
| `allow_unauthenticated_remote` | `false` | Explicit opt-in for anonymous non-loopback mode; discouraged. |
| `allow_insecure_http_remote` | `false` | Explicit opt-in for non-loopback HTTP on a controlled private network. |
| `accept_img` | `true` | Accept structurally valid PNG, JPEG, and WebP images. |
| `accept_doc` | `true` | Accept valid UTF-8 text without NUL bytes. |
| `accept_bin` | `true` | Accept opaque binary content. |
| `log_level` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

All three `accept_*` switches may be disabled, but disabling all of them
refuses every upload.

The `max_upload_size` limit applies to the extracted content. Multipart framing
and all other operational budgets are controlled in the optional `[limits]`
table. Most numeric or size values there accept `"unlimited"`; the listen
backlog is the exception.

The available `[limits]` keys are `max_image_dimension`, `max_image_raw_size`,
`max_filename_length`, `max_filename_size`, `max_png_chunks`,
`max_jpeg_segments`, `max_webp_chunks`, `max_mime_length`,
`max_multipart_boundary_length`, `max_multipart_parts`,
`max_multipart_header_size`, `max_multipart_field_name_length`,
`max_multipart_body_size`, `max_batch_names`, `max_batch_body_size`,
`max_archive_size`, `max_archive_duration_seconds`, `max_comment_body_size`,
`max_http_header_size`, `max_login_body_size`, `max_login_fields`,
`max_login_delay_seconds`, `max_login_concurrent_checks`,
`max_login_tracked_ips`, `login_forget_after_seconds`,
`max_scrypt_memory_size`, `max_password_file_size`,
`max_metadata_size`, `max_comment_length`, `max_comment_bytes`,
`request_queue_size`, `max_active_requests`, `max_pending_requests`,
`http_header_timeout_seconds`, and `http_request_timeout_seconds`.
`request_queue_size` must remain a positive integer because it is passed to the
operating-system listen backlog; the other numeric and size budgets may use
`"unlimited"` where the operation supports it.

`max_multipart_body_size` defaults to `24MiB` and limits the complete multipart
request, including framing and auxiliary fields. It is independent from
`max_upload_size`, whose limit applies to the extracted content. The archive
limits default to `256MiB` of uncompressed selected files and `300` seconds of
total streaming time; ZIP output remains streamed without a temporary archive.

#### Operational budget defaults

These are TOML key names, not the private Python attribute names. Size values
accept a byte count or a size string such as `"8KiB"` or `"20MiB"`.

| `[limits]` Key | Default | Budget |
|---|---:|---|
| `max_image_dimension` | `16384` | Width or height of a structurally validated image. |
| `max_image_raw_size` | `"256MiB"` | Encoded image input to structural validation. |
| `max_filename_length` | `200` | Filename characters. |
| `max_filename_size` | `240` | Filename UTF-8 bytes. |
| `max_png_chunks` | `100000` | PNG chunks. |
| `max_jpeg_segments` | `100000` | JPEG segments. |
| `max_webp_chunks` | `100000` | WebP chunks. |
| `max_mime_length` | `120` | Declared MIME string length. |
| `max_multipart_boundary_length` | `70` | Multipart boundary length. |
| `max_multipart_parts` | `32` | Multipart parts. |
| `max_multipart_header_size` | `"8KiB"` | Headers of one multipart part. |
| `max_multipart_field_name_length` | `256` | Multipart field name length. |
| `max_multipart_body_size` | `"24MiB"` | Complete multipart upload body. |
| `max_batch_names` | `10000` | Filenames in one batch selection. |
| `max_batch_body_size` | `"2MiB"` | Batch/transfer and direct-drop JSON request body. |
| `max_archive_size` | `"256MiB"` | Total uncompressed selected files. |
| `max_archive_duration_seconds` | `300` | Total archive streaming duration. |
| `max_comment_body_size` | `"8KiB"` | Comment request body. |
| `max_http_header_size` | `"64KiB"` | HTTP request header budget. |
| `max_login_body_size` | `"4KiB"` | Login request body. |
| `max_login_fields` | `8` | URL-encoded login fields. |
| `max_login_delay_seconds` | `900` | Maximum progressive login backoff. |
| `max_login_concurrent_checks` | `4` | Concurrent password checks. |
| `max_login_tracked_ips` | `4096` | IP entries in the login limiter. |
| `login_forget_after_seconds` | `3600` | Idle login-limiter history lifetime. |
| `max_scrypt_memory_size` | `"64MiB"` | Memory budget for scrypt verification. |
| `max_password_file_size` | `"16KiB"` | Password hash file read. |
| `max_metadata_size` | `"64KiB"` | Sidecar read. |
| `max_comment_length` | `280` | Comment Unicode characters. |
| `max_comment_bytes` | `1024` | Comment UTF-8 bytes. |
| `request_queue_size` | `32` | OS listen backlog; must be a positive integer. |
| `max_active_requests` | `64` | Active request admission limit. |
| `max_pending_requests` | `8` | Connections waiting for headers. |
| `http_header_timeout_seconds` | `5` | Pending-header timeout. |
| `http_request_timeout_seconds` | `60` | Request timeout; streaming responses also have activity and archive deadlines. |

Removing one budget does not remove the others or a proxy's limits. Large
uploads and multipart bodies are read into memory; `"unlimited"` is not a
bounded-memory streaming-upload mode. ZIP downloads, by contrast, are streamed.

### TLS

Pasteberth can terminate TLS directly:

```toml
[tls]
enabled = true
certificate = "/absolute/path/cert.pem"
private_key = "/absolute/path/key.pem"
```

The recommended deployment keeps Pasteberth on loopback and terminates HTTPS
in Caddy or nginx. A non-loopback listener must use direct TLS unless the
explicit private-network HTTP exception is enabled.

### Authentication

```toml
[auth]
enabled = true
session_ttl_hours = 72
max_sessions = 4096
# password_file = "/absolute/path/to/passwd"
```

The password file defaults to `passwd` next to the selected configuration. It
contains a salted scrypt hash and should be readable only by the service user.
`pasteberth passwd` creates or replaces it safely. `max_sessions` bounds live
authenticated sessions held in memory; when the bound is reached, the oldest
session is evicted before a new one is created. Use `"unlimited"` to disable
FIFO eviction. It does not limit TCP connections or pending unauthenticated
requests.

Authentication uses one shared password, not individual accounts. A session
can access all configured zones; zone groups are not access-control lists.
Changing the password file invalidates existing sessions on their next
validation; each session is tied to the file version at login. A restart is
not required. See [credential rotation](../operations.md#credentials-and-sessions).

### Zones

Each `[[zones]]` table defines one independent project area:

| Key | Default | Meaning |
|---|---:|---|
| `id` | required | Lowercase API/UI identifier, up to 64 characters. |
| `label` | `id` | Human-readable UI label. |
| `type` | `local` | Only `local` is implemented in v2.1.21. |
| `directory` | required | Absolute path as seen by the server and the harness. |
| `retain` | `10` | Number of managed items retained in the zone. |
| `reference_prefix` | `@` | Text prepended to one returned reference. |
| `reference_suffix` | empty | Text appended to one returned reference. |
| `reference_list_prefix` | empty | Prefix for a copied list of references. |
| `reference_list_suffix` | empty | Suffix for a copied list of references. |
| `reference_separator` | `,` | Separator between references in a copied list. |
| `allow_zip_download` | `true` | Allow ZIP download for a multiple selection. |
| `color` | `#243447` | Six-digit zone background color. |
| `create_directory` | `true` | Create a missing zone directory when safe. |
| `min_free_percent` | `2.0` | Required free-space reserve on the zone filesystem. |
| `storage_mode` | `sidecar` | The managed-pair contract. Legacy `directory` values are normalized to `sidecar` with a warning. |
| `max_items` | none | Legacy setting; ignored after normalization unless used to populate `retain`. |
| `file_group` | none | Optional POSIX group name or numeric GID for files created by Pasteberth. The daemon account and any other process that must read or modify those files must be allowed to use the group. |

The directory is not a browser path. It is the exact server-side directory
where the harness reads the stored content and where `drop` writes.
Zone directories must be distinct. A private `0700` directory is recommended;
deliberately shared directories are allowed but produce an audit warning when
their permissions are broad.

For a shared POSIX zone, use one common group, a `setgid` directory, and group
membership for every writer and for the daemon process. A `file_group` setting
does not grant access to the directory or add a group to a running process. The
group must be present in the credentials of the actual daemon process, not only
in the shell that ran `register`. Prefer ordinary group membership and
filesystem `setgid` permissions over filesystem-specific ACLs when the zone must
work across several filesystems.

If a user is added to the shared group after a `systemd --user` manager has
started, log out and in again (or reboot) before restarting Pasteberth. A
`systemctl --user daemon-reload` only rereads the unit; it does not refresh the
manager's supplementary groups.

Example:

```toml
[[zones]]
id = "project-alpha"
label = "Project Alpha"
type = "local"
directory = "/srv/pasteberth-data/project-alpha"
retain = 10
reference_prefix = "@"
reference_suffix = ""
reference_list_prefix = ""
reference_list_suffix = ""
reference_separator = ","
allow_zip_download = true
color = "#304237"
min_free_percent = 2.0
```

To copy a reference enclosed in backticks:

```toml
reference_prefix = "`"
reference_suffix = "`"
```

### Zone groups

Groups control which zones are visible and how they are laid out. They do not
restrict API access or isolate users:

| Key | Values | Meaning |
|---|---|---|
| `name` | non-empty string | Tab label. |
| `selection` | `all`, `pattern`, `other` | How zones are selected. |
| `pattern` | list of Python regexes | Required for `pattern`; uses case-sensitive `re.search`. |
| `layout` | `area`, `tab` | Grid view or opened-zone tab view. |
| `hide_empty` | boolean | Hide a group with no matching zones. |
| `show_count` | boolean | Show the matching zone count in the tab. |

Defaults are `selection = "pattern"`, `layout = "area"`,
`hide_empty = false`, and `show_count = true`.

Without a `[[groups]]` section, all zones are displayed through an implicit
`All` view and no group bar is shown. An `all` group contains every zone. A
`pattern` group matches zone or collection IDs, not labels; matching a collection
selects all of its zones. An `other` group contains zones
not selected by any `pattern` group; `all` groups are deliberately ignored for
that calculation.

Groups are loaded at startup. `pasteberth audit` reports redundant groups,
ignored patterns on `all`/`other`, and equivalent effective selections.
The Group options menu can show or hide the left zone column for a `tab` group;
that preference is kept separately for each group in the browser.

### Zone collections

Repeatable `[[zone_collection]]` rules expose existing directories below an
absolute `base_directory` when their resolved relative path matches `pattern`.
Each collection has an ID such as `@repositories`; it does not own or generate
a group. A `selection = "pattern"` group can match that ID and selects every
zone discovered by the collection. A configuration may use collections without
any static `[[zones]]` entries.

`label_mode` defaults to `"git-or-relative"`; `"relative"` uses the complete
path below `base_directory`, while `"first-directory"` uses its first
directory component as the zone label.

Several collections may contain the same zone. Their zone behavior must agree;
when retention, references, permissions, or another zone setting conflicts, the
candidate is rejected and reported by `audit`.

Discovery does not create directories or edit configuration. If `color` is
omitted, the zone color is deterministic and distinct within its collections;
an explicit `color` remains authoritative for the rule. The discovered
directory must already be readable, writable, and traversable by the server
account; discovery alone may succeed without write permission, but uploads and
sidecars will not.

Regular files copied or moved directly into a collection zone have no coherent
sidecar, so they remain foreign and are ignored. Uploads through the browser,
API, or CLI create the data/sidecar pair. A visible browser polls `/api/zones`
every 10 seconds; a hidden tab refreshes when it becomes visible. Overview
requests use background discovery and return the last completed registry while
it runs. A new matching directory appears after a scan observes it and a later
poll reads that registry, without a daemon restart. `/api/groups` uses the same
background discovery path.

**Unreleased (runtime version still `2.1.21`):** both endpoints share a
background cooldown of `max(10 seconds, last full refresh duration)`, measured
from completion and including registry installation. Startup, foreground, and
failed refresh attempts also set the cooldown. The next eligible poll can
start one job; expiry alone starts nothing. Explicit service actions bypass
the cooldown or wait for an in-flight refresh, without a second refresh within
the same action. These are not configurable timing settings or hard deadlines.
Overview history and free-space reads remain synchronous and can still block.

**Unreleased:** scanner caches share filesystem observations across rules only
within a discovery pass; later passes read the filesystem again. Collection
and group regex semantics are unchanged. The complete discovery contract,
including cache boundaries, aliases, diagnostics, permissions, group
expansion, and lifecycle, is in
[`docs/zone-collection-contract.md`](../zone-collection-contract.md).

For a working project-tree setup, see [provisioning](../provisioning.md) and
[project zones](../recipes/project-zones.md). Collection matching uses
`fullmatch` on normalized relative paths; group matching uses `re.search` on
IDs. Those are different contracts.
