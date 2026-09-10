# Provisioning Project Exchange Points

Put Pasteberth support in an environment template rather than in each project's
application code. A template creates a conventional directory; one configured
zone collection exposes eligible directories as zones. This suits a developer
working serially across many repositories as well as worktrees, sandboxes, and
short-lived environments.

## Separate the Responsibilities

- The **Pasteberth operator** installs and runs the service, configures collection rules and groups, chooses authentication and limits, and checks the daemon's filesystem access.
- The **environment provisioner** creates each project and its conventional exchange directory with the agreed permissions. This may be a template or provisioning script, with no Pasteberth client dependency.
- A **producer** explicitly publishes artifacts through Web, CLI, API, or MCP, or registers files it has already placed in a zone.

One person may do all three, but creating a project environment is distinct from
operating the service. These are responsibilities outside Pasteberth, not
application roles or permissions. A Web client does not need SSH merely for a
properly provisioned new project to appear.

## Establish a Convention

This example uses an existing base directory `/srv/workspaces`:

```text
/srv/workspaces/
  alpha/work/exchange/
  beta/work/exchange/
```

Each exchange directory is a leaf: it contains files, not subdirectories. Keep
build trees, unpacked archives, and nested output directories elsewhere. If the
directory is inside a repository, have the project template exclude its runtime
contents from version control as appropriate; discovery does not edit ignore
rules.

## Configure Once

Add the following collection and group to the service's existing TOML
configuration. This is a fragment, not a replacement for listener,
authentication, and other deployment settings. Use a unique collection ID and
group name if the configuration already contains these entries.

```toml
[[zone_collection]]
id = "@workspaces"
base_directory = "/srv/workspaces"
pattern = "^[^/]+/work/exchange$"
max_depth = 3
label_mode = "first-directory"
retain = 10
min_free_percent = 2.0
reference_prefix = "@"
allow_zip_download = true

[[groups]]
name = "Workspaces"
selection = "pattern"
pattern = ["^@workspaces$"]
layout = "tab"
hide_empty = false
show_count = true
```

The collection pattern fully matches the normalized, resolved path relative to
the base. `max_depth = 3` reaches the three components
`alpha/work/exchange`. `label_mode = "first-directory"` labels that zone
`alpha`, not `work/exchange` or a Git-derived name. Its ID is
`alpha-work-exchange`. The group matches the collection ID and includes all its
active zones. A collection does not create its own group.

A collection-only configuration is valid even before any candidates exist.
Static zones may also coexist. Audit the selected configuration, review its
diagnostics, then restart the daemon to load this new rule and group:

```sh
pasteberth audit --config /absolute/path/config.toml
```

Audit exits `0` when clean, `1` for warnings, and `2` for errors. It does not
create the base or candidate directories. See [deployment](deployment.md) for
starting or restarting the actual service.

## Provision Each Project

Assume `/srv/workspaces/alpha` already exists, its parent permissions are
appropriate, and the provisioner can create directories below it. For a private
setup where the provisioner and daemon use the same account, a template can run:

```sh
(umask 077; mkdir -p /srv/workspaces/alpha/work/exchange)
```

`umask` controls newly created directories; it does not repair existing
permissions. The template must also verify existing paths and the chosen
ownership. Substitute the newly created project's name on each run. There is
no per-project Pasteberth configuration edit or restart after the rule is loaded.

For several POSIX accounts, the operator and provisioner must instead arrange
a common filesystem group, suitable parent traversal permissions, and a setgid
exchange directory. They may add `file_group = "pasteberth"` to the collection
only after that group exists and the daemon's actual process credentials include
it. Every producer or consumer needs the access required for its operations.
`file_group` sets the group on Pasteberth-created files; it does not grant
directory access or update process membership. See [operations](operations.md).

## Verify Discovery and Publication

1. Check that the daemon can traverse the parents and read the base and exchange directory. Read/execute access can be enough for discovery, but write access is also needed for publication, sidecars, locks, and retention.
2. Run `pasteberth audit --config /absolute/path/config.toml` in the daemon's account/context and inspect collection candidate diagnostics. Account for service sandbox restrictions as well as Unix permissions.
3. Open the Web UI's `Workspaces` group. A visible page polls every 10 seconds; overview requests use background discovery and return the last completed registry while it runs. Wait for a scan that observes the new directory and a later poll that reads its result. The Unreleased cooldown below can delay scan startup.
4. Confirm the `alpha` label. The API zone ID for this example is `alpha-work-exchange`, not `alpha` or `@workspaces`.
5. Upload a small allowed test artifact and confirm its history entry and downloadable bytes. A directory appearing in a group alone does not prove it is writable or that a separately registered file is readable by the daemon.

No watcher continuously announces filesystem events. **Unreleased (runtime
version still `2.1.21`):** `/api/zones` and `/api/groups` share a background
cooldown of `max(10 seconds, last full refresh duration)` from completion,
including startup, foreground, and failed refresh attempts. Duration includes
registry installation. The next eligible poll can launch one job; not every
overview request starts a scan. Mutations, directory resolution, and legacy
`/images` history reads bypass the cooldown or wait for an in-flight refresh,
synchronously. Generic `/items` listings and downloads use the published
registry without starting discovery or waiting for a scan. A newly eligible
zone therefore returns `404` on download until
published; removals take effect through later registry publication.
A hidden browser tab refreshes when it becomes visible. There is no exact
discovery deadline, and overview history/free-space reads can still block.

## Eligibility and Lifecycle

- The base and candidate must already exist. Discovery creates neither and does not register ordinary files.
- The resolved candidate must remain below the base, match the case-sensitive pattern, fit `max_depth`, and contain no subdirectory, including a directory link.
- IDs join relative components with `-` and convert to lowercase. They must match `^[a-z0-9][a-z0-9_-]{0,63}$`; invalid, overlong, or colliding IDs are rejected rather than truncated or given suffixes. Prefer short project directory names using letters, digits, hyphens, and underscores.
- A static zone takes precedence at its directory. Multiple collections may include the same candidate only when their zone settings agree. Groups may present that one zone in several views without copying its files.
- A new eligible directory appears after a discovery refresh publishes it. A removed, inaccessible, nonmatching, or no-longer-leaf directory drops out after a later refresh publishes that change, not necessarily on the next request. Losing eligibility is not an instruction to delete its files.
- Renaming a project can change both its zone ID and references. Old references do not redirect, and previously known MCP zone IDs can stop working.
- Discovery is not a recursive filesystem browser, an ACL system, or a queue. Provisioning an exchange point does not assign work or publish a tool's outputs automatically.

Use the [project-zones recipe](recipes/project-zones.md) for the repeatable
day-to-day sequence. For rejected candidates, aliases, and snapshot details,
read the [zone collection contract](zone-collection-contract.md),
[configuration reference](reference/configuration.md), and
[troubleshooting](troubleshooting.md).
