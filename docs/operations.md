# Operations

[Documentation map](../GUIDE.md).

Use [deployment](deployment.md) for installation and listener/proxy setup,
[configuration](reference/configuration.md) for settings, and
[troubleshooting](troubleshooting.md) for failure symptoms.

## Routine Checks

For a systemd user deployment:

```sh
systemctl --user status pasteberth.service
systemctl --user show pasteberth.service --property=MainPID --value
journalctl --user -u pasteberth -n 50 --no-pager
curl --fail --silent --show-error http://127.0.0.1:8765/api/health
```

Adjust the health URL for your host policy, TLS, and public prefix. With
`url_prefix = "/paste"`, the probe is `/paste/api/health`. Health returns
`{"ok":true}` without a session; it does not audit zone permissions, free space,
or every storage transaction. Check an authenticated overview and a controlled
deposit/download when validating the full handoff path. Do not use a production
zone for a write probe unless its retention effects are acceptable.

The overview reads zone histories separately, not as one atomic cross-zone
content snapshot. A zone with `busy: true`, `count: null`, and `images: []` has
unavailable history, not a confirmed empty directory. Refresh after the busy
operation finishes before treating missing items as deleted.

Background discovery does not make the entire overview asynchronous. History
and free-space checks still run synchronously and can block on filesystem I/O.

Monitor filesystem free space, service errors, unreadable sidecars, collection
diagnostics, repeated login throttling, and `zone_busy` responses. Retention
counts managed items, not bytes; foreign files still consume disk space.
`max_upload_size` and multipart/request budgets limit requests, not total
storage. See [retention](reference/storage.md#retention).

**Unreleased:** ZIP transfers retain source handles without zone locks, so even
selected files can be replaced or deleted by managed operations during output.
Plan for up to 64 source files per archive and four concurrent archives per
process by default, bounded separately from request admission. Retained open
versions can defer reclamation of deleted data until handles close. Monitor
`503 server_busy` archive-slot exhaustion separately from writer-lock `423`;
both return `Retry-After: 1`. The 256 MiB source-byte and 300-second streaming
limits remain. See [resource budgets](reference/configuration.md#operational-budget-defaults).

`pasteberth audit --config /absolute/path/config.toml` is a read-only deployment
check. It includes listener binding and TLS checks, so run it before startup or
with the service stopped when checking whether its configured port can bind.
Exit `1` means warnings to review; exit `2` means errors. It also reports
collection scan timing, rules, and candidate counts.

## Configuration Changes

Restart the daemon after changing listener, proxy, host, TLS, upload, zone,
collection, group, or authentication settings. Existing collection rules can
discover new matching directories without restart or config edits. A visible
browser polls every 10 seconds, with new candidates appearing after a scan
completes; this is not a filesystem watcher or a fixed discovery deadline.

**Unreleased (runtime version still `2.1.21`):** zone and group overviews share
a background cooldown of `max(10 seconds, last full refresh duration)` from
completion. The duration includes scanning and registry installation; startup,
foreground, and failed refresh attempts also set the cooldown. Its expiry
does not launch work: the next eligible poll can start one job. Mutations,
directory resolution, and explicit history reads bypass the cooldown or join
an in-flight refresh, without a second refresh in the same action. Those actions
can therefore still wait on discovery. Preview/download GET and HEAD and ZIP
neither trigger nor join scans: they use the published registry. New zones
return `404` until published, and removals take effect through later publication.
Acquisition still checks the destination and can block on filesystem I/O or an
exclusive writer; this change does not add a hard response-time guarantee.

**Unreleased diagnostics:** debug logs separate scan and registry-install
durations and report per-rule scan timing, matches, and newly cached paths.
The scanner shares observations across rules within one pass only; caches do
not persist into the next scan. Use these measurements to distinguish scan
cost from installation and overview storage I/O. See
[troubleshooting](troubleshooting.md#discovery-or-overview-is-slow).

Local filesystem commands read their own selected configuration on each
invocation. Running them with a different file from the daemon can apply
different zone settings. Pass `--config` consistently.

## Credentials And Sessions

```sh
pasteberth passwd --config /absolute/path/config.toml
```

This safely replaces the salted scrypt hash, not the TOML configuration. The
server reloads the password hash at each login, so new logins use the changed
password without restart. Each session records the password file's version;
after rotation, validation rejects and removes sessions created against the
previous version. No daemon restart is required for that invalidation. Sessions
also expire or end through logout, FIFO eviction at `max_sessions`, or process
restart. After a suspected disclosure, change the password and update trusted
clients' protected secret sources; clients must log in again.

Sessions are in memory and are not backed up. Keep the password file and TLS
private keys private, outside the code bundle. Back them up using a protected
backup destination if recovery requires the same credentials.

## Backup, Upgrade, and Recovery

Stop the service and all CLI or external writers before a consistent
filesystem-level backup. Preserve each
managed data file with its `.json` sidecar and preserve the directory structure.
Do not edit transaction markers or sidecars by hand.

On startup, Pasteberth reconciles interrupted transaction state. It is designed
to recover from crashes during publication, replacement, deletion, and rename
without treating foreign files as its own. Keep a copy of the zone before
manual recovery work.

An upgrade should use the same configured zone directories. Existing valid
sidecars and transaction markers from 1.5.0 are part of the compatibility
contract. Run `audit` after changing the executable or configuration, then
restart the service.

### Backup

Record the deployed version and configuration path. Preserve all configured
zone directories, matching sidecars, existing transaction artifacts, the TOML
configuration, password file, and any locally managed TLS credentials. Code is
replaceable from a release; zone data and deployment secrets are not part of
the code-only bundle. Back up foreign files separately if they matter, without
relabeling them as Pasteberth-owned content.

For example, with a pre-existing private backup destination and a stopped
service, copy a zone without stripping its permissions or sidecars:

```sh
systemctl --user stop pasteberth.service
# Also stop scripts, agents, and external writers using the zone.
rsync -a /srv/pasteberth-data/project-alpha/ /secure/backup/project-alpha/
```

Adapt paths, ownership, and backup tooling to your installation. The example
does not create a consistent snapshot if another process continues writing,
and does not itself copy configuration or credentials.

### Upgrade

1. Record the active wrapper path, version, configuration, and service unit.
2. Stop the service and other writers, then back up state as described above.
3. Obtain the intended tagged code bundle and replace only the code deployment.
4. Generate and verify deployment traceability using the [release process](release-process.md).
5. Audit the same configuration with the new deployed wrapper while the service is stopped.
6. Restart the service, reauthenticate, and verify overview, deposit, reference, and download behavior.

Keep the same zone paths unless deliberately migrating them. Do not synchronize
the entire repository over a production directory or put credentials and zones
under `PasteBerth/`. A manifest verifies copied code, not storage backups or
the already-running process; inspect `MainPID` and the service command after
restart. Do not assume arbitrary downgrade compatibility. Keep the previous
bundle and matching state backup until the upgrade is verified.

### Restore And Recovery

Restore into stopped, private directories and preserve the data/sidecar pairs
together. Recheck ownership, group membership, parent traversal, and configured
paths before starting the daemon. A backup made on another machine may have
different numeric UIDs/GIDs even when user names match.

Normal service initialization reconciles recognized interrupted transactions.
Recovery is conservative when entries no longer match the recorded identity;
it may preserve artifacts and log a problem rather than delete a possible
foreign file. Do not remove `.pasteberth.lock` to clear a busy error, or guess
which `.pb*` files are safe to delete. Stop writers, preserve an untouched copy
of the affected zone and logs, then investigate the specific failure. The
[storage reference](reference/storage.md#filesystem-layout-and-data-ownership)
lists reserved prefixes and legacy `.pbdel-...` ambiguity.
