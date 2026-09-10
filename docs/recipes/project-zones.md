# Discover Project Zones

**Need:** Give each new project an exchange point without repeatedly editing
Pasteberth configuration. A single developer moving through projects one at a
time benefits from separate histories just as a multi-process environment does.

**Prerequisites:** The operator has loaded the collection and `Workspaces`
group from [provisioning](../provisioning.md), using `/srv/workspaces` and the
`<project>/work/exchange` convention. The environment provisioner can create
the directory with permissions suitable for the daemon.

## Steps

1. Create the project environment, for example `/srv/workspaces/beta`, through the usual template or provisioning tool.
2. Have the template create `beta/work/exchange` as a leaf directory. In the private, same-account setup from the provisioning guide:

```sh
(umask 077; mkdir -p /srv/workspaces/beta/work/exchange)
```

3. Verify permissions, including existing parents; `umask` does not change existing directories. Do not put subdirectories inside `exchange`.
4. Open the `Workspaces` group and wait for a scan that observes the new directory and a later poll that reads its result. A visible Web UI polls every 10 seconds; the Unreleased cooldown below can delay scan startup.
5. Confirm the `beta` label, then publish a small allowed artifact and verify its history entry and download.

## Observable Result

The new zone has ID `beta-work-exchange` and label `beta` with
`label_mode = "first-directory"`. It appears without a per-project config
edit, daemon restart, or SSH action by the Web client. The template need not
call a Pasteberth API or add a dependency to the project.

**Unreleased (runtime version still `2.1.21`):** overview requests reuse the
completed registry during a cooldown of the greater of 10 seconds or the last
full refresh duration, measured from completion. The next eligible poll can
start one background job; mutations, directory resolution, and legacy `/images`
history reads bypass the cooldown or wait for a running refresh. Generic `/items`
listings and downloads use only the published registry, without starting or
joining a scan: a new zone returns `404`
until published. This is not a fixed discovery or response deadline.

## Pitfalls

- The operator must load the collection rule once; discovery does not invent rules or create directories.
- Discovery is polling/background scanning, not a watcher or an instantaneous notification.
- Adding a subdirectory, losing access, or renaming the path can remove the zone when a later registry publishes that change, not necessarily on the next request. A changed path can also change its ID and invalidate old references.
- Merely copying files into the directory does not publish them. Use `drop` or explicit `register`.
- A group is a view of zones, not an access-control boundary. Use audit diagnostics when a candidate is missing.

See [provisioning](../provisioning.md),
[zone collection contract](../zone-collection-contract.md), and
[registration](register-file.md).
