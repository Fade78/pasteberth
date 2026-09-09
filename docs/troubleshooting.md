# Troubleshooting

[Documentation map](../GUIDE.md).

Use the same configuration and account as the failing process. Start with
`pasteberth --version`, the service command and logs, and
`pasteberth audit --config /absolute/path/config.toml` while the service is
stopped if you need to validate port binding. An audit does not repair files.

### `ModuleNotFoundError: No module named 'PasteBerth'`

This means an old or manually configured launcher is invoking the private
module with `python3 -m PasteBerth.runtime`. The v2 deployment must be started
through the executable inside the deployment directory, which loads its
runtime directly from that directory:

```sh
/home/atelier/PasteBerth/pasteberth --help
```

If `pasteberth` resolves to `~/.local/bin/pasteberth`, replace that symlink or
copy with the current `PasteBerth/pasteberth` executable. Do not run the module
directly from inside the `PasteBerth/` directory; `PYTHONPATH` would then need
to point to its parent. For systemd, `ExecStart` must end in
`/PasteBerth/pasteberth`, not `/usr/bin/python3 -m PasteBerth.runtime`.

### `pasteberth` starts without authentication

Check which configuration was discovered. No configuration means minimal
loopback mode. Use an explicit path consistently:

```sh
pasteberth audit --config /absolute/path/config.toml
pasteberth passwd --config /absolute/path/config.toml
pasteberth serve --config /absolute/path/config.toml
```

### The server refuses to start with authentication enabled

The selected `[auth]` configuration requires a readable valid scrypt hash.
Run `pasteberth passwd` against the same config and then `pasteberth audit`.

### `audit` returns status 1

The deployment is usable but has warnings. Review broad directory permissions,
wildcard host checks, proxy trust, listener policy, and TLS settings before
exposing it.

### A file is not visible in the history

Pasteberth only lists coherent data/sidecar pairs owned by Pasteberth. Check
that the file is regular, its sidecar is valid and readable, and no transaction
marker is active. Foreign files and malformed sidecars are deliberately left
alone.

For a file created by `register` in a shared POSIX zone, check both the writer
and the daemon process. A successful `register` only proves that the registering
account could read the data and write the sidecar; it does not prove that the daemon can
read them. Check the daemon's group list and its log:

```sh
systemctl --user show pasteberth --property=MainPID --value
grep '^Groups:' /proc/$(systemctl --user show pasteberth --property=MainPID --value)/status
journalctl --user -u pasteberth -n 50 --no-pager
```

The daemon must have the group that owns the shared files. After changing group
membership, restart the user session or user manager before restarting
Pasteberth. `sidecar unreadable` indicates an open or permission failure; a
valid `sha256` field is accepted, and older sidecars without that field remain
valid.

### A request returns `423 zone_busy`

Another process is holding the zone's exclusive operation lock. Wait for the
`Retry-After` delay, refresh the history, and retry. Do not remove the lock
file manually.

The overview can still return `200` with `busy: true`, `count: null`, and
`images: []` for that zone. This means its history is unavailable, not that
the files were deleted. The per-zone history request returns `423` while
the zone remains locked.

### A filename replacement is refused

The name may be occupied by a foreign file, or the managed pair may require an
explicit replacement flag. Browser replacement requires confirmation;
filesystem and API operations require `--replace` or `replace=1`. Foreign
files remain protected even with that flag.

### systemd cannot see a zone or temporary source

Check `WorkingDirectory`, `ExecStart`, and every configured absolute path. With
`PrivateTmp=true`, a service-private `/tmp` is different from the host's `/tmp`.
Check the unit's `ReadWritePaths` if hardening is enabled.

### The browser cannot use a returned path

That is expected. The reference is for the harness on the Pasteberth server
machine. Use the browser's download action when a workstation needs a copy.

### `drop FILE` is rejected or the daemon is unreachable

`drop` always needs the daemon. Use `drop ZONE_DIRECTORY FILE...` on a shared
filesystem or `drop --zone ID FILE...` when addressing a zone by ID. A lone
`drop FILE` does not mean registration; use `register FILE` only when you want
to create or refresh a local sidecar without changing the data bytes.

Check the actual `--server` URL, including any `/paste` mount prefix. Without
a discovered config, `drop` tries `http://127.0.0.1:8765`, not a remote service.
Staging locally does not remove the need to contact the daemon. A remote client
should normally use `--zone ID` rather than its workstation's directory path.

### TLS certificate verification fails

Check the certificate chain, expiration, and SAN against the exact hostname or
IP in the URL. For direct loopback HTTPS, a certificate for a public DNS name
does not automatically validate `127.0.0.1`. Prefer a trusted matching
certificate. `--insecure` is an explicit exception for a separately trusted
self-signed endpoint; it disables verification, not authentication, and should
not become an unconditional flag in scripts or MCP launchers.

### A proxy deployment returns `403` or the UI cannot load

`forbidden_host` means the request Host is invalid or not on an explicit
allowlist. `forbidden_origin` means the supplied browser origin does not match
the effective scheme and Host. Preserve `Host[:port]`, overwrite forwarded
headers, and trust only the actual proxy peer. Set `url_prefix` explicitly and
forward it unchanged; `/paste` belongs in routes, not in the Origin header.

Check the browser's failing URL: login, assets, API, and previews all need the
same prefix. Follow the [Caddy/nginx examples](deployment.md#caddy). Those
examples deliberately block direct-drop resolve/regularize at the public
proxy; use ordinary uploads or remote `drop --zone ID` instead.

### A collection zone does not appear or disappears

Run `audit` using the daemon's configuration and inspect discovery diagnostics.
Verify the base directory exists, the resolved relative path full-matches the
rule, its depth is within `max_depth`, its ID is valid and unique, and the
candidate has no subdirectories. Check daemon read/traversal permission and
write permission for actual operations. Overlapping collection settings must
agree. A group pattern selects IDs, not labels, and is not a discovery rule.

New candidates appear after a background scan observes them and a subsequent
visible-browser poll reads the completed registry; a hidden tab refreshes on
becoming visible. **Unreleased (runtime version still `2.1.21`):** not every
overview starts a scan. Zone and group overviews share a cooldown of
`max(10 seconds, last full refresh duration)` from completion, including
startup, foreground, and failed refresh attempts. The duration includes
registry installation. The next eligible poll can start one background job;
polls during a refresh do not start another. A removed, inaccessible, or newly
nonmatching directory can leave the active registry
without deleting its contents. See the [collection contract](zone-collection-contract.md).

### Discovery or overview is slow

Measure before changing collection rules. `pasteberth audit` reports discovery
duration, but not the service's registry-installation or overview storage cost.
**Unreleased:** debug logs separate scan and installation timings and include
per-rule timing, matches, and newly cached paths. Filesystem observations are
shared across rules only within that pass; later scans read the filesystem
again. Narrow bases and appropriate `max_depth` values can reduce traversal;
the optimization does not change regex semantics.

**Unreleased:** explicit service actions bypass the background cooldown or
wait for the in-flight refresh, with one refresh or join per service action.
This avoids duplicate scans within an action, not waits on slow filesystem
calls. Zone-overview history and free-space checks also remain synchronous and
can block independently of discovery. Neither the 10-second browser poll nor
the cooldown is a response deadline. A hard 100 ms scan timeout cannot be
enforced by checking elapsed time around blocking filesystem calls. A bounded
wait would require separate worker scheduling and a policy for incomplete
scans, not just a timer in the traversal loop. See
[discovery performance](discovery-performance.md) for the strategy comparison
and limitations.

### Registration succeeds but retention or server policy was not applied

This is expected. `register` checks its CLI-selected configuration, updates
only the sidecar, and never contacts the daemon or applies its per-zone
retention/free-space policy. It can register a file outside any configured
zone. Use [server-backed drop](reference/cli.md#filesystem-drop) when the
daemon must validate and publish the deposit.

### An upload returns `413`, `507`, or `503`

`413 too_large` can come from content, multipart framing, other request budgets,
or the proxy. Raising `max_upload_size` alone may leave the independent
`max_multipart_body_size` or proxy limit unchanged. `507 storage_low` indicates
the zone filesystem's free-space reserve would be crossed; retention is not a
promise to make space before accepting any upload.

`503 retention_error` can occur after the item was published. Refresh history
and inspect logs before retrying or replacing it. `503 server_busy` means
request admission is full; honor `Retry-After`. For transfers or batch
operations, inspect per-file results: the batch is not all-or-nothing.

### Clipboard copy fails or HTML looks different

Browser clipboard APIs depend on permissions and secure-context support.
A successful deposit does not prove that automatic clipboard copying
succeeded; use the returned reference or visible copy action and inspect the
browser's feedback. Multi-format clipboard behavior can differ by receiving
application.

HTML preview is sanitized text, not rendered stored HTML. `Copy Text` sanitizes
the HTML flavor, but storage and downloads retain original bytes. The explicit
red `Copy raw HTML` button bypasses that sanitized-copy behavior; only use it
for trusted content and a trusted destination. A broken image preview can also
mean that a structurally valid image is not fully decodable by the browser.
