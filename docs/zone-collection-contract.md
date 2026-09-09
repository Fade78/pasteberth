# Pasteberth Zone Collection Contract

Status: implemented contract. This document describes `[[zone_collection]]`
and the sidecar storage used by the zones it discovers. Changes marked
**Unreleased** describe the working tree, not the `2.1.21` release; the runtime
version remains `2.1.21`.

For a setup walkthrough, see [provisioning](provisioning.md) and the
[project-zones recipe](recipes/project-zones.md). This page is the detailed
discovery contract; shared zone settings are in the
[configuration reference](reference/configuration.md), and managed-pair
behavior is in the [storage reference](reference/storage.md). For scan costs,
scheduling tradeoffs, and timeout limits, see
[discovery performance](discovery-performance.md).

## 1. Purpose

A zone collection discovers existing directories and exposes them as Pasteberth
zones without writing those directories into `config.toml`. A discovered zone
exists while its directory satisfies the collection rule and disappears when it
no longer does.

The primary use case is a repository tree:

```text
/home/me/Depots/
  project-a/work/exchange/
  project-b/work/exchange/
  project-c/work/exchange/
```

The operator configures one collection for `^[^/]+/work/exchange$`, rather than
one static `[[zones]]` entry per repository.

Collection discovery is read-only with respect to the directory tree and the
configuration file. It does not create candidates, rewrite the configuration,
or migrate an existing static zone.

Discovery is refreshed when the service refreshes its zone registry. Web UI
overview requests use background discovery and serve the last complete
registry while it runs. The Web UI polls every 10 seconds while visible and
refreshes when a hidden tab becomes visible. A new matching directory appears
after a scan observes it and a later poll reads the completed registry, without
a service restart. `/api/groups` uses the same background refresh path.
**Unreleased:** not every overview request starts a scan; both endpoints share
the cooldown described in [refresh and lifecycle](#7-refresh-and-lifecycle).
Directory resolution and other explicit service operations use the refresh
path synchronously.

## 2. Configuration

The spelling of the configuration table is `[[zone_collection]]` (singular,
repeatable). Each collection has an ID in its own namespace. Collection IDs
must start with `@` and use the same lower-case identifier characters as zone
IDs after that prefix.

```toml
[[zone_collection]]
id = "@repositories"
base_directory = "/home/me/Depots"
pattern = "^[^/]+/work/exchange$"
max_depth = 4
label_mode = "git-or-relative"
retain = 10
file_group = "pasteberth"
min_free_percent = 2.0
reference_prefix = "@"
reference_suffix = ""
reference_list_prefix = ""
reference_list_suffix = ""
reference_separator = ","
allow_zip_download = true
color = "#304237"

[[groups]]
name = "Repositories"
selection = "pattern"
pattern = ["^@repositories$"]
layout = "tab"
```

### 2.1 Collection keys

| Key | Required/default | Meaning |
|---|---|---|
| `id` | required | Collection reference ID. It must match `^@[a-z0-9][a-z0-9_-]{0,63}$`. |
| `base_directory` | required | Absolute directory below which candidates are searched. It is resolved by the server and never created by discovery. |
| `pattern` | required | Case-sensitive Python regular expression, applied with `fullmatch` to the normalized resolved relative path. The path separator is `/`. |
| `max_depth` | `4` | Maximum number of path components below `base_directory`. |
| `label_mode` | `git-or-relative` | Use the nearest Git worktree name when found; `relative` uses the full relative path and `first-directory` uses its first component. |
| `storage_mode` | `sidecar` | `directory` is accepted only as a legacy value and normalized to sidecar storage with a warning. |
| `retain` | `10` | Number of managed pairs retained after a successful upload. |
| `max_items` | none | Legacy alias for `retain` when `retain` is absent. |
| `file_group` | none | Optional POSIX group name or numeric GID for files created by Pasteberth. |
| `min_free_percent` | `2.0` | Minimum free-space reserve on the filesystem. |
| `reference_prefix` | `@` | Prefix for one returned filesystem reference. |
| `reference_suffix` | empty | Suffix for one returned filesystem reference. |
| `reference_list_prefix` | empty | Prefix for a copied reference list. |
| `reference_list_suffix` | empty | Suffix for a copied reference list. |
| `reference_separator` | `,` | Separator for a copied reference list. |
| `allow_zip_download` | `true` | Whether several selected files can be downloaded as a ZIP. |
| `color` | none | Optional zone color. Omitted colors are deterministic and distinct within each collection. |

The old collection-to-group options do not exist. A collection is reusable and
does not own a group.

The server process must have read, write, and execute permission on a
collection's directories. Read and execute are enough to discover and inspect
a candidate, but uploads, sidecars, retention, and the `.pasteberth.lock` file
also require write permission. `file_group` affects files created by Pasteberth
but cannot grant access to a parent directory or make an unreadable candidate
discoverable.

At least one `[[zones]]` or `[[zone_collection]]` entry is required. A
collection-only configuration is valid even when it currently discovers no
directory. Static zones and discovered zones share one registry.

## 3. Group selection

Groups have only the selections `all`, `pattern`, and `other`.
They select presentation, not authorization: all authenticated clients share
access to the active zones. Collection membership and a POSIX `file_group`
are not per-zone Web access-control lists.

For `selection = "pattern"`, every expression is matched against both:

- normal zone IDs, such as `project-a-work-exchange`;
- collection IDs, such as `@repositories`.

When an expression matches a collection ID, all active zones in that collection
are selected. A zone may be selected directly, through several collections, or
both. The returned group membership contains each zone at most once and keeps
the registry order.

`selection = "other"` contains zones not selected by any pattern group.
`selection = "all"` contains every active zone. A pattern in an `all` or
`other` group is ignored, as with the existing group contract.

Collection IDs are references only. They are never published as zones and
cannot be used as upload targets.

## 4. Candidate discovery

Each collection's matching and traversal decisions are evaluated independently.

1. Pasteberth resolves `base_directory` and verifies that it is an existing directory.
2. The scanner walks existing directories below that base, following directory links.
3. Directory identity and resolved paths stop cycles and deduplicate aliases.
4. A resolved path is converted to a normalized relative path with `/` separators and matched with `fullmatch`.
5. `max_depth` counts components of that relative path.
6. A candidate must be an existing, accessible directory. Discovery never creates it.
7. A candidate is accepted only when its subtree contains no subdirectory.

**Unreleased:** one discovery pass shares filesystem observations across all
collection rules: strict full-path resolutions, stat results, directory
enumerations and their errors, and leaf-check results. Directory observations
are keyed by canonical resolved path, not just inode; each rule still has its
own identity-based visited set for cycle and alias handling. Resolving a new
path still checks the full path, not just a cached parent. A leaf check can
reject at the first subdirectory or inspection error; that partial probe is
not cached as a complete traversal listing. All these caches are discarded
after the pass, so a later scan checks the filesystem again. There is no
persistent filesystem cache or change to regex matching, depth, containment,
eligibility, or overlapping-rule policy.

Regular files at the candidate root are allowed, but files without a coherent
sidecar are not managed automatically. Directory links are followed for
discovery; content entries remain subject to the normal no-follow safety rules.

If multiple lexical paths resolve to the same directory, the lexicographically
smallest matching relative path is canonical. The candidate is one zone and is
assigned to every collection rule that matched it.

When several matching collections define different zone behavior, the
candidate is rejected with a diagnostic instead of inheriting an arbitrary
rule. Matching collections must agree on retention, references, ZIP policy,
color, filesystem group, free-space reserve, label mode, and storage mode.

## 5. Zone identity and labels

The public zone ID is derived only from the canonical normalized relative path:

1. join path components with `-`;
2. convert to lower case;
3. validate with `^[a-z0-9][a-z0-9_-]{0,63}$`.

For example:

```text
relative path: project-a/work/exchange
zone ID:      project-a-work-exchange
```

There is no automatic suffixing or truncation. A candidate is ignored when its
ID is invalid, too long, or collides with a static zone or another candidate.
`pasteberth audit` reports the reason.

With `label_mode = "git-or-relative"`, Pasteberth uses the nearest ancestor
containing a `.git` directory or worktree file. With `relative`, it always uses
the normalized relative path. With `first-directory`, it uses the first
component of that relative path, the root directly below `base_directory`.
Labels may be duplicated; IDs may not.

## 6. Sidecar storage

Collection zones use the same sidecar layout and ownership contract as static
zones:

```text
zone/
  report.pdf
  report.pdf.json
```

The data file and matching JSON sidecar form one managed item. Pasteberth
validates the pair before reads, replacements, renames, and deletions, and
preserves foreign files. `register FILE` can explicitly create or refresh a
sidecar without rewriting the data file.

Registration is filesystem-only. It validates against the CLI-selected
configuration or defaults, does not require an active zone or contact the
daemon, and does not run daemon retention or per-zone free-space checks. The
pair is visible only if its parent is an active zone and the daemon can read
it. See [`register`](reference/cli.md#filesystem-register) for the full contract.

`retain` counts coherent managed pairs after operations that apply retention;
it is not a continuous cap or byte quota. Foreign files, orphan sidecars, and
malformed sidecars are preserved. Recognized transaction remnants are handled
by recovery; unknown or ambiguous artifacts are preserved, not treated as
ordinary retained items. Locks coordinate Pasteberth operations, not arbitrary
external writers; see [transaction scope](reference/storage.md#transaction-scope).

## 7. Refresh and lifecycle

On each completed discovery refresh, Pasteberth replaces the dynamic candidate
snapshot, not necessarily on each overview request:

1. a new matching, accessible directory without user subdirectories becomes a zone;
2. a directory that no longer matches, becomes inaccessible, or gains a subdirectory is removed;
3. a static zone keeps precedence over a candidate at the same resolved path.

**Unreleased scheduling:** background requests share a cooldown of
`max(10 seconds, last full refresh duration)`, starting when the refresh
finishes. Duration includes discovery and registry installation, not just the
tree walk. Every refresh attempt sets this cooldown on completion, including
startup, foreground, background, and failed attempts. Its expiry does not
itself schedule work: the next eligible overview poll can launch one job, and
requests arriving while a refresh runs do not launch another.

**Unreleased:** explicit service actions bypass the background cooldown. They
perform a synchronous refresh or wait for the in-flight refresh instead of
starting a second one. Actions that already refreshed during zone validation do not
refresh again when acquiring their operation lock: one refresh or join per
service action, not one scan per helper call. A multi-request client workflow
can still invoke several service actions. This is request coalescing and
throttling, not a watcher, a hard timeout, or a discovery deadline.

The service replaces the dynamic zone configuration, destinations, locks, and
group memberships as one in-memory snapshot. A later request sees the current
snapshot; a request already holding a zone lock completes against its current
operation state.

This is a registry snapshot, not an atomic snapshot of all zone contents.
Overview requests still read each history and check free space synchronously;
slow filesystem I/O can block the response even with background discovery.
A busy or temporarily unavailable dynamic zone can have `busy: true`,
`count: null`, and `images: []`.
Do not interpret that placeholder history as file deletion. See the
[API response contract](reference/api.md#routes).

Dynamic zones use the same API shape as static zones. They appear in
`GET /api/zones`, and uploads, comments, deletes, previews, archives, and CLI
directory resolution use the current registry.

## 8. Audit and diagnostics

`pasteberth audit` validates collections without creating directories or changing
files. It reports:

- missing, unreadable, or invalid base directories;
- invalid expressions and depth values;
- candidates rejected for user subdirectories;
- resolved-path aliases and duplicate candidates;
- invalid, overlong, or colliding generated IDs;
- static-zone precedence;
- invalid or unusable `file_group` settings;
- the number of candidates currently discovered per collection.

One bad candidate does not hide valid candidates from the same collection. A
malformed collection is a configuration error; a transient candidate failure is
a diagnostic and leaves that candidate out until a later refresh.

## 9. Breaking change

The former `[[autozone]]` configuration and vocabulary are removed. Existing
configurations must declare an ID-bearing `[[zone_collection]]` and add an
explicit group pattern when the discovered zones should appear in a group.
