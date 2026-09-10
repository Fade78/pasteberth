# Fetch a Version-Aware Item

**Need:** Retrieve a managed PDF, text file, image, or other file for a local
consumer such as Siftport, without mounting the server's zone or reading its
sidecars and lock files.

**Unreleased, after 2.1.21:** this recipe needs the generic item API and
conditional downloads in the current working tree. A 2.1.21 server does not
provide this contract. It uses the existing shared-password login and cookie
session, not a new token service.

## Run the Example

Use the standalone, standard-library-only
[`contrib/fetch_pasteberth_item.py`](../../contrib/fetch_pasteberth_item.py)
from a repository checkout with Python 3.11+. It is not part of the code-only
deployment bundle. You need a running authenticated service, a known published
zone ID, an exact managed filename, and a trusted, existing output directory.

From the checkout root, with `PASTEBERTH_PASSWORD` already injected into the
environment by your trusted launcher or secret store:

```sh
python3 contrib/fetch_pasteberth_item.py \
  --server https://pasteberth.example.internal/paste \
  --zone default --filename 'report.pdf' ./report.pdf
```

The final positional argument is the local output file, not a directory.
Adapt the URL, zone, filename, and output. Include the configured mount prefix
in `--server`; omit `/paste` for a root deployment. Filenames are exact Unicode
names, not pre-encoded URL segments; the example percent-encodes them, including
literal percent signs. The server URL itself must be ASCII, with an encoded
path if needed, and contain no credentials, query, or fragment.

Use HTTPS outside a trusted local/loopback connection. TLS certificate checking
is enabled and the example has no insecure switch. For a local test endpoint,
`--server http://127.0.0.1:8765/paste` is accepted, but a Secure session cookie
requires HTTPS. Host policy still applies. A reverse proxy must preserve the
public Host and mount and supply trusted HTTPS forwarding information; do not
weaken [deployment checks](../deployment.md#foreground-operation) for a script.

## What It Verifies

1. POST the password to `/login` with the URL's origin, expect `303` and a
   nonempty `pb_session` cookie, and keep that cookie in memory. No response
   redirect is followed, including the successful login redirect.
2. GET `/api/zones/{id}/items`, validate its shape, and select exactly one item
   with the requested filename. Validate size and identity metadata, and require
   `content_url` to equal that item's canonical server-relative content route
   inside the configured mount. Credentials never follow a server-provided URL
   to another host or mount.
3. GET `content_url`, sending the exact listing `etag` as `If-Match` when known.
   Require HTTP 200, an exactly matching response ETag when requested, and a
   `Content-Length` equal to the listed size. Reject transfer encoding and
   content encoding other than absent or `identity`.
4. Stream into a private staging file in the output's parent, count bytes and
   hash them locally, and check the completed length and known listing SHA-256.
   Only after verification and closing the staging file, call `os.replace` to
   publish the output. A pre-publication failure preserves the previous output
   and attempts to remove staging; an existing output symlink is replaced, not
   followed.
5. Report JSON to stdout with the local `sha256`, `size`, and
   `listing_identity_verified`. A known digest produces `true`; legacy
   `sha256: null` produces a stderr warning and `false`. A locally calculated
   hash alone cannot establish the identity of the version in a legacy listing.

The server's ETag identifies managed payload bytes, not comment changes or an
item generation. A managed A-to-B replacement before acquisition causes 412
when A was requested; A-to-B-to-A yields A's original identity. The server does
not hash on read. Local verification therefore remains necessary to reject
truncation or bytes changed by an unsupported external in-place writer. See
[item metadata](../reference/api.md#item-metadata-unreleased) and
[conditional reads](../reference/api.md#downloads-unreleased).

## Outcomes and Limits

Publication and stdout reporting are separate operations, not an atomic pair.
There is no rollback after publication, and a nonzero exit does not always mean
the old output remains:

| Exit | Meaning |
|---:|---|
| `0` | Output published and JSON report flushed to stdout. |
| `1` | Pre-publication failure, including missing password, invalid metadata/URL, HTTP failure, TLS/network failure, or local I/O failure. |
| `2` | Invalid command-line arguments reported by the argument parser. |
| `3` | Output published, but writing or flushing the stdout report failed. The new output remains in place. |
| `130` | Interruption; it may occur before or after publication. Inspect the output before retrying. |

- The listing limit is **16 MiB**. Reads of payload bytes are **64 KiB** at a
  time; there is no separate example-level payload-size cap beyond the listed
  size and server policy.
- Each network operation has a **30-second timeout**, not a total transfer
  deadline. There are no retries, resumable downloads, or skip-if-unchanged
  cache. The example downloads each time it runs.
- HTTP `412`, `423`, `503`, redirects, missing items, and malformed responses
  are failures, not successful empty downloads. A larger integration should
  distinguish these outcomes, respect `Retry-After` where provided, and refresh
  metadata before deciding to retry. An overview's `busy: true`, `count: null`,
  `items: []` is unavailable history, never evidence of deletion.
- Output staging deliberately uses the output parent, not `TMPDIR`, so
  `os.replace` stays on the same filesystem. The example does not create that
  parent, change destination permissions with `chmod`, coordinate concurrent
  local writers, or promise crash durability. Keep the parent trusted.
- With unknown legacy identity, length is checked and bytes are hashed locally,
  but a same-size replacement between listing and download cannot be pinned.
  `changed_at` is always null; neither creation time nor filename supplies the
  missing identity.
- One file is published locally. No SFTP deployment, server-side build,
  whole-zone snapshot, cross-request atomicity, or downstream routing policy is
  implemented. Metadata and comments are not copied alongside the output. No
  Pasteberth lock is held while a downstream tool later uses the local file.

Return to [integrations](../integrations.md) or the
[documentation map](../../GUIDE.md).
