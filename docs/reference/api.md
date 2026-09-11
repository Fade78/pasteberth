# HTTP API Reference

[Documentation map](../../GUIDE.md).

## HTTP API

**Unreleased:** the overview also reports the effective top-level
`max_archive_files` (an integer, or `null` for no configured count limit).
The Web UI checks this limit before submitting a ZIP selection; the server
remains authoritative. Archive capacity failures still return HTTP 503.

The browser API is same-origin and uses the session cookie. Non-browser clients
can use HTTP login and cookies, or an operator-issued bearer token with the
scope documented below. There is no CORS API. The supplied Web UI and bundled
HTTP client are implementation examples.

**Unreleased, after 2.1.21:** new clients should use the generic `items` routes
below, multipart `file`, and `schema=items` on aggregate routes. Content identity
and conditional downloads are also unreleased; they are not features of 2.1.21.
See [compatibility](#compatibility-unreleased) for the retained legacy contract
and [the external-consumer recipe](../recipes/external-consumer.md) for a runnable
authenticated download example.

### Routes

The paths below are shown for a root deployment. When `url_prefix = "/paste"`,
prepend `/paste` to every route, including `/login`, static assets, API paths,
and previews. The prefix is a configured public path, not part of the browser
`Origin` value.

| Method | Path | Authentication | Purpose |
|---|---|---|---|
| `GET` | `/api/health` | public | Liveness probe. |
| `GET` | `/api/zones?schema=items` | session or bearer `L` | Registry/group snapshot with separately read per-zone histories, counts, and busy status. |
| `GET` | `/api/groups` | session or bearer `L` | Group definitions and matching zone IDs. |
| `POST` | `/api/drop/resolve` | loopback or session | Resolve a target directory to a configured zone ID. |
| `GET` | `/api/zones/{id}/items` | session or bearer `L` | Complete zone history, newest first, from a published zone without discovery refresh. |
| `POST` | `/api/zones/{id}/items` | session or bearer `W` | Upload multipart content. |
| `POST` | `/api/zones/{id}/items/regularize` | loopback or session | Regularize one CLI direct-drop staging file. |
| `PATCH` | `/api/zones/{id}/items/{filename}/comment` | session or bearer `W` | Replace the item's short Unicode comment. |
| `DELETE` | `/api/zones/{id}/items/{filename}` | session or bearer `W` | Delete one managed item. |
| `POST` | `/api/zones/{id}/items/batch-delete` | session or bearer `W` | Delete several managed items. |
| `POST` | `/api/zones/{id}/items/archive` | session or bearer `R` | Stream selected managed items as a ZIP. |
| `POST` | `/api/transfers?schema=items` | session or scoped bearer | Copy or move managed items between two configured zones. |
| `GET` | `/api/zones/{id}/items/{filename}/content` | session or bearer `R` | Preview or download managed payload bytes. |
| `HEAD` | `/api/zones/{id}/items/{filename}/content` | session or bearer `R` | Same acquisition, conditional checks, and representation headers as GET, without a body. |
| `GET` | `/login` | public | Login page when authentication is enabled. |
| `POST` | `/login` | public | Create a session from a password form, JSON body, or multipart form. |
| `POST` | `/logout` | session | Revoke the current session. |
| `GET` | `/api/zones/{id}/access` | session or bearer | Report access to one known zone without exposing a global listing. |
| `GET` | `/api/tokens` | session admin | List token metadata, grant resolution, suspensions, and scope catalog. |
| `POST` | `/api/tokens` | session admin | Create a token; the plaintext secret is returned once. |
| `POST` | `/api/tokens/{id}/extend` | session admin | Replace the expiration while keeping the secret. |
| `POST` | `/api/tokens/{id}/rotate` | session admin | Replace the secret and expiration; the new secret is returned once. |
| `DELETE` | `/api/tokens/{id}` | session admin | Revoke one token. |
| `GET` | `/api/token-suspensions` | session admin | List global, zone, and group suspensions. |
| `POST` | `/api/token-suspensions` | session admin | Set or clear one token suspension. |

The table describes an authentication-enabled deployment. If authentication
is explicitly disabled, session checks accept requests without a cookie.
"Public" does not bypass Host checks. Health returns `{"ok":true}` and is a
liveness check, not proof that every zone is writable or recovery has succeeded.

### Bearer tokens

Tokens are created by an authenticated administrator in the Web UI or with the
admin endpoints. Send the returned credential only in an HTTP header:

```sh
export PASTEBERTH_TOKEN='pb_<selector>.<secret>'
curl --fail-with-body --silent --show-error \
  --header "Authorization: Bearer $PASTEBERTH_TOKEN" \
  https://pasteberth.example.internal/paste/api/zones/default/items
unset PASTEBERTH_TOKEN
```

The registry stores only a hash of the secret. The secret is returned once on
creation or rotation; it is not recoverable. A bearer request must not also
send the `pb_session` cookie. A token cannot access token administration or
the direct-drop `resolve`/`regularize` routes.

Each token has one or more grants. A grant targets a zone ID, current group
name, normalized absolute `PATH`, or `global`, and carries any combination of
`L`, `R`, and `W`. `L` lists metadata, `R` reads bytes and archives, and `W`
uploads, explicitly replaces named content, deletes, and edits comments.
Copy requires `R` on the source and `W` on the target; move requires `R+W` on
the source and `W` on the target. A grant with no rights can use
`GET /api/zones/{id}/access` to test whether its target exists, but cannot list
or read it.

Scopes are resolved against the current published zone registry. Missing zone
IDs, renamed groups, missing or ambiguous paths, expired/revoked tokens, and
suspended scopes fail closed. An out-of-scope zone is intentionally returned
as `404 unknown_zone`, not distinguished from an unknown zone. A token that
does cover a zone but lacks the requested permission receives `403 forbidden`.

Create a token with exactly this JSON shape; `duration_seconds: null` is
permanent and an explicit duration is measured from creation time:

```json
{
  "label": "CI upload",
  "duration_seconds": 2592000,
  "grants": [
    {
      "scope_type": "group",
      "scope_value": "builds",
      "permissions": ["L", "R", "W"],
      "allow_replace": false
    }
  ]
}
```

`allow_replace` is policy, not a replacement request. A client must still send
both `preserve_name=1` and `replace=1` for a named replacement. The admin
listing reports `active`, `expired`, `revoked`, `suspended`, or `missing` grant
states and the resolved zone IDs. The registry is external mutable state and
must be included in protected backups.

`/api/zones/{id}/items` returns `{"zone":"id","items":[...]}` and covers images,
UTF-8 text, and opaque binary content such as PDFs. Each overview zone reports
`busy`, copied-list formatting settings, whether ZIP download is enabled, its
sidecar `retain` setting, and an
effective `upload_limit_bytes` value. That value is the smaller of the hard
upload limit and the bytes that can be accepted without crossing the configured
free-space safeguards; raw disk capacity is not returned.

The `POST /api/drop/resolve` endpoint accepts a target directory
and returns its configured zone ID after static/collection and canonical-path
resolution. It is used by the filesystem client when the client has no local
configuration; it never authorizes an arbitrary target directory.
Its JSON body must contain exactly `{"directory":"/absolute/path/to/zone"}`;
success returns `{"zone":"configured-zone-id"}`. Like `regularize`, it accepts
an authenticated session or an unauthenticated loopback peer.

The `/api/zones` response takes one registry/group snapshot, then reads each
zone's history separately. For an available zone, `count` is computed from its
returned `items` array with `schema=items`, or `images` in the default legacy
schema. Only one history collection is returned, not duplicate arrays. This is
not an atomic content snapshot across zones; another operation can change
storage between the individual history reads.

A busy or temporarily unavailable dynamic zone can return `busy: true`,
`count: null`, and `items: []` (`images: []` in the legacy schema). That array
means history was unavailable, not that its files were deleted or the zone is
empty. Preserve the previous display
or show an unavailable state, then refresh. Generic per-zone listing uses
`blocking=False` and the published registry: it neither triggers nor joins a
global discovery scan and returns `423 zone_busy` on lock contention. It still
reads the selected zone's full history; histories are not cached, and filesystem
I/O can block. Legacy `/images` listing retains synchronous discovery refresh
or join and blocking history acquisition.

The server account owns files it creates; configure `file_group` and matching
directory group permissions when other system users must read them.
Item payloads include `changed_at`, but the current implementation never
populates it: it is always `null`. It is not a usable filesystem modification
time or change-detection signal. `created_at` is the stored creation timestamp,
not a substitute for filesystem modification time.

The `regularize` route is used by a local `drop` when it has staged a
`.pbdrop-<24 lowercase hex digits>.tmp` file in the configured zone. It accepts
a JSON object containing `stage`, `filename`, `mime`, and `replace`; it is not a
general filesystem-registration endpoint. Unauthenticated access is limited to a
loopback peer, while an authenticated session may use the route remotely.

The loopback exception uses the immediate TCP peer, not `X-Forwarded-For`.
A loopback reverse proxy therefore qualifies even when its original client is
remote. Do not describe these endpoints as remote-session-only behind such a
proxy. Block them at a public proxy unless deliberately needed; ordinary
uploads and remote `drop --zone ID` do not need them. `regularize` still requires
the correctly named staging file in an active zone. Resolution alone does not
require a staging file. See [proxy deployment](../deployment.md#foreground-operation).
Block both `/items/regularize` and `/images/regularize`; blocking only the legacy
path leaves the same loopback exception reachable through the new alias.

### Compatibility (Unreleased)

Legacy routes delegate to shared handlers, without redirects or a forked storage
implementation. They remain fully supported throughout 2.x. Removal will be no
earlier than 3.0 and will be announced in advance; no removal date is set.

| Legacy surface | Canonical surface |
|---|---|
| `GET`, `POST /api/zones/{id}/images` | Same methods on `/api/zones/{id}/items` |
| `PATCH .../images/{filename}/comment` | `PATCH .../items/{filename}/comment` |
| `DELETE .../images/{filename}` | `DELETE .../items/{filename}` |
| `POST .../images/batch-delete`, `.../images/archive`, `.../images/regularize` | Same children below `/items` |
| `GET`, `HEAD /previews/{id}/{filename}` | Same methods on `/api/zones/{id}/items/{filename}/content` |
| Multipart `image` | Multipart `file`; either alias is accepted on both upload routes |
| `unknown_image` for a missing managed file | `unknown_item` in generic responses, including batch/transfer failures |

`GET /api/zones` defaults to the legacy `images` schema; `?schema=images`
selects it explicitly. `?schema=items` returns `items` instead. Likewise,
`POST /api/transfers` defaults to legacy item payloads and error codes, preserving
`preview_url` and `unknown_image`; `?schema=items` selects generic payloads and
errors for the bundled UI. Transfers already call their result collection
`items` in either schema. Each aggregate route accepts exactly one `schema`
value, either `images` or `items`; blank, repeated, or unknown values return
`400 invalid_request`.

Generic item responses expose `content_url`, not `preview_url`. Legacy item
responses keep `preview_url` and add `sha256`, `etag`, and `content_url`.
Methods, authentication, mounted paths, and legacy status/error behavior are
preserved, with the explicit read-mode differences described above and the
new conditional-read behavior below. Content classification is unchanged:
`kind: image`, dimensions, PNG/JPEG/WebP validation, image limits, and genuine
image errors such as `invalid_image` remain image-specific. Neither persisted
zones nor sidecars or journals need migration for this vocabulary change.

### Upload

The canonical multipart field is `file`. Both upload routes accept either
`file` or the legacy `image`, but require exactly one payload: supplying both,
repeating a field, or adding another payload returns `400 invalid_request`.
The legacy route also preserves its sole-unknown-field fallback, only when that
is the entire form; new clients must use a named alias. Control fields must not
be file parts. `preserve_name=1` retains a valid dropped filename; `replace=1`
together with `preserve_name=1` explicitly authorizes
replacing a coherent managed pair. For bearer uploads, the token grant must
also have `allow_replace = true`. Clients may send `creation_method` with one
of `web_mouse_drop`, `web_paste`, `filesystem_drop`, or
`filesystem_register`; browser requests default to `web_paste`, and filesystem
clients should send `filesystem_drop`. This field records the client's declared
path through the upload pipeline; it does not authenticate the source and must
not be treated as a security boundary. A bearer token with `W` but without `R`
receives `{"accepted":true}` instead of a reference or duplicate/replacement
metadata; clients must not infer the stored filename from that response.

#### Login and write example

Run this in Bash with a real configured `default` zone and a local
`capture.png`. It uses a private temporary cookie jar and reads the password
without displaying it or placing it in curl's command-line arguments. For a
root deployment use `base="$origin"`; for the configured `/paste` mount use
the value shown. `Origin` never includes the mount path or a trailing slash.

```bash
origin='https://pasteberth.example.internal'
base="$origin/paste"
umask 077
mkdir -p work/tmp
cookies=$(mktemp "$PWD/work/tmp/pasteberth-cookies.XXXXXX")
trap 'rm -f "$cookies"' EXIT
read -r -s -p 'Pasteberth password: ' password
printf '\n' >&2
printf '%s' "$password" | curl --fail-with-body --silent --show-error \
  --cookie-jar "$cookies" --header "Origin: $origin" \
  --data-urlencode 'password@-' "$base/login"
unset password

curl --fail-with-body --silent --show-error \
  --cookie "$cookies" --header "Origin: $origin" \
  --form 'file=@capture.png' --form 'preserve_name=1' \
  --form 'creation_method=filesystem_drop' \
  "$base/api/zones/default/items"
```

Login succeeds with `303` and `Set-Cookie`; following the redirect is not
required. A new upload returns `201`. Check curl's exit status after each call;
do not continue a script after a failed login. Use a trusted TLS certificate or
an explicit CA trust configuration, not an unconditional `--insecure`.

All request bodies need valid `Content-Length` framing. Transfer-encoded or
ambiguous requests are rejected; chunked uploads are not supported.

Without `preserve_name=1`, the server generates the filename. A managed name
collision without `replace=1` returns `428 replacement_required`. A foreign
file collision returns `409 storage_conflict`. Low free space returns
`507 storage_low`.

Unnamed uploads are deduplicated by SHA-256 within a zone. Identical managed
content returns `200` with `duplicate: true` and the existing reference, without
retention. Named uploads can contain identical bytes as distinct items. A
successful upload can include `retention_deleted`; inspect it rather than
assuming that an upload only adds a file.

API errors use a JSON object with this shape:

```json
{
  "error": {
    "code": "replacement_required",
    "message": "..."
  }
}
```

The main application error codes are:

| Status | Codes | Meaning |
|---:|---|---|
| `400` | `invalid_request`, `empty_upload`, `invalid_filename`, `invalid_image`, `invalid_comment` | The request or content is invalid. |
| `401` | `unauthorized` | A protected route has no valid session or bearer credential; the response includes `WWW-Authenticate`. |
| `403` | `forbidden_host`, `forbidden_origin`, `forbidden`, `admin_required`, `zip_disabled` | The host/origin is not allowed, the token lacks a required permission, or ZIP is disabled for the zone. |
| `404` | `unknown_zone`, `unknown_item`, `unknown_image` (legacy), `not_found` | The requested resource does not exist. |
| `405` | `method_not_allowed` | The HTTP method is not supported for the requested resource. |
| `409` | `storage_conflict` | A foreign file or another storage conflict prevents the operation. |
| `412` | `precondition_failed` | The acquired content does not match `If-Match`; no file bytes are sent. |
| `413` | `too_large` | The request or content exceeds a configured limit. |
| `415` | `unsupported_media_type`, `unsupported_format` | The declared or detected content type is not supported. |
| `423` | `zone_busy` | Another operation holds the zone lock; inspect `Retry-After`. |
| `428` | `replacement_required` | Explicit replacement was required but not requested. |
| `429` | `rate_limited` | Login attempts are temporarily throttled. |
| `500` | `destination_error`, `internal` | The server could not complete a storage or internal operation. |
| `503` | `retention_error`, `server_busy`, `token_store_error` | Retention failed after publication, request admission/archive capacity is exhausted, or the token registry is unavailable. |
| `507` | `storage_low` | The configured free-space reserve would be exceeded. |

This is the application error shape, not a guarantee for proxy failures,
malformed HTTP, or login pages. Header-budget failures can return `431`.
An HTTP failure does not always mean no mutation: inspect history after
`retention_error`, and inspect per-file transfer/batch results before retrying.

### Item Metadata (Unreleased)

A generic item response includes fields such as the following. The identity
values are illustrative; real clients must use the values returned by the server.

```json
{
  "id": "2026-08-25_01-22-31_a81c42.png",
  "filename": "2026-08-25_01-22-31_a81c42.png",
  "created_at": "2026-08-24T23:22:31.412000+00:00",
  "width": 1920,
  "height": 1080,
  "size": 9283,
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "etag": "\"sha256-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\"",
  "format": "png",
  "kind": "image",
  "mime": "image/png",
  "creation_method": "web_paste",
  "replaced": false,
  "comment": "Reference capture",
  "changed_at": null,
  "content_url": "/api/zones/default/items/2026-08-25_01-22-31_a81c42.png/content",
  "reference": "@/home/user/.local/share/pasteberth/storage/default/2026-08-25_01-22-31_a81c42.png"
}
```

`sha256` is the stored content digest: exactly 64 lowercase hexadecimal digits,
or JSON `null` for valid legacy metadata without a digest. `etag` is exactly
`"sha256-HEX"`, including the surrounding double quotes, where `HEX` is that
same lowercase digest; it is `null` when the digest is unknown. GET and HEAD
send that value as `ETag` when known and omit the header otherwise.

This is payload identity for bytes maintained by cooperating managed writers,
not a metadata revision or proof of read-time integrity. A comment-only change
does not change the ETag. Different bytes, even at the same size and filename,
have a different digest; replacing A with B and then the original A returns A's
ETag, not a new generation number. `changed_at` remains `null`.

Listings and downloads do not hash payloads on read or hash all legacy files in
a zone to fill missing identities. Stored digests are not signatures and do
not protect against arbitrary external in-place mutation. A consumer should
verify downloaded length and SHA-256 locally before publishing. With legacy
`sha256: null` and `etag: null`, it can calculate a local digest but cannot pin
the download to the version in the listing. Use `content_url`, including its
mount prefix and percent-encoded filename, not the filesystem `reference`.

### Downloads (Unreleased)

These changes are after `2.1.21`. Preview/download `GET` and `HEAD`, and ZIP
`POST`, look up the zone in the last published registry. They neither start
discovery nor wait for an in-flight scan, even for an unknown ID. A new zone
returns `404 unknown_zone` until a refresh publishes it; an ineligible zone is
removed when a later registry publishes that change, not necessarily on the
next download request. The selected destination's directory identity and
managed files are still checked. Generic `/items` listing also uses the
published registry without discovery refresh or join. Mutations, directory
resolution, and legacy `/images` history retain synchronous global refresh or
join behavior.

The service captures selected metadata and open payload handles under shared
filesystem operation locks, then releases those locks before sending headers
or content. It bypasses the per-zone Python `RLock`, so acquisition can coexist
with shared history reads. Generic content GET/HEAD and HTTP ZIP use
`blocking=False` and return `423 zone_busy` with `Retry-After: 1` if a writer
holds the lock. Legacy `/previews` acquisition preserves `blocking=True` and
can wait for that writer. Neither mode bounds filesystem I/O latency.
Content downloads on either route remain subject to `max_upload_size`; a larger
item returns `413 too_large`.

Both content routes evaluate `If-Match` against the same acquired item whose
metadata supplies the response headers and whose retained handle supplies the
bytes. They do not test a path and then reopen it. On an acquired item:

- No `If-Match` means an unconditional read of the acquired version.
- A quoted entity-tag or comma-separated list uses strong, exact comparison;
  any matching strong tag satisfies the condition. Repeated header lines are
  combined into one list.
- Weak tags such as `W/"sha256-HEX"` are valid syntax but never match a strong
  tag. An unknown digest cannot match a concrete tag.
- `If-Match: *` tests existence and succeeds even for legacy content without a
  digest. It must stand alone, not appear in a list.
- Malformed grammar, including bare tags or empty list members, returns
  `400 invalid_request`. A valid but unsatisfied condition returns
  `412 precondition_failed` before file-body streaming. GET can return a JSON
  error body; HEAD has no body. The acquired handle is closed on either failure.

Acquisition precedes conditional evaluation, so a missing/hidden item, busy
zone, or unavailable server can fail first. This is not an `If-Match` contract
for uploads, comments, deletion, or ZIP archives. For example, list A, then
request its `content_url` with `If-Match` set to its exact `etag`: if managed B
has replaced it before acquisition, the response is 412, not B under A's tag.

GET streams at most 64 KiB per source read, up to the captured item size.
`Content-Type` and `Content-Length` come from that captured metadata, with
`Cache-Control: no-store` and the normal security headers. Non-PNG/JPEG/WebP
content uses attachment disposition, including UTF-8 filename encoding; stored
HTML is not rendered on the application's origin. HEAD performs the same
acquisition and validation but sends no body, including on errors. HEAD is
routed explicitly for both content paths; it is not enabled for every GET route.

Once acquisition succeeds, cooperating managed operations can replace or delete
even the selected files while their open versions continue to stream. This is
not a snapshot against arbitrary external in-place writes. Missing, invalid,
or transaction-hidden items return `404 unknown_item` (`unknown_image` on the
legacy route); filesystem failures return `500 destination_error` before headers.
Failures after streaming starts
close the response rather than append a second JSON response. A source ending
before its captured length aborts the transfer; HTTP 200 alone does not prove
completion. The server does not detect a same-length external mutation by
hashing during streaming; local digest verification must reject those bytes.
There is no atomic listing/download pair, whole-zone snapshot, or build-generation
contract. See [managed reads](storage.md#managed-reads-unreleased) for visibility
and lock scope.

The `http_request_timeout_seconds` deadline still covers the initial request
and acquisition phase. During preview or ZIP emission it is an inactivity timeout, not a total
transfer deadline. ZIP also has its separate total streaming-duration budget.
Timeout or disconnect closes the response and releases retained handles as the
handler unwinds; a timer cannot interrupt a blocked filesystem call.

### Multiple operations

Batch deletion accepts repeated `filename` form fields or a JSON `filenames`
array. Archive accepts the same selection as a repeated form field or JSON
array and streams the ZIP without a temporary server archive.

**Unreleased:** ZIP retains all selected handles for the transfer, but holds no
zone locks while compressing or sending. `[limits].max_archive_files` defaults
to `64`; a larger selection returns `413 too_large` before opening sources.
`[limits].max_active_archives` defaults to `4` concurrent acquisitions/transfers
across all zones in one server process. A nonblocking semaphore reserves each
slot; exhaustion returns `503 server_busy` with `Retry-After: 1`, distinct from
the writer-lock `423`. These two settings accept positive integers or
`"unlimited"`; batch name/body limits also apply.

The existing defaults remain `max_archive_size = "256MiB"` of selected
uncompressed source bytes and `max_archive_duration_seconds = 300` for the
streaming phase, not acquisition plus streaming. ZIP source reads are at most
64 KiB and stop at each captured size. Every retained handle and the archive
slot are released on completion, timeout, disconnect, or failure. A truncated
transfer is not a completed ZIP; retry only after checking the failure and
selection. See [resource budgets](configuration.md#operational-budget-defaults).

Internal transfers accept exactly this JSON object. New clients post to
`/api/transfers?schema=items`; omitting the selector retains the legacy response:

```json
{
  "mode": "copy",
  "source_zone": "default",
  "target_zone": "secondary",
  "filenames": ["report.txt", "capture.png"]
}
```

`mode` is `copy` or `move`. Only coherent managed data/sidecar pairs are
eligible. Filenames are preserved; an existing data file, sidecar, foreign file,
or active transaction at the target returns `409 storage_conflict` before any
item is copied. Copy leaves the source unchanged. Move publishes each target
pair before deleting its source. The response contains `transferred`, `items`,
`retention_deleted`, and per-file `failed` entries; a failed entry may include
`target_published: true` when the target was durable before a later step failed.
The target zone's free-space reserve, retention, group, and permission rules
apply. The two zone locks are acquired in a stable order, and a busy zone
returns `423 zone_busy` with `Retry-After: 1`.

Stored dates are preserved, but each publication protects its own filename
during retention. With `retain = 1`, an older incoming item therefore survives
its own retention pass and can evict a newer resident item. A later publication
in the same batch can evict an earlier incoming item. Such an eviction is
reported in `failed` with `code: "retention_error"` and
`target_published: true`; that item is not reported as successfully transferred,
and a move keeps its source. Inspect the final results and `retention_deleted`.

Transfers are not an atomic batch or an atomic move between two filesystems;
the locks coordinate Pasteberth operations, not external writers. See
[transaction scope](storage.md#transaction-scope) and [retention](storage.md#retention).

Long-running batch deletion holds an exclusive zone lock. In `2.1.21`, archives
also hold that lock; **Unreleased** ZIP transfers instead use the short shared
acquisition described above. Conflicting nonblocking lock requests return:

```text
423 zone_busy
Retry-After: 1
```

Clients should refresh the zone and retry after the indicated delay.

### Comments

Comments are stored in the item's JSON sidecar and accept valid UTF-8 Unicode,
including emoji and line breaks. They are limited to 280 Unicode characters and
1 KiB of UTF-8 data; control, private-use, noncharacter, and invisible formatting
code points are rejected. In the Web UI, `Enter` inserts a line break and
`Ctrl`+`Enter` (or `Command`+`Enter`) saves the comment. Send a JSON object
containing only `comment`:

Using the cookie jar and URL variables from the upload example:

```sh
curl --fail-with-body --silent --show-error --cookie "$cookies" -X PATCH \
  --header "Origin: $origin" --header 'Content-Type: application/json' \
  --data '{"comment":"Reference capture"}' \
  "$base/api/zones/default/items/capture.png/comment"
```

An empty string clears the comment. Existing sidecars without a `comment` field
remain valid and are read as an empty comment. These are the default character
and byte budgets; `[limits]` can change them. URL-encode filenames when placing
them in route segments.

### Login clients

The login endpoint accepts a `password` field in a normal URL-encoded form,
multipart form, or JSON object. A successful login returns a session cookie and
redirects to the public root (`/` or the configured `url_prefix`). Failed
attempts are delayed and rate limited. A client should
preserve and resend the cookie for protected API calls, and should send the
same-origin `Origin` or `Referer` on unsafe requests.

The implementation checks a supplied Origin or Referer against the effective
scheme and Host. Non-browser requests with neither header are accepted; this
is not a mandatory-header API contract. An opaque `Origin: null` has separate
Referer/`Sec-Fetch-Site` handling and must not be used as a substitute for the
real origin. Examples send an explicit valid Origin so they also exercise the
same-origin boundary.

The cookie is `HttpOnly`, `SameSite=Lax`, scoped to the configured public path,
and `Secure` for effective HTTPS. Sessions are held in memory, expire after
`session_ttl_hours`, and disappear on server restart. Password rotation
invalidates existing sessions: validation rejects tokens whose recorded
password-file version no longer matches. No restart is required. The shared
password provides no per-zone user authorization; groups only control
presentation.

To revoke the example session and remove its cookie jar:

```sh
curl --fail-with-body --silent --show-error --cookie "$cookies" \
  --header "Origin: $origin" --request POST "$base/logout"
rm -f "$cookies"
```
