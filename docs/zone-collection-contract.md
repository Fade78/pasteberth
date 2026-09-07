# Pasteberth Zone Collection Contract

Status: implemented contract. This document describes `[[zone_collection]]`
and the sidecar storage used by the zones it discovers.

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

Discovery is refreshed when the service refreshes its zone registry. The Web UI
polls the zone overview every 10 seconds while visible, so a new matching
directory normally appears on the next poll without a service restart.
Directory resolution and other service operations use the same refresh path.

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
| `label_mode` | `git-or-relative` | Use the nearest Git worktree name when found, or always use the relative path with `relative`. |
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

Each collection is evaluated independently.

1. Pasteberth resolves `base_directory` and verifies that it is an existing directory.
2. The scanner walks existing directories below that base, following directory links.
3. Directory identity and resolved paths stop cycles and deduplicate aliases.
4. A resolved path is converted to a normalized relative path with `/` separators and matched with `fullmatch`.
5. `max_depth` counts components of that relative path.
6. A candidate must be an existing, accessible directory. Discovery never creates it.
7. A candidate is accepted only when its subtree contains no subdirectory.

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
the normalized relative path. Labels may be duplicated; IDs may not.

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

`retain` counts coherent managed pairs. Foreign files, orphan sidecars,
malformed sidecars, and transaction remnants are preserved.

## 7. Refresh and lifecycle

On each normal zone-overview refresh, Pasteberth replaces the dynamic candidate
snapshot:

1. a new matching, accessible directory without user subdirectories becomes a zone;
2. a directory that no longer matches, becomes inaccessible, or gains a subdirectory is removed;
3. a static zone keeps precedence over a candidate at the same resolved path.

The service replaces the dynamic zone configuration, destinations, locks, and
group memberships as one in-memory snapshot. A later request sees the current
snapshot; a request already holding a zone lock completes against its current
operation state.

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
